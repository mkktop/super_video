import base64
import io
import json
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageOps

from sv.server.app import app
from sv.watermark_match import MatchSkipped, locate, make_template
from sv.watermark_repair import erase


def sample_page():
    im = Image.new('RGB', (300, 420), 'white')
    draw = ImageDraw.Draw(im)
    draw.text((215, 380), 'WM-TEST', fill=(175,)*3)
    draw.line((216, 402, 279, 402), fill=(205,)*3, width=2)
    return im


BOX = (207, 373, 290, 410)


@pytest.mark.parametrize('background', [255, 0])
def test_auto_fill_handles_white_and_black_samples(background):
    page = sample_page()
    if background == 0:
        page = ImageOps.invert(page)
    template = make_template(page, BOX, allow_dark=True)
    assert template.background == background
    match = locate(page, template, allow_dark=True)
    before = np.asarray(page).copy()
    assert erase(page, match.box, 'auto', template) == ('white' if background else 'black')
    x,y,r,b = match.box
    before[y:b,x:r] = background
    np.testing.assert_array_equal(np.asarray(page), before)


def test_auto_skips_artwork_but_glyph_repair_preserves_unmasked_lines_and_alpha():
    sample = sample_page()
    template = make_template(sample, BOX, allow_dark=True)
    match = locate(sample, template)
    page = sample.convert('RGBA')
    # A line through the otherwise-white template context is protected by auto mode.
    x,y,r,b = match.box
    ImageDraw.Draw(page).line((x,y+2,r-1,y+2), fill=(20,20,20,180), width=2)
    before = np.array(page)
    with pytest.raises(MatchSkipped):
        erase(page, match.box, 'auto', template)
    np.testing.assert_array_equal(np.array(page), before)
    assert erase(page, match.box, 'repair', template) == 'inpaint'
    after = np.array(page)
    np.testing.assert_array_equal(after[:,:,3], before[:,:,3])
    outside = np.ones(before.shape[:2],bool)
    outside[y:b,x:r] = False
    np.testing.assert_array_equal(after[outside],before[outside])
    assert np.any(after[y:b,x:r,:3] != before[y:b,x:r,:3])


@pytest.mark.parametrize('mode', ['L', 'LA', 'RGB', 'RGBA'])
def test_fixed_repair_preserves_pixels_outside_mask_and_alpha(mode):
    page = Image.new('RGB',(80,60),(80,110,140)).convert(mode)
    ImageDraw.Draw(page).rectangle((50,35,60,44), fill=255 if mode == 'L' else
                                  (255,90) if mode == 'LA' else
                                  (255,255,255,90) if mode == 'RGBA' else 'white')
    before = np.array(page)
    assert erase(page,(50,35,61,45),'repair') == 'inpaint'
    after = np.array(page)
    outside = np.ones((60,80),bool);outside[35:45,50:61]=False
    np.testing.assert_array_equal(after[outside],before[outside])
    if mode in ('LA','RGBA'):
        np.testing.assert_array_equal(after[:,:,-1],before[:,:,-1])


def test_preview_and_batch_use_identical_repair_and_report_skips(tmp_path):
    client = TestClient(app)
    page = Image.new('RGB',(80,60),(80,110,140))
    ImageDraw.Draw(page).rectangle((50,35,60,44),fill='white')
    src = tmp_path/'page.png';page.save(src)
    settings = {'mask':{'width':11,'height':10,'right':19,'bottom':15},'removal':'repair'}
    result = client.post('/api/watermark/preview',json={'path':str(src),**settings})
    assert result.status_code == 200 and result.json()['method'] == 'inpaint'
    expected = erase(page,(50,35,61,45),'repair')
    preview = Image.open(io.BytesIO(base64.b64decode(result.json()['processed'].split(',')[1])))
    np.testing.assert_array_equal(np.asarray(preview),np.asarray(page))
    start = client.post('/api/watermark/batch',json={'paths':[str(src)],**settings})
    assert start.status_code == 200
    for _ in range(300):
        job = client.get('/api/watermark/batch/'+start.json()['id']).json()
        if job['status'] != 'running':break
        time.sleep(.01)
    assert job['succeeded'] == 1 and job['failed'] == 0
    from pathlib import Path
    out = Path(job['output_dir'])
    with Image.open(out/'page.png') as actual:
        np.testing.assert_array_equal(np.asarray(actual),np.asarray(page))
    report = json.loads((out/'watermark-report.json').read_text(encoding='utf8'))
    assert report['removal'] == 'repair' and report['results'][0]['method'] == expected
    assert client.post('/api/watermark/preview',json={'path':str(src),'removal':'bad'}).status_code == 422


def test_auto_preview_returns_unmodified_image_when_region_is_not_a_margin(tmp_path):
    page = Image.new('RGB',(80,60),(80,110,140))
    src=tmp_path/'page.png';page.save(src)
    result=TestClient(app).post('/api/watermark/preview',json={
        'path':str(src),'mask':{'width':11,'height':10},'removal':'auto'}).json()
    assert result['detected'] is False and result['box'] is None
    assert result['original'] == result['processed']


def test_fixed_auto_clears_opaque_white_text_on_black_margin():
    page=Image.new('RGB',(100,80),'black')
    ImageDraw.Draw(page).text((62,56),'WM',fill='white')
    assert erase(page,(55,50,95,75),'auto') == 'black'
    assert not np.any(np.asarray(page))


def test_fixed_sample_mask_repairs_overlay_without_erasing_other_content_in_box(tmp_path):
    client=TestClient(app)
    clean=sample_page();ref=tmp_path/'sample.png';clean.save(ref)
    page=clean.copy()
    ImageDraw.Draw(page).line((BOX[0],BOX[1]+1,BOX[2]-1,BOX[1]+1),fill='black',width=2)
    src=tmp_path/'overlay.png';page.save(src)
    mask={'width':83,'height':37,'right':10,'bottom':10}
    result=client.post('/api/watermark/preview',json={
        'path':str(src),'mask':mask,'mode':'fixed','removal':'repair',
        'sample':{'path':str(ref),'mask':mask}})
    assert result.status_code == 200 and result.json()['method'] == 'inpaint'
    assert result.json()['box'] == list(BOX)
    after=Image.open(io.BytesIO(base64.b64decode(result.json()['processed'].split(',')[1])))
    np.testing.assert_array_equal(np.array(after)[BOX[1]+1],np.array(page)[BOX[1]+1])
