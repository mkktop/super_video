"""Prepare text subtitles once, then burn them in the existing encoding pass."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..paths import ffmpeg_bin
from ..utils.process import WINDOWS_CREATE_FLAGS

TEXT_CODECS = {
    'subrip', 'srt', 'ass', 'ssa', 'webvtt', 'mov_text', 'text', 'sami',
    'microdvd', 'subviewer', 'vplayer', 'realtext', 'stl', 'pjs', 'jacosub',
    'mpl2', 'subviewer1',
}
ENCODINGS = ('utf-8-sig', 'gb18030', 'big5')
_capability_cache: dict[tuple, dict] = {}


def subtitle_capability() -> dict:
    exe = Path(shutil.which(ffmpeg_bin()) or ffmpeg_bin())
    try:
        stat = exe.stat()
        key = (str(exe.resolve()), stat.st_size, stat.st_mtime_ns)
        if key not in _capability_cache:
            p = subprocess.run([str(exe), '-hide_banner', '-filters'], capture_output=True,
                               timeout=10, creationflags=WINDOWS_CREATE_FLAGS)
            output = p.stdout.decode('utf-8', 'replace')
            supported = p.returncode == 0 and all(re.search(rf'^\s*\S+\s+{name}\s', output, re.M)
                                                 for name in ('ass', 'subtitles'))
            _capability_cache.clear()
            _capability_cache[key] = {'supported': supported, 'error': None if supported else '当前 FFmpeg 不支持 libass 字幕烧录，请使用包含 ass/subtitles 滤镜的 FFmpeg'}
        return dict(_capability_cache[key])
    except (OSError, subprocess.TimeoutExpired):
        return {'supported': False, 'error': '无法检测 FFmpeg 字幕烧录能力，请检查 FFmpeg 路径及安装'}


def normalize_language(language: str) -> str:
    value = language.strip().casefold().replace('_', '-')
    return {'chi': 'zh', 'zho': 'zh', 'jpn': 'ja', 'eng': 'en',
            'zh-cn': 'zh-hans', 'zh-sg': 'zh-hans', 'zh-tw': 'zh-hant', 'zh-hk': 'zh-hant'}.get(value, value)


def match_subtitle_track(info, language: str, title: str) -> int:
    language = normalize_language(language)
    candidates = []
    for track in info.subtitle_tracks:
        if track['codec'] not in TEXT_CODECS:
            continue
        actual = normalize_language(track.get('language', 'und'))
        if language and not (actual == language or ('-' not in language and actual.startswith(language + '-'))):
            continue
        if title and title.casefold() not in track.get('title', '').casefold():
            continue
        candidates.append(track['stream'])
    if len(candidates) != 1:
        reason = '没有匹配的文本字幕' if not candidates else f'匹配到 {len(candidates)} 条字幕，需增加标题条件'
        raise ValueError(f'{info.path.name}：{reason}（语言={language or "不限"}，标题包含={title or "不限"}）')
    return candidates[0]


@dataclass(frozen=True)
class BurnSubtitle:
    path: Path
    fonts: Path
    delay_s: float = 0
    fingerprint: str = ''
    warnings: tuple[str, ...] = ()


def validate_options(options: dict, info=None) -> dict:
    if not isinstance(options, dict):
        raise ValueError('subtitle 需为配置对象')
    source = options.get('source', 'embedded')
    if source not in ('embedded', 'external', 'matching'):
        raise ValueError('字幕来源需为 embedded / external / matching')
    result = {'source': source}
    encoding = options.get('encoding', 'utf-8-sig')
    if encoding not in ENCODINGS:
        raise ValueError('字幕编码需为 UTF-8 / GB18030 / Big5')
    result['encoding'] = encoding
    for name, default, low, high in [('delay_s', 0, -3600, 3600),
                                     ('font_size', 48, 12, 144),
                                     ('outline', 2, 0, 8), ('shadow', 1, 0, 8), ('margin_v', 50, 0, 400)]:
        v = options.get(name, default)
        if isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) or not low <= v <= high:
            raise ValueError(f'字幕 {name} 范围 {low} ~ {high}')
        result[name] = v
    font = options.get('font_name', 'Microsoft YaHei')
    if not isinstance(font, str) or not font.strip() or len(font) > 100 or any(c in font for c in ',\r\n{}'):
        raise ValueError('字幕字体名称无效')
    result['font_name'] = font.strip()
    color = options.get('font_color', '#FFFFFF')
    if not isinstance(color, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', color):
        raise ValueError('字幕颜色需为 #RRGGBB')
    result['font_color'] = color.upper()
    fonts = options.get('fonts_dir', '')
    if not isinstance(fonts, str) or (fonts and not Path(fonts).is_dir()):
        raise ValueError('字幕字体目录不存在')
    result['fonts_dir'] = fonts
    if source == 'embedded':
        selection = options.get('selection', 'track')
        if selection not in ('track', 'match'):
            raise ValueError('内封字幕选择需为 track / match')
        result['selection'] = selection
        index = 0 if selection == 'match' else options.get('stream', 0)
        if selection == 'match':
            language, title = options.get('language', ''), options.get('title', '')
            if not all(isinstance(v, str) and len(v) <= 100 for v in (language, title)):
                raise ValueError('字幕匹配语言和标题需为不超过 100 字的文本')
            language, title = normalize_language(language), title.strip()
            if not language and not title:
                raise ValueError('请设置字幕匹配语言或标题，不能固定第一条字幕轨')
            result.update(language=language, title=title)
            if info is not None:
                index = match_subtitle_track(info, language, title)
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ValueError('字幕轨序号需为非负整数')
        if info is not None:
            if index >= len(info.subtitles):
                raise ValueError('选中的字幕轨不存在')
            if info.subtitles[index] not in TEXT_CODECS:
                raise ValueError('目前仅支持文本字幕烧录；PGS/VobSub 等位图字幕请保留为 MKV 字幕轨')
        result['stream'] = index
    else:
        if source == 'matching':
            if info is None:
                return result
            candidates = [p for p in info.path.parent.iterdir()
                          if p.is_file() and p.stem.casefold() == info.path.stem.casefold()
                          and p.suffix.lower() in ('.srt', '.ass', '.ssa')]
            if len(candidates) != 1:
                raise ValueError(f'{info.path.name} 需有唯一同名 SRT/ASS/SSA 字幕，当前找到 {len(candidates)} 个')
            path = str(candidates[0])
        else:
            path = options.get('path', '')
        if not isinstance(path, str) or not Path(path).is_file():
            raise ValueError('外部字幕文件不存在')
        p = Path(path)
        if p.suffix.lower() not in ('.srt', '.ass', '.ssa'):
            raise ValueError('外部字幕支持 SRT / ASS / SSA')
        if p.stat().st_size > 32 * 1024 * 1024:
            raise ValueError('字幕文件不能超过 32MB')
        result.update(source='external', path=str(p.resolve()))
    return result


def _digest(path: Path) -> str:
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def _run(args: list[str], cwd: Path, timeout=120) -> str:
    p = subprocess.run([ffmpeg_bin(), '-hide_banner', '-nostdin', '-y', *args],
                       cwd=cwd, capture_output=True, timeout=timeout,
                       creationflags=WINDOWS_CREATE_FLAGS)
    log = p.stderr.decode('utf-8', 'replace')
    if p.returncode:
        raise ValueError(f'字幕处理失败：{log[-1000:]}')
    return log


def prepare_subtitle(info, options: dict, directory: Path, size: tuple[int, int]) -> BurnSubtitle:
    """Persist an immutable snapshot; reject changed inputs when resuming."""
    capability = subtitle_capability()
    if not capability['supported']:
        raise ValueError(capability['error'])
    options = validate_options(options, info)
    source = Path(options['path']) if options['source'] == 'external' else info.path
    stat = source.stat()
    render_options = dict(options)
    for name, default in (('selection', 'track'), ('shadow', 1), ('font_color', '#FFFFFF')):
        if render_options.get(name) == default:
            render_options.pop(name, None)
    signature = {'version': 1, 'options': render_options, 'source': str(source.resolve()),
                 'size': stat.st_size, 'mtime': stat.st_mtime_ns, 'target': size,
                 'fps': info.fps_str}
    exe = Path(ffmpeg_bin())
    if exe.is_file():
        signature['renderer'] = (str(exe.resolve()), exe.stat().st_size, exe.stat().st_mtime_ns)
    signature['system_fonts'] = []
    for base in (Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts',
                 Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'Microsoft/Windows/Fonts'):
        if base.is_dir():
            signature['system_fonts'].extend((str(p), p.stat().st_size, p.stat().st_mtime_ns)
                                             for p in sorted(base.iterdir()) if p.is_file())
    if options['source'] == 'external':
        signature['sha256'] = _digest(source)
    font_files = sorted(p for p in Path(options['fonts_dir']).iterdir()
                        if p.is_file() and p.suffix.lower() in ('.ttf', '.otf', '.ttc')) if options['fonts_dir'] else []
    signature['fonts'] = [(p.name, _digest(p)) for p in font_files]
    fingerprint = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
    manifest = directory / 'manifest.json'
    if manifest.exists():
        data = json.loads(manifest.read_text(encoding='utf-8'))
        if data['fingerprint'] != fingerprint:
            raise ValueError('字幕、字体或输出设置已改变，不能复用旧分段；请新建任务')
        if not (directory / 'subtitle.ass').is_file() or _digest(directory / 'subtitle.ass') != data['ass_hash']:
            raise ValueError('字幕快照损坏，请新建任务')
        for filename, digest in data.get('fonts', []):
            if not (directory / 'fonts' / filename).is_file() or _digest(directory / 'fonts' / filename) != digest:
                raise ValueError('字体快照损坏，请新建任务')
        return BurnSubtitle(directory / 'subtitle.ass', directory / 'fonts',
                            options['delay_s'], fingerprint, tuple(data.get('warnings', [])))
    work = directory.with_name(directory.name + '.tmp-' + uuid.uuid4().hex)
    work.mkdir(parents=True)
    fonts = work / 'fonts'
    fonts.mkdir()
    try:
        for i, path in enumerate(font_files):
            shutil.copyfile(path, fonts / f'user_{i}{path.suffix.lower()}')
        plain = False
        if options['source'] == 'external':
            try:
                text = source.read_text(encoding=options['encoding'])
            except UnicodeError as e:
                raise ValueError('字幕编码不匹配，请选择正确的 UTF-8 / GB18030 / Big5 编码') from e
            local = work / ('input' + source.suffix.lower())
            local.write_text(text, encoding='utf-8')
            plain = source.suffix.lower() == '.srt'
            if source.suffix.lower() == '.ass':
                shutil.copyfile(local, work / 'subtitle.ass')
            else:
                _run(['-loglevel', 'error', '-i', str(local), '-map', '0:s:0',
                      '-c:s', 'ass', 'subtitle.ass'], work)
        else:
            from .probe import _ffprobe_json
            data = _ffprobe_json(['-show_streams', '-of', 'json', str(info.path)])
            attachments = [s for s in data.get('streams', []) if s.get('codec_type') == 'attachment']
            dump = []
            for i, s in enumerate(attachments):
                ext = Path(s.get('tags', {}).get('filename', '')).suffix.lower()
                if ext in ('.ttf', '.otf', '.ttc'):
                    dump += [f'-dump_attachment:t:{i}', str(fonts / f'attached_{i}{ext}')]
            if dump:
                _run(['-loglevel', 'error', *dump, '-i', str(info.path),
                      '-map', '0:v:0', '-frames:v', '0', '-f', 'null', '-'], work)
            codec = info.subtitles[options['stream']]
            plain = codec not in ('ass', 'ssa')
            _run(['-loglevel', 'error', '-i', str(info.path), '-map', f"0:s:{options['stream']}",
                  '-c:s', 'copy' if codec == 'ass' else 'ass', 'subtitle.ass'], work)
        ass = work / 'subtitle.ass'
        text = ass.read_text(encoding='utf-8-sig')
        if not re.search(r'^Dialogue\s*:', text, re.M | re.I):
            raise ValueError('字幕没有可显示的文本事件')
        if plain:
            # Text formats have no authored layout; give them a stable 1080p design grid.
            text = re.sub(r'^PlayRes[XY]:.*\n?', '', text, flags=re.M | re.I)
            text = text.replace('[Script Info]', '[Script Info]\nPlayResX: 1920\nPlayResY: 1080')
            rgb = options['font_color'][1:]
            ass_color = '&H00' + rgb[4:6] + rgb[2:4] + rgb[0:2]
            style = (f"Style: Default,{options['font_name']},{options['font_size']},{ass_color},"
                     '&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,'
                     f"{options['outline']},{options['shadow']},2,60,60,{options['margin_v']},1")
            text = re.sub(r'^Style:.*$', lambda m: style, text, flags=re.M)
        ass.write_text(text, encoding='utf-8')
        # Exercise every authored font, including inline overrides, before long AI inference.
        names = {line.split(',')[1] for line in text.splitlines()
                 if line.startswith('Style:') and len(line.split(',')) > 2}
        names.update(re.findall(r'\\fn([^\\}]+)', text))
        header = re.split(r'^\[Events\]\s*$', text, maxsplit=1, flags=re.M | re.I)[0]
        check = header + ('[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n')
        check += '\n'.join(f'Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,{{\\fn{name}}}字幕 Aa'
                           for name in sorted(names))
        (work / 'fontcheck.ass').write_text(check, encoding='utf-8')
        log = _run(['-loglevel', 'info', '-f', 'lavfi', '-i', f'color=s={size[0]}x{size[1]}:r=1',
                    '-vf', 'ass=subtitle.ass:fontsdir=fonts,ass=fontcheck.ass:fontsdir=fonts',
                    '-frames:v', '1', '-f', 'null', '-'], work, 30)
        warnings = [line.strip() for line in log.splitlines()
                    if 'failed' in line.lower() or 'glyph' in line.lower()]
        for requested, selected in re.findall(r'fontselect: \((.*?),\s*\d+,\s*\d+\) -> (.*?),', log):
            normalize = lambda s: re.sub(r'[^\w]', '', s).casefold()
            if not normalize(selected).startswith(normalize(requested)):
                warning = f'字体 {requested} 使用了替代字体 {selected}，请检查预览或补充字体目录'
                if warning not in warnings:
                    warnings.append(warning)
        payload = dict(fingerprint=fingerprint, ass_hash=_digest(ass), warnings=warnings,
                       fonts=[(p.name, _digest(p)) for p in fonts.iterdir() if p.is_file()])
        (work / 'manifest.json').write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        directory.parent.mkdir(parents=True, exist_ok=True)
        work.rename(directory)
        return BurnSubtitle(directory / 'subtitle.ass', directory / 'fonts',
                            options['delay_s'], fingerprint, tuple(warnings))
    finally:
        if work.exists():
            shutil.rmtree(work)


def filter_path(path: Path) -> str:
    """Escape an option value, then quote it at the filtergraph parsing layer."""
    value = path.resolve().as_posix().replace('\\', '\\\\').replace(':', '\\:').replace("'", "\\'")
    return "'" + value.replace("'", "'\\''") + "'"


def output_filters(frame_size: tuple[int, int], target: tuple[int, int],
                   burn: BurnSubtitle | None, start_s: float = 0) -> str | None:
    parts = []
    if frame_size != target:
        parts.append(f'scale={target[0]}:{target[1]}:flags=lanczos')
    if burn:
        parts.extend([f'setpts=PTS+({start_s:.12f}-{burn.delay_s:.12f})/TB',
                      f'ass=filename={filter_path(burn.path)}:fontsdir={filter_path(burn.fonts)}',
                      'setpts=PTS-STARTPTS'])
    return ','.join(parts) or None


def check_resume_fingerprint(work: Path, burn: BurnSubtitle | None) -> None:
    """Encoded segments cannot be reused with a different subtitle render."""
    marker = work / 'subtitle-render.json'
    fingerprint = burn.fingerprint if burn else ''
    if marker.exists():
        if json.loads(marker.read_text())['fingerprint'] != fingerprint:
            raise ValueError('字幕烧录设置与已完成分段不同，请新建任务')
    elif fingerprint and list(work.glob('seg_*.mp4')):
        raise ValueError('已有分段未烧录字幕，请新建任务')
    else:
        tmp = marker.with_suffix(f'.tmp{uuid.uuid4().hex}')
        tmp.write_text(json.dumps({'fingerprint': fingerprint}))
        tmp.replace(marker)
