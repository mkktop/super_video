"""Timed top watermarks, prepared once and rendered in the final encoding pass."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from .subtitle import _digest, _run, filter_path, subtitle_capability
from ..paths import ffmpeg_bin


def validate_watermark(options: dict) -> dict:
    if not isinstance(options, dict):
        raise ValueError('片头水印需为配置对象')
    kind = options.get('kind', 'text')
    position = options.get('position', 'top-right')
    if kind not in ('text', 'image') or position not in ('top-left', 'top-center', 'top-right'):
        raise ValueError('水印类型或顶部位置无效')
    result = dict(kind=kind, position=position)
    for name, default, low, high in [('start_s', 0, 0, 86400), ('duration_s', 5, .1, 3600),
                                     ('opacity', .85, .05, 1), ('width_pct', 12, 1, 50),
                                     ('font_size', 48, 12, 144), ('margin', 40, 0, 400)]:
        value = options.get(name, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f'水印 {name} 范围 {low} ~ {high}')
        result[name] = value
    if kind == 'image':
        path = options.get('path', '')
        if not isinstance(path, str) or not Path(path).is_file() or Path(path).suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp'):
            raise ValueError('请选择存在的 PNG / JPG / WebP Logo 图片')
        if Path(path).stat().st_size > 32 * 1024 * 1024:
            raise ValueError('Logo 图片不能超过 32MB')
        result['path'] = str(Path(path).resolve())
    else:
        text = options.get('text', '')
        font = options.get('font_name', 'Microsoft YaHei')
        color = options.get('font_color', '#FFFFFF')
        if not isinstance(text, str) or not text.strip() or len(text) > 100:
            raise ValueError('水印文字需为 1 ~ 100 字')
        if not isinstance(font, str) or not font.strip() or len(font) > 100 or any(c in font for c in ',\r\n{}'):
            raise ValueError('水印字体名称无效')
        if not isinstance(color, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', color):
            raise ValueError('水印颜色需为 #RRGGBB')
        if not subtitle_capability()['supported']:
            raise ValueError('当前 FFmpeg 不支持 libass 文字水印')
        result.update(text=text.strip(), font_name=font.strip(), font_color=color.upper())
    return result


@dataclass(frozen=True)
class Watermark:
    path: Path
    options: dict
    fingerprint: str
    warnings: tuple[str, ...] = ()


def _time(value: float) -> str:
    ticks = round(value * 100)
    return f'{ticks // 360000}:{ticks // 6000 % 60:02}:{ticks // 100 % 60:02}.{ticks % 100:02}'


def prepare_watermark(options: dict, directory: Path, size: tuple[int, int]) -> Watermark:
    options = validate_watermark(options)
    exe = Path(shutil.which(ffmpeg_bin()) or ffmpeg_bin())
    stat = exe.stat()
    signature = dict(options=options, target=size, renderer=(str(exe), stat.st_size, stat.st_mtime_ns))
    if options['kind'] == 'image':
        signature['source_hash'] = _digest(Path(options['path']))
    else:
        signature['fonts'] = []
        for base in (Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts',
                     Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'Microsoft/Windows/Fonts'):
            if base.is_dir():
                signature['fonts'].extend((str(p), p.stat().st_size, p.stat().st_mtime_ns)
                                          for p in sorted(base.iterdir()) if p.is_file())
    fingerprint = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
    filename = 'logo.png' if options['kind'] == 'image' else 'watermark.ass'
    manifest = directory / 'manifest.json'
    if manifest.exists():
        data = json.loads(manifest.read_text())
        if data['fingerprint'] != fingerprint:
            raise ValueError('水印内容或输出设置已改变，不能复用旧分段；请新建任务')
        if not (directory / filename).is_file() or _digest(directory / filename) != data['hash']:
            raise ValueError('水印快照损坏，请新建任务')
        return Watermark(directory / filename, options, fingerprint, tuple(data.get('warnings', [])))
    work = directory.with_name(directory.name + '.tmp-' + uuid.uuid4().hex)
    work.mkdir(parents=True)
    try:
        warnings = []
        if options['kind'] == 'image':
            # Snapshot a decoded, resized still image; preserve the source alpha channel.
            width = max(2, round(size[0] * options['width_pct'] / 100))
            height = max(2, round(size[1] * .4))
            _run(['-loglevel', 'error', '-i', options['path'], '-frames:v', '1',
                  '-vf', f'scale={width}:{height}:force_original_aspect_ratio=decrease,format=rgba,colorchannelmixer=aa={options["opacity"]}',
                  filename], work)
        else:
            rgb = options['font_color'][1:]
            alpha = f'{round((1 - options["opacity"]) * 255):02X}'
            color = '&H' + alpha + rgb[4:6] + rgb[2:4] + rgb[:2]
            alignment = {'top-left': 7, 'top-center': 8, 'top-right': 9}[options['position']]
            # Literal text only: prevent ASS override tags or escapes in user text.
            text = options['text'].replace('\\', '＼').replace('{', '｛').replace('}', '｝').replace('\r', '').replace('\n', r'\N')
            ass = ('[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\nScaledBorderAndShadow: yes\n'
                   '[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n'
                   f'Style: Default,{options["font_name"]},{options["font_size"]},{color},{color},&H{alpha}000000,&H{alpha}000000,0,0,0,0,100,100,0,0,1,2,1,{alignment},{round(options["margin"])},{round(options["margin"])},{round(options["margin"])},1\n'
                   '[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'
                   f'Dialogue: 0,{_time(options["start_s"])},{_time(options["start_s"] + options["duration_s"])},Default,,0,0,0,,{text}\n')
            (work / filename).write_text(ass, encoding='utf-8')
            log = _run(['-loglevel', 'info', '-f', 'lavfi', '-i', f'color=s={size[0]}x{size[1]}',
                        '-vf', f'setpts=PTS+{options["start_s"] + .02}/TB,ass=watermark.ass',
                        '-frames:v', '1', '-f', 'null', '-'], work)
            warnings = [line.strip() for line in log.splitlines() if 'failed' in line.lower() or 'glyph' in line.lower()]
            for requested, selected in re.findall(r'fontselect: \((.*?),\s*\d+,\s*\d+\) -> (.*?),', log):
                normalize = lambda s: re.sub(r'[^\w]', '', s).casefold()
                if not normalize(selected).startswith(normalize(requested)):
                    warnings.append(f'水印字体 {requested} 使用了替代字体 {selected}，请检查预览')
        (work / 'manifest.json').write_text(json.dumps(dict(fingerprint=fingerprint, hash=_digest(work / filename), warnings=warnings)))
        directory.parent.mkdir(parents=True, exist_ok=True)
        work.rename(directory)
        return Watermark(directory / filename, options, fingerprint, tuple(warnings))
    finally:
        if work.exists():
            shutil.rmtree(work)


def watermark_filter(base: str | None, watermark: Watermark | None, size: tuple[int, int], start_s: float) -> str | None:
    if not watermark:
        return base
    o = watermark.options
    # Skip decoding/rendering the logo entirely after its display interval.
    if start_s >= o['start_s'] + o['duration_s']:
        return base
    prefix = base + ',' if base else ''
    if o['kind'] == 'text':
        return prefix + f'setpts=PTS+{start_s:.12f}/TB,ass=filename={filter_path(watermark.path)},setpts=PTS-STARTPTS'
    xmargin = round(o['margin'] * size[0] / 1920)
    ymargin = round(o['margin'] * size[1] / 1080)
    x = {'top-left': str(xmargin), 'top-center': '(main_w-overlay_w)/2',
         'top-right': f'main_w-overlay_w-{xmargin}'}[o['position']]
    enable = f"gte(t+{start_s:.12f},{o['start_s']})*lt(t+{start_s:.12f},{o['start_s'] + o['duration_s']})"
    return (f'{base or "null"}[wm_base];movie=filename={filter_path(watermark.path)},setpts=PTS-STARTPTS[wm_logo];'
            f"[wm_base][wm_logo]overlay=x='{x}':y={ymargin}:eof_action=repeat:repeatlast=1:enable='{enable}'")


def check_watermark_resume(work: Path, watermark: Watermark | None) -> None:
    marker = work / 'watermark-render.json'
    fingerprint = watermark.fingerprint if watermark else ''
    if marker.exists():
        if json.loads(marker.read_text())['fingerprint'] != fingerprint:
            raise ValueError('水印与已完成分段不同，请新建任务')
    elif fingerprint and list(work.glob('seg_*.mp4')):
        raise ValueError('已有分段未包含水印，请新建任务')
    else:
        tmp = marker.with_suffix('.tmp' + uuid.uuid4().hex)
        tmp.write_text(json.dumps(dict(fingerprint=fingerprint)))
        tmp.replace(marker)
