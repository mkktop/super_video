"""White fill must preserve artwork, handle real folder batches, and stop safely."""
import base64
import io
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from sv.server.app import app
from sv.server.routes import watermark as wm


@pytest.fixture
def client():
    return TestClient(app)


def fixture_image(path, size=(32, 24), mode="RGB"):
    path.parent.mkdir(parents=True, exist_ok=True)
    channels = 4 if mode == "RGBA" else 3
    data = np.random.default_rng(0).integers(0, 256, (size[1], size[0], channels), dtype=np.uint8)
    Image.fromarray(data).save(path)
    return data


def wait_done(client, job_id):
    for _ in range(200):
        response = client.get(f"/api/watermark/batch/{job_id}")
        assert response.status_code == 200
        job = response.json()
        if job["status"] != "running":
            return job
        time.sleep(.01)
    pytest.fail("batch did not finish")


def test_preview_and_batch_preserve_every_pixel_outside_mask(client, tmp_path):
    src = tmp_path / "源目录" / "chapter" / "001.png"
    original = fixture_image(src, mode="RGBA")
    mask = {"width": 7, "height": 5, "right": 2, "bottom": 3}
    preview = client.post("/api/watermark/preview", json={"path": str(src), "mask": mask})
    assert preview.status_code == 200
    preview = preview.json()
    assert preview["box"] == [23, 16, 30, 21]
    p = np.array(Image.open(io.BytesIO(base64.b64decode(preview["processed"].split(",")[1]))))
    expected = original.copy()
    expected[16:21, 23:30] = 255
    np.testing.assert_array_equal(p, expected)
    body = {"paths": [str(src)], "folder": str(src.parent.parent), "mask": mask}
    response = client.post("/api/watermark/batch", json=body)
    assert response.status_code == 200
    job = wait_done(client, response.json()["id"])
    assert (job["succeeded"], job["failed"]) == (1, 0)
    output = Path(job["output_dir"])
    np.testing.assert_array_equal(np.array(Image.open(output / "chapter" / "001.png")), expected)
    np.testing.assert_array_equal(np.array(Image.open(src)), original)
    # Repeating creates a separate result directory rather than overwriting.
    repeat = client.post("/api/watermark/batch", json=body).json()
    assert repeat["output_dir"] != job["output_dir"]
    wait_done(client, repeat["id"])


def test_percentage_geometry_and_invalid_inputs(client, tmp_path):
    assert wm.WhiteMask(unit="percent", width=10, height=20, right=5, bottom=10).box((200, 100)) == (170, 70, 190, 90)
    # Unit conversion floating point noise must not expand a region by one pixel.
    assert wm.WhiteMask(unit="percent", width=175 / 11, height=75 / 16.45).box((1100, 1645)) == (925, 1570, 1100, 1645)
    src = tmp_path / "small.png"
    fixture_image(src)
    assert client.post("/api/watermark/preview", json={"path": str(src)}).status_code == 400
    for mask in ({"width": 0}, {"right": -1}, {"unit": "invalid"}):
        assert client.post("/api/watermark/preview", json={"path": str(src), "mask": mask}).status_code == 422
    assert client.post("/api/watermark/batch", json={"paths": [str(src)], "folder": str(tmp_path / "other")}).status_code == 400
    assert client.post("/api/watermark/batch", json={"paths": []}).status_code == 422


def test_batch_skips_corrupt_or_out_of_bounds_and_avoids_same_stems(client, tmp_path):
    a, b, small, corrupt = [tmp_path / name for name in ("same.png", "same.jpg", "small.png", "broken.jpg")]
    fixture_image(a)
    fixture_image(b)
    fixture_image(small, size=(2, 2))
    corrupt.write_bytes(b"invalid image")
    response = client.post("/api/watermark/batch", json={"paths": list(map(str, [a, b, small, corrupt])),
        "mask": {"width": 7, "height": 5}})
    assert response.status_code == 200
    job = wait_done(client, response.json()["id"])
    assert (job["completed"], job["succeeded"], job["failed"]) == (4, 2, 2)
    output = Path(job["output_dir"])
    assert {p.name for p in output.iterdir()} == {"same.png", "same_1.png"}
    assert len(job["errors"]) == 2


def test_cancel_finishes_current_image_and_rejects_concurrent_batch(client, tmp_path, monkeypatch):
    paths = [tmp_path / f"{i}.png" for i in range(3)]
    for path in paths:
        fixture_image(path)
    entered, release = threading.Event(), threading.Event()
    original_load = wm.load_image

    def slow_load(path):
        entered.set()
        assert release.wait(5)
        return original_load(path)

    monkeypatch.setattr(wm, "load_image", slow_load)
    body = {"paths": list(map(str, paths)), "mask": {"width": 7, "height": 5}}
    response = client.post("/api/watermark/batch", json=body)
    assert response.status_code == 200
    job_id = response.json()["id"]
    try:
        assert entered.wait(2)
        assert client.post("/api/watermark/batch", json=body).status_code == 409
        assert client.post(f"/api/watermark/batch/{job_id}/cancel").status_code == 200
    finally:
        release.set()
    job = wait_done(client, job_id)
    assert job["status"] == "cancelled"
    assert job["completed"] == job["succeeded"] == 1


def test_exif_orientation_and_grayscale(tmp_path):
    src = tmp_path / "oriented.jpg"
    im = Image.new("RGB", (32, 24))
    exif = Image.Exif()
    exif[274] = 6
    im.save(src, exif=exif)
    with wm.load_image(src) as loaded:
        assert loaded.size == (24, 32)
        assert loaded.getexif().get(274) is None
    gray = Image.new("L", (32, 24), 42)
    wm.fill_white(gray, wm.WhiteMask(width=7, height=5))
    assert gray.mode == "L"
    assert gray.getpixel((31, 23)) == 255
    assert gray.getpixel((24, 23)) == 42
