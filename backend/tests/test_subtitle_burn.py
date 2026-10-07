"""Real FFmpeg rendering, pipeline integration and resume correctness."""
import asyncio
import json
import subprocess
from pathlib import Path

import numpy as np
import pytest
from fastapi import HTTPException

from sv.paths import ffmpeg_bin
from sv.pipeline.probe import probe
from sv.pipeline.subtitle import prepare_subtitle, output_filters, validate_options
from sv.pipeline.stream import EncodeOpts, StreamPipeline
from sv.pipeline.segmented import SegmentedPipeline
from sv.pipeline.chunked import ChunkedPipeline
from sv.utils.process import WINDOWS_CREATE_FLAGS


def ffmpeg(*args, input=None):
    p = subprocess.run([ffmpeg_bin(), '-hide_banner', '-loglevel', 'error', '-nostdin', '-y', *args],
                       input=input, capture_output=True, creationflags=WINDOWS_CREATE_FLAGS, timeout=30)
    assert p.returncode == 0, p.stderr.decode('utf-8', 'replace')
    return p.stdout


class Nearest2x:
    scale = 2

    def process(self, frame):
        return np.repeat(np.repeat(frame, 2, axis=0), 2, axis=1)


class Interp:
    def interpolate(self, a, b):
        return a


@pytest.fixture
def media(tmp_path):
    video = tmp_path / 'video.mp4'
    ffmpeg('-f', 'lavfi', '-i', 'color=c=0x304050:s=160x90:r=24', '-t', '2',
           '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(video))
    sub = tmp_path / "字幕, [测试] ' .srt"
    sub.write_text('1\n00:00:00,500 --> 00:00:01,500\n你好 Subtitle\n', encoding='utf-8')
    return probe(video), sub


def frame(path, t):
    raw = ffmpeg('-ss', str(t), '-i', str(path), '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-')
    return np.frombuffer(raw, np.uint8)


@pytest.mark.parametrize('kind', ['stream', 'segmented', 'interpolated', 'chunked'])
def test_burn_in_each_pipeline(media, tmp_path, monkeypatch, kind):
    import sv.pipeline.segmented as segmented
    import sv.pipeline.chunked as chunked
    monkeypatch.setattr(segmented, 'TEMP_DIR', tmp_path)
    monkeypatch.setattr(chunked, 'TEMP_DIR', tmp_path)
    info, sub = media
    burn = prepare_subtitle(info, {'source': 'external', 'path': str(sub), 'font_size': 96}, tmp_path / "快照, ' [a]", (320, 180))
    enc = EncodeOpts(subtitle_mode='burn', burn_subtitle=burn, preset='ultrafast', crf=10)
    out = tmp_path / 'output.mp4'
    if kind == 'stream':
        pipe = StreamPipeline(info, out, Nearest2x(), enc)
    elif kind == 'chunked':
        pipe = ChunkedPipeline(info, out, Nearest2x(), enc, task_id='test', chunk=12)
    else:
        pipe = SegmentedPipeline(info, out, Nearest2x(), enc, task_id='test', seg_frames=24,
                                 interp=Interp() if kind == 'interpolated' else None)
    stats = asyncio.run(pipe.run())
    result = probe(out)
    assert (result.width, result.height) == (320, 180)
    assert not result.subtitles  # burned text must not also produce a soft subtitle track
    factor = 2 if kind == 'interpolated' else 1
    assert stats.frames == info.total_frames * factor
    assert result.total_frames == info.total_frames * factor
    plain, shown, after = frame(out, .25), frame(out, 1.25), frame(out, 1.75)
    assert np.count_nonzero(shown > 180) > 30
    assert np.count_nonzero(plain > 180) == np.count_nonzero(after > 180) == 0


def test_embedded_extraction_and_probe(media, tmp_path):
    info, sub = media
    mkv = tmp_path / 'with_sub.mkv'
    ffmpeg('-i', str(info.path), '-i', str(sub), '-map', '0:v', '-map', '1:s', '-c', 'copy',
           '-metadata:s:s:0', 'language=zho', '-metadata:s:s:0', 'title=中文字幕', str(mkv))
    source = probe(mkv)
    assert source.subtitle_tracks[0]['language'] == 'zho'
    assert source.subtitle_tracks[0]['title'] == '中文字幕'
    burn = prepare_subtitle(source, {'source': 'embedded', 'stream': 0}, tmp_path / 'snapshot', (320, 180))
    assert '你好 Subtitle' in burn.path.read_text(encoding='utf-8')


def test_snapshot_rejects_modified_subtitle(media, tmp_path):
    info, sub = media
    options = {'source': 'external', 'path': str(sub)}
    burn = prepare_subtitle(info, options, tmp_path / 'snapshot', (320, 180))
    assert prepare_subtitle(info, options, tmp_path / 'snapshot', (320, 180)) == burn
    sub.write_text(sub.read_text(encoding='utf-8').replace('你好', '变化'), encoding='utf-8')
    with pytest.raises(ValueError, match='不能复用旧分段'):
        prepare_subtitle(info, options, tmp_path / 'snapshot', (320, 180))


def test_delay_and_segment_animation(media, tmp_path):
    info, sub = media
    # Test fixture is self-contained: preserve a normalized header, author animated events.
    burn = prepare_subtitle(info, {'source': 'external', 'path': str(sub)}, tmp_path / 'base', (320, 180))
    header = burn.path.read_text(encoding='utf-8').split('[Events]')[0]
    ass = tmp_path / 'animated.ass'
    ass.write_text(header + r'''[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.50,0:00:01.50,Default,,0,0,0,,{\move(200,800,1500,800)\fad(100,100)}Animated
''', encoding='utf-8')
    burn = prepare_subtitle(info, {'source': 'external', 'path': str(ass), 'delay_s': .25}, tmp_path / 'animation', (320, 180))
    def hashes(start, n):
        vf = output_filters((320, 180), (320, 180), burn, start)
        raw = ffmpeg('-f', 'lavfi', '-i', 'color=c=0x304050:s=320x180:r=24', '-vf', vf,
                     '-frames:v', str(n), '-fps_mode', 'passthrough', '-f', 'framemd5', '-')
        return [line.split(',')[-1].strip() for line in raw.decode().splitlines() if not line.startswith('#')]
    full = hashes(0, 48)
    assert hashes(1, 24) == full[24:]
    assert full[6] != full[24]


def test_segment_resume_and_changed_render(media, tmp_path, monkeypatch):
    import sv.pipeline.segmented as module
    monkeypatch.setattr(module, 'TEMP_DIR', tmp_path)
    info, sub = media
    burn = prepare_subtitle(info, {'source': 'external', 'path': str(sub)}, tmp_path / 'snapshot', (320, 180))
    enc = EncodeOpts(subtitle_mode='burn', burn_subtitle=burn, preset='ultrafast')
    out = tmp_path / 'output.mp4'
    def pipe(encoder):
        return SegmentedPipeline(info, out, Nearest2x(), encoder, task_id='resume', seg_frames=24, cleanup=False)
    asyncio.run(pipe(enc).run())
    work = tmp_path / 'segmented/resume'
    segments = {p.name: p.stat().st_mtime_ns for p in work.glob('seg_*.mp4')}
    asyncio.run(pipe(enc).run())
    assert segments == {p.name: p.stat().st_mtime_ns for p in work.glob('seg_*.mp4')}
    with pytest.raises(ValueError, match='已完成分段不同'):
        asyncio.run(pipe(EncodeOpts()).run())


def test_matching_encoding_and_invalid_options(media, tmp_path):
    info, sub = media
    matching = info.path.with_suffix('.srt')
    matching.write_text(sub.read_text(encoding='utf-8'), encoding='gb18030')
    burn = prepare_subtitle(info, {'source': 'matching', 'encoding': 'gb18030'}, tmp_path / 'snapshot', (320, 180))
    assert '你好' in burn.path.read_text(encoding='utf-8')
    info.path.with_suffix('.ass').write_text('duplicate')
    with pytest.raises(ValueError, match='唯一同名'):
        validate_options({'source': 'matching'}, info)
    for opts in [None, {'delay_s': float('nan')}, {'stream': True}, {'font_name': 'font,inject'}, {'source': 'external', 'path': str(tmp_path)}]:
        with pytest.raises(ValueError):
            validate_options(opts, info)


def test_preview_endpoint(media, tmp_path, monkeypatch):
    from sv.server.routes import models
    monkeypatch.setattr(models, 'TEMP_DIR', tmp_path)
    info, sub = media
    result = models.subtitle_preview(models.SubtitlePreviewBody(
        input=str(info.path), subtitle={'source': 'external', 'path': str(sub)}, width=320, height=180))
    assert result.body.startswith(b'\x89PNG')
    assert float(result.headers['X-Subtitle-Time']) == .7
    assert not list(tmp_path.glob('subtitle-preview-*'))
    with pytest.raises(HTTPException):
        models.subtitle_preview(models.SubtitlePreviewBody(input=str(info.path), width=320, height=180))


@pytest.mark.parametrize('mode', ['burn', 'burn_keep'])
def test_task_api_validates_and_presets_do_not_bind_previous_file(media, tmp_path, monkeypatch, mode):
    from sv.server import db, user_presets
    from sv.server.routes.tasks import create_task, TaskCreate
    from sv.server.routes.models import create_preset, PresetCreate
    monkeypatch.setenv('SV_DB', str(tmp_path / 'api.db'))
    monkeypatch.setattr(user_presets, 'PRESETS_PATH', tmp_path / 'presets.json')
    db.init_db()
    info, sub = media
    options = {'source': 'external', 'path': str(sub), 'font_size': 60}
    task = create_task(TaskCreate(input=str(info.path), model_id='realesr-animevideov3',
                                 params={'subtitle_mode': mode, 'subtitle': options}))
    assert task['params']['subtitle']['font_size'] == 60
    assert task['params']['subtitle_mode'] == mode
    db.delete_task(task['id'])
    for params in [{'subtitle_mode': 'burn'},
                   {'subtitle_mode': 'burn', 'subtitle': options, 'out_kind': 'png'}]:
        with pytest.raises(HTTPException) as exc:
            create_task(TaskCreate(input=str(info.path), model_id='realesr-animevideov3', params=params))
        assert exc.value.status_code == 400
    preset = create_preset(PresetCreate(name='字幕预设', model_id='realesr-animevideov3',
                                       target_scale=2, subtitle_mode=mode, subtitle=options))
    assert preset['subtitle']['source'] == 'matching'
    assert 'path' not in preset['subtitle'] and 'stream' not in preset['subtitle']
    assert preset['subtitle']['font_size'] == 60
    matched = create_preset(PresetCreate(name='语言匹配预设', model_id='realesr-animevideov3',
                                        target_scale=2, subtitle_mode=mode,
                                        subtitle={'source': 'embedded', 'selection': 'match',
                                                  'language': 'chi', 'title': '简体', 'stream': None}))
    assert matched['subtitle']['source'] == 'embedded'
    assert matched['subtitle']['selection'] == 'match'
    assert matched['subtitle']['language'] == 'zh'
    assert matched['subtitle']['title'] == '简体'
    assert 'stream' not in matched['subtitle']
    from sv.pipeline import subtitle
    monkeypatch.setattr(subtitle, 'subtitle_capability', lambda: {'supported': False, 'error': '缺少 libass'})
    with pytest.raises(HTTPException) as exc:
        create_task(TaskCreate(input=str(info.path), model_id='realesr-animevideov3',
                               params={'subtitle_mode': mode, 'subtitle': options}))
    assert exc.value.status_code == 400 and 'libass' in str(exc.value.detail)


def test_embedded_ass_with_font_attachment(media, tmp_path):
    info, sub = media
    font = Path('C:/Windows/Fonts/msyh.ttc')
    if not font.is_file():
        pytest.skip('Windows test font unavailable')
    burn = prepare_subtitle(info, {'source': 'external', 'path': str(sub)}, tmp_path / 'base', (320, 180))
    mkv = tmp_path / 'attached.mkv'
    ffmpeg('-i', str(info.path), '-i', str(burn.path), '-map', '0:v', '-map', '1:s', '-c', 'copy',
           '-attach', str(font), '-metadata:s:t:0', 'mimetype=application/x-truetype-font',
           '-metadata:s:t:0', 'filename=中文字体.ttc', str(mkv))
    prepared = prepare_subtitle(probe(mkv), {'source': 'embedded'}, tmp_path / 'attached', (320, 180))
    copied = list(prepared.fonts.glob('*.ttc'))
    assert len(copied) == 1 and copied[0].read_bytes() == font.read_bytes()


@pytest.mark.parametrize('fps', ['24000/1001', '30000/1001'])
def test_fractional_fps_segment_timing(media, tmp_path, fps):
    from fractions import Fraction
    info, sub = media
    burn = prepare_subtitle(info, {'source': 'external', 'path': str(sub)}, tmp_path / 'snap', (320, 180))
    def hashes(start, n):
        raw = ffmpeg('-f', 'lavfi', '-i', f'color=s=320x180:r={fps}',
                     '-vf', output_filters((320, 180), (320, 180), burn, start),
                     '-frames:v', str(n), '-fps_mode', 'passthrough', '-f', 'framemd5', '-')
        return [line.split(',')[-1].strip() for line in raw.decode().splitlines() if not line.startswith('#')]
    full = hashes(0, 60)
    assert hashes(float(Fraction(24) / Fraction(fps)), 36) == full[24:]


@pytest.mark.parametrize('container', ['mp4', 'mkv'])
@pytest.mark.parametrize('kind', ['stream', 'segmented', 'chunked'])
def test_burn_keep_preserves_subtitles_and_audio(media, tmp_path, monkeypatch, container, kind):
    import sv.pipeline.segmented as segmented
    import sv.pipeline.chunked as chunked
    monkeypatch.setattr(segmented, 'TEMP_DIR', tmp_path)
    monkeypatch.setattr(chunked, 'TEMP_DIR', tmp_path)
    info, sub = media
    source = tmp_path / 'source.mkv'
    ffmpeg('-i', str(info.path), '-i', str(sub), '-f', 'lavfi', '-i', 'sine=sample_rate=48000',
           '-map', '0:v', '-map', '1:s', '-map', '2:a', '-t', '2', '-c:v', 'copy',
           '-c:s', 'srt', '-c:a', 'aac', str(source))
    info = probe(source)
    burn = prepare_subtitle(info, {'source': 'embedded', 'font_size': 96}, tmp_path / 'snapshot', (224, 126))
    enc = EncodeOpts(subtitle_mode='burn_keep', burn_subtitle=burn, container=container, preset='ultrafast')
    out = tmp_path / f'output.{container}'
    commands = []
    original_run = subprocess.run
    def recorded_run(cmd, *args, **kwargs):
        commands.append(cmd)
        return original_run(cmd, *args, **kwargs)
    if kind == 'stream':
        pipe = StreamPipeline(info, out, Nearest2x(), enc, target_size=(224, 126))
    elif kind == 'segmented':
        monkeypatch.setattr(subprocess, 'run', recorded_run)
        pipe = SegmentedPipeline(info, out, Nearest2x(), enc, task_id='keep', seg_frames=24, target_size=(224, 126))
    else:
        pipe = ChunkedPipeline(info, out, Nearest2x(), enc, task_id='keep', chunk=12, target_size=(224, 126))
    asyncio.run(pipe.run())
    result = probe(out)
    assert result.subtitles == ['mov_text' if container == 'mp4' else 'subrip']
    assert (result.width, result.height) == (224, 126)
    assert np.count_nonzero(frame(out, 1.25) > 180) > 10
    # Compare actual encoded audio packets: subtitle processing must not transcode AAC.
    def audio_hash(path):
        return ffmpeg('-i', str(path), '-map', '0:a:0', '-c:a', 'copy', '-f', 'hash', '-hash', 'sha256', '-')
    assert audio_hash(source) == audio_hash(out)
    if kind == 'segmented':
        concat = next(c for c in commands if 'concat' in c)
        assert concat[concat.index('-c:v') + 1] == 'copy' and '-vf' not in concat


def test_semantic_matching_survives_track_reordering_and_rejects_ambiguity(media, tmp_path):
    info, sub = media
    english = tmp_path / 'en.srt'
    english.write_text(sub.read_text(encoding='utf-8').replace('你好', 'English'), encoding='utf-8')
    for order in [0, 1]:
        files = [sub, english] if order == 0 else [english, sub]
        source = tmp_path / f'episode{order}.mkv'
        ffmpeg('-i', str(info.path), '-i', str(files[0]), '-i', str(files[1]), '-map', '0:v',
               '-map', '1:s', '-map', '2:s', '-c', 'copy',
               f'-metadata:s:s:{order}', 'language=zho', f'-metadata:s:s:{order}', 'title=简体中文',
               f'-metadata:s:s:{1-order}', 'language=eng', f'-metadata:s:s:{1-order}', 'title=English', str(source))
        media_info = probe(source)
        options = {'source': 'embedded', 'selection': 'match', 'language': 'chi', 'title': '简体'}
        normalized = validate_options(options, media_info)
        assert normalized['stream'] == order
        burn = prepare_subtitle(media_info, normalized, tmp_path / f'snapshot{order}', (320, 180))
        assert '你好' in burn.path.read_text(encoding='utf-8')
        assert all(t['is_text'] and t['burn_supported'] for t in media_info.subtitle_tracks)
        with pytest.raises(ValueError, match='没有匹配'):
            validate_options(dict(options, language='ja'), media_info)
        media_info.subtitle_tracks.append(dict(media_info.subtitle_tracks[order], stream=2))
        with pytest.raises(ValueError, match='匹配到 2 条'):
            validate_options(options, media_info)
    with pytest.raises(ValueError, match='匹配语言或标题'):
        validate_options({'source': 'embedded', 'selection': 'match'}, info)


def test_missing_libass_fails_early_with_clear_message(media, tmp_path, monkeypatch):
    import sv.pipeline.subtitle as module
    info, sub = media
    monkeypatch.setattr(module, 'subtitle_capability', lambda: {'supported': False, 'error': '当前 FFmpeg 不支持 libass 字幕烧录'})
    with pytest.raises(ValueError, match='当前 FFmpeg 不支持 libass'):
        prepare_subtitle(info, {'source': 'external', 'path': str(sub)}, tmp_path / 'snapshot', (320, 180))
    assert not (tmp_path / 'snapshot').exists()


def test_capability_detection_checks_filters(monkeypatch, tmp_path):
    import sv.pipeline.subtitle as module
    exe = tmp_path / 'ffmpeg.exe'
    exe.write_bytes(b'fake')
    monkeypatch.setattr(module, 'ffmpeg_bin', lambda: str(exe))
    module._capability_cache.clear()
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a, 0, b' .. ass V->V\n .. subtitles V->V\n', b''))
    assert module.subtitle_capability()['supported']
    module._capability_cache.clear()
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a, 0, b' .. scale V->V\n', b''))
    assert not module.subtitle_capability()['supported']
    assert 'libass' in module.subtitle_capability()['error']
    module._capability_cache.clear()


def test_real_cancellation_and_resume_with_burned_subtitles(media, tmp_path, monkeypatch):
    import sv.pipeline.segmented as module
    from sv.pipeline.stream import TaskCanceled
    monkeypatch.setattr(module, 'TEMP_DIR', tmp_path)
    info, sub = media
    burn = prepare_subtitle(info, {'source': 'external', 'path': str(sub), 'font_size': 96}, tmp_path / 'snapshot', (320, 180))
    enc = EncodeOpts(subtitle_mode='burn', burn_subtitle=burn, preset='ultrafast')
    out = tmp_path / 'resume.mp4'
    cancel = asyncio.Event()
    class CancelDuringSecondSegment(Nearest2x):
        main_thread_only = True
        n = 0
        def process(self, frame):
            self.n += 1
            if self.n == 30:
                cancel.set()
            return super().process(frame)
    async def cancelled_run():
        pipe = SegmentedPipeline(info, out, CancelDuringSecondSegment(), enc, task_id='cancel',
                                 seg_frames=24, cancel_event=cancel, cleanup=False)
        with pytest.raises(TaskCanceled):
            await asyncio.wait_for(pipe.run(), timeout=15)
    asyncio.run(cancelled_run())
    work = tmp_path / 'segmented/cancel'
    assert json.loads((work / 'checkpoint.json').read_text())['done'] == [0]
    first_mtime = (work / 'seg_000000.mp4').stat().st_mtime_ns
    asyncio.run(SegmentedPipeline(info, out, Nearest2x(), enc, task_id='cancel', seg_frames=24, cleanup=False).run())
    assert (work / 'seg_000000.mp4').stat().st_mtime_ns == first_mtime
    assert probe(out).total_frames == info.total_frames
    assert np.count_nonzero(frame(out, 1.25) > 180) > 30


def test_srt_color_shadow_and_filter_order(media, tmp_path):
    from sv.pipeline.stream import encoder_cmd
    info, sub = media
    burn = prepare_subtitle(info, {'source': 'external', 'path': str(sub), 'font_color': '#123456', 'shadow': 3}, tmp_path / 'snapshot', (224, 126))
    text = burn.path.read_text(encoding='utf-8')
    assert '&H00563412' in text
    assert ',2,3,2,60,60,50,1' in text
    cmd = encoder_cmd(info.path, tmp_path / 'out.mp4', 320, 180, 224, 126, '24',
                      EncodeOpts(subtitle_mode='burn_keep', burn_subtitle=burn), True, [], ['subrip'])
    assert cmd.count('-vf') == 1
    vf = cmd[cmd.index('-vf') + 1]
    assert vf.index('scale=') < vf.index('ass=')
