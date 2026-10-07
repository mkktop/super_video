"""Real FFmpeg watermark timing and coexistence with subtitle rendering."""
import asyncio
from pathlib import Path

import numpy as np
import pytest
from fastapi import HTTPException

from test_subtitle_burn import ffmpeg, frame, media, Nearest2x
from sv.pipeline.probe import probe
from sv.pipeline.subtitle import prepare_subtitle
from sv.pipeline.watermark import prepare_watermark, check_watermark_resume, validate_watermark
from sv.pipeline.stream import EncodeOpts, StreamPipeline
from sv.pipeline.segmented import SegmentedPipeline
from sv.pipeline.chunked import ChunkedPipeline


def options(tmp_path, kind):
    result = dict(kind=kind, text='雨帧 Logo', start_s=.5, duration_s=.75,
                  position='top-right', font_size=100, margin=40, opacity=1, width_pct=25)
    if kind == 'image':
        logo = tmp_path / "Logo 中文, [a] ' .png"
        pixels = np.zeros((24, 48, 4), np.uint8)
        pixels[4:20, 4:44] = [255, 255, 255, 255]
        ffmpeg('-f', 'rawvideo', '-pix_fmt', 'rgba', '-s', '48x24', '-i', '-',
               '-frames:v', '1', str(logo), input=pixels.tobytes())
        result['path'] = str(logo)
    return result


@pytest.mark.parametrize('kind', ['text', 'image'])
@pytest.mark.parametrize('pipeline', ['stream', 'segmented', 'chunked'])
def test_timed_watermark_with_subtitles(media, tmp_path, monkeypatch, kind, pipeline):
    import sv.pipeline.segmented as segmented
    import sv.pipeline.chunked as chunked
    monkeypatch.setattr(segmented, 'TEMP_DIR', tmp_path)
    monkeypatch.setattr(chunked, 'TEMP_DIR', tmp_path)
    info, sub = media
    size = (320, 180)
    watermark = prepare_watermark(options(tmp_path, kind), tmp_path / "快照, [b] ' ", size)
    burn = prepare_subtitle(info, dict(source='external', path=str(sub), font_size=96), tmp_path / 'sub', size)
    enc = EncodeOpts(watermark=watermark, burn_subtitle=burn, subtitle_mode='burn', preset='ultrafast', crf=10)
    output = tmp_path / 'output.mp4'
    if pipeline == 'stream':
        pipe = StreamPipeline(info, output, Nearest2x(), enc)
    elif pipeline == 'segmented':
        pipe = SegmentedPipeline(info, output, Nearest2x(), enc, task_id='wm', seg_frames=12)
    else:
        pipe = ChunkedPipeline(info, output, Nearest2x(), enc, task_id='wm', chunk=12)
    asyncio.run(pipe.run())
    assert probe(output).total_frames == info.total_frames
    before, during, later_segment, after = [frame(output, t).reshape(180, 320, 3) for t in (.25, .75, 1.1, 1.75)]
    assert np.count_nonzero(before[:70] > 180) == 0
    assert np.count_nonzero(during[:70] > 180) > 30
    assert np.count_nonzero(later_segment[:70] > 180) > 30
    assert np.count_nonzero(after[:70] > 180) == 0
    assert np.count_nonzero(during[100:] > 180) > 30  # subtitles coexist at the bottom


def test_snapshot_and_segment_resume_reject_changed_watermark(tmp_path):
    o = options(tmp_path, 'image')
    directory = tmp_path / 'snapshot'
    first = prepare_watermark(o, directory, (320, 180))
    assert prepare_watermark(o, directory, (320, 180)) == first
    work = tmp_path / 'segments'
    work.mkdir()
    check_watermark_resume(work, first)
    with pytest.raises(ValueError, match='水印'):
        check_watermark_resume(work, None)
    with pytest.raises(ValueError, match='改变'):
        prepare_watermark(dict(o, duration_s=1), directory, (320, 180))
    Path(o['path']).write_bytes(b'changed')
    with pytest.raises(ValueError, match='改变'):
        prepare_watermark(o, directory, (320, 180))


def test_preview_watermark_alone_and_after_interval(media, tmp_path, monkeypatch):
    from sv.server.routes import models
    monkeypatch.setattr(models, 'TEMP_DIR', tmp_path)
    info, _ = media
    o = options(tmp_path, 'text')
    for t, visible in [(.75, True), (1.75, False)]:
        response = models.subtitle_preview(models.SubtitlePreviewBody(
            input=str(info.path), watermark=o, width=320, height=180, time_s=t))
        image = tmp_path / 'preview.png'
        image.write_bytes(response.body)
        pixels = frame(image, 0).reshape(180, 320, 3)
        assert (np.count_nonzero(pixels[:70] > 180) > 30) == visible
    assert not list(tmp_path.glob('subtitle-preview-*'))


def test_default_watermark_preview_matches_explicit_top_right(media, tmp_path, monkeypatch):
    from sv.server.routes import models
    monkeypatch.setattr(models, 'TEMP_DIR', tmp_path)
    info, _ = media
    images = []
    for position in ({}, {'position': 'top-right'}):
        response = models.subtitle_preview(models.SubtitlePreviewBody(
            input=str(info.path), watermark=dict(kind='text', text='雨帧 Logo', **position),
            width=640, height=360))
        image = tmp_path / 'default.png'
        image.write_bytes(response.body)
        pixels = frame(image, 0).reshape(360, 640, 3)
        assert np.count_nonzero(pixels[:100, 320:] > 180) > 30
        images.append(pixels)
    assert np.array_equal(*images)


def test_api_presets_and_invalid_watermarks(media, tmp_path, monkeypatch):
    from sv.server import db, user_presets
    from sv.server.routes.tasks import create_task, TaskCreate
    from sv.server.routes.models import create_preset, PresetCreate
    monkeypatch.setenv('SV_DB', str(tmp_path / 'api.db'))
    monkeypatch.setattr(user_presets, 'PRESETS_PATH', tmp_path / 'presets.json')
    db.init_db()
    info, _ = media
    o = options(tmp_path, 'text')
    task = create_task(TaskCreate(input=str(info.path), model_id='realesr-animevideov3', params={'watermark': o}))
    assert task['params']['watermark']['duration_s'] == .75
    preset = create_preset(PresetCreate(name='片头水印', model_id='realesr-animevideov3', target_scale=2, watermark=o))
    assert preset['watermark']['text'] == o['text']
    db.delete_task(task['id'])
    with pytest.raises(HTTPException) as exc:
        create_task(TaskCreate(input=str(info.path), model_id='realesr-animevideov3', params={'watermark': o, 'out_kind': 'png'}))
    assert exc.value.status_code == 400
    for bad in [dict(kind='text', text=''), dict(kind='image', path='missing.png'), dict(o, duration_s=0), dict(o, opacity=float('nan'))]:
        with pytest.raises(ValueError):
            validate_watermark(bad)


def test_multiline_text_renders_on_separate_rows(media, tmp_path, monkeypatch):
    from sv.server.routes import models
    monkeypatch.setattr(models, 'TEMP_DIR', tmp_path)
    info, _ = media
    bottom_rows = []
    for text in ('雨帧制作', '雨帧制作\n超分修复'):
        response = models.subtitle_preview(models.SubtitlePreviewBody(
            input=str(info.path), watermark=dict(options(tmp_path, 'text'), text=text),
            width=320, height=180, time_s=.75))
        image = tmp_path / 'multiline.png'
        image.write_bytes(response.body)
        pixels = frame(image, 0).reshape(180, 320, 3)
        rows = np.where(np.any(pixels > 180, axis=(1, 2)))[0]
        assert len(rows) > 0
        bottom_rows.append(rows[-1])
    assert bottom_rows[1] > bottom_rows[0] + 10
