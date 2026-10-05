"""Detection must cope with scale/location changes and refuse uncertain erasure."""
import json
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from sv.server.app import app
from sv.server.routes.watermark import WhiteMask
from sv.watermark_match import MatchSkipped, locate, make_template, ncc


def logo():
    im = Image.new("RGB", (84, 32), "white")
    draw = ImageDraw.Draw(im)
    draw.text((4, 3), "WM-TEST", fill=(170, 170, 170))
    draw.line((5, 22, 76, 22), fill=(205, 205, 205), width=2)
    draw.rectangle((62, 8, 78, 16), outline=(185, 185, 185), width=2)
    return im


def page(size=(300, 420), scale=1., right=10, bottom=10):
    im = Image.new("RGB", size, "white")
    patch = logo()
    patch = patch.resize((round(patch.width * scale), round(patch.height * scale)), Image.Resampling.LANCZOS)
    x, y = size[0] - right - patch.width, size[1] - bottom - patch.height
    im.paste(patch, (x, y))
    return im


def template():
    src = page()
    return make_template(src, WhiteMask(width=84, height=32, right=10, bottom=10).box(src.size))


def test_ncc_matches_direct_calculation():
    rng = np.random.default_rng(12)
    image = rng.random((12, 15))
    patch = rng.random((4, 5))
    scores = ncc(image, patch)
    centered = patch - patch.mean()
    for y, x in [(0, 0), (5, 4), (8, 10)]:
        cut = image[y:y + 4, x:x + 5]
        expected = ((cut - cut.mean()) * centered).sum() / np.sqrt(((cut - cut.mean()) ** 2).sum() * (centered ** 2).sum())
        assert scores[y, x] == pytest.approx(expected, abs=1e-10)
    assert np.all(ncc(np.ones((12, 15)), patch) == -1)


@pytest.mark.parametrize("size,scale,right,bottom", [
    ((300, 420), 1, 20, 32), ((450, 630), 1.5, 24, 22),
    ((600, 840), 1, 30, 18), ((240, 340), .75, 6, 11),
    ((1200, 1680), 4, 24, 35),
])
def test_locates_moved_or_scaled_watermark(size, scale, right, bottom):
    im = page(size, scale, right, bottom)
    match = locate(im, template())
    assert match.score >= .88
    # Erasure must contain all actual watermark pixels, including resampling fringe.
    gray = np.asarray(im.convert("L"))
    ys, xs = np.where(gray < 245)
    x, y, r, b = match.box
    assert x <= xs.min() and y <= ys.min()
    assert r > xs.max() and b > ys.max()


def test_blank_page_and_ambiguous_duplicates_are_not_erased():
    with pytest.raises(MatchSkipped):
        locate(Image.new("RGB", (300, 420), "white"), template())
    im = page()
    im.paste(logo(), (206, 310))
    with pytest.raises(MatchSkipped, match="多个"):
        locate(im, template())


def test_artwork_next_to_watermark_is_skipped():
    im = page()
    detected = locate(im, template())
    x, y, r, b = detected.box
    ImageDraw.Draw(im).line((x - 2, y, x - 2, b), fill="black", width=2)
    with pytest.raises(MatchSkipped):
        locate(im, template())
    with pytest.raises(ValueError, match="特征"):
        make_template(Image.new("RGB", (300, 420), "white"), (200, 370, 290, 410))
    unsafe = page()
    ImageDraw.Draw(unsafe).line((206, 379, 289, 379), fill="black", width=1)
    with pytest.raises(ValueError, match="漫画内容"):
        make_template(unsafe, WhiteMask(width=84, height=32, right=10, bottom=10).box(unsafe.size))


def test_api_preview_batch_skips_and_preserves_outside_pixels(tmp_path):
    client = TestClient(app)
    folder = tmp_path / "manga"
    (folder / "vol" / "chapter").mkdir(parents=True)
    ref = folder / "001.png"
    changed = folder / "vol" / "chapter" / "002.png"
    blank = folder / "vol" / "chapter" / "003.png"
    page().save(ref)
    page((450, 630), 1.5, 22, 20).save(changed)
    Image.new("RGB", (300, 420), "white").save(blank)
    sample = {"path": str(ref), "mask": {"width": 84, "height": 32, "right": 10, "bottom": 10}}
    settings = {"mode": "smart", "sample": sample}
    preview = client.post("/api/watermark/preview", json={"path": str(changed), **settings})
    assert preview.status_code == 200
    assert preview.json()["detected"] is True
    empty = client.post("/api/watermark/preview", json={"path": str(blank), **settings}).json()
    assert empty["detected"] is False and empty["box"] is None
    assert empty["original"] == empty["processed"]
    response = client.post("/api/watermark/batch", json={"paths": list(map(str, [ref, changed, blank])), "folder": str(folder), **settings})
    assert response.status_code == 200
    job_id = response.json()["id"]
    for _ in range(300):
        job = client.get(f"/api/watermark/batch/{job_id}").json()
        if job["status"] != "running":
            break
        time.sleep(.01)
    assert (job["completed"], job["succeeded"], job["skipped"], job["failed"]) == (3, 2, 1, 0)
    out = Path(job["output_dir"])
    assert not (out / "vol/chapter/003.png").exists()
    report = json.loads((out / "watermark-report.json").read_text(encoding="utf-8"))
    for item in report["results"]:
        if item["status"] != "done":
            continue
        with Image.open(item["path"]) as im: original = np.asarray(im).copy()
        x, y, r, b = item["box"]
        original[y:b, x:r] = 255
        with Image.open(item["output"]) as im: processed = np.asarray(im)
        np.testing.assert_array_equal(original, processed)
    assert client.post("/api/watermark/batch", json={"paths": [str(ref)], "mode": "smart"}).status_code == 400
    assert client.post("/api/watermark/batch", json={"paths": [str(ref)], "mode": "other"}).status_code == 422
