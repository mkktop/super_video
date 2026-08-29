"""批量图片合并输出 PDF：writer 无损性 + 创建端点参数 + worker 端到端。

PDF 校验不依赖第三方阅读器：测试内按 PDF 规格独立实现解码侧——解析 xref
偏移、抽取图像流、Flate 解压 + PNG 预测器还原，再与源像素逐字节比对。
JPEG 直嵌则比对嵌入流与源文件字节是否完全一致。
"""
import os
import re
import zlib
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from sv.paths import TEMP_DIR
from sv.pdfmerge import write_pdf

os.environ.setdefault("SV_DB", str(TEMP_DIR / "test_pdfmerge.db"))

SPEC = SimpleNamespace(
    id="imgsr-fake", engine="onnx", scale=[2],
    fp16=False, tile_hint=0,
    io={"color": "rgb", "range": "0-255", "pad": 2},
)


# ---- 测试内独立实现的 PDF 解码侧 ----

def _pdf_objects(raw: bytes) -> dict[int, bytes]:
    objs: dict[int, bytes] = {}
    for m in re.finditer(rb"(\d+) 0 obj\n", raw):
        objs[int(m.group(1))] = raw[m.end():raw.index(b"endobj", m.end())]
    return objs


def _xref_points_ok(raw: bytes) -> bool:
    """xref 每条 in-use 记录必须精确落在 "N 0 obj" 上（规格合规性）"""
    tail = raw[raw.rindex(b"startxref") + len(b"startxref\n"):]
    xref_at = int(tail.split(b"%%EOF", 1)[0])
    assert raw[xref_at:xref_at + 5] == b"xref\n"
    lines = raw[xref_at:].split(b"\n")
    size = int(lines[1].split()[1])
    assert lines[2] == b"0000000000 65535 f "
    n_used = 0
    for i in range(1, size):
        entry = lines[2 + i]
        off = int(entry[:10])
        if entry.rstrip() == b"0000000000 65535 f" or entry[11:16] == b"65535":
            continue
        assert raw[off:].startswith(f"{i} 0 obj".encode()), \
            f"xref 对象 {i} 偏移 {off} 不指向对象头"
        n_used += 1
    return n_used == size - 1


def _image_pages(raw: bytes) -> list[dict]:
    """抽取全部图像 XObject：{w,h,filter,stream}（页序=对象号升序）"""
    pages = []
    for oid, body in sorted(_pdf_objects(raw).items()):
        if b"/Subtype /Image" not in body:
            continue
        w = int(re.search(rb"/Width (\d+)", body).group(1))
        h = int(re.search(rb"/Height (\d+)", body).group(1))
        filt = re.search(rb"/Filter /(\w+Decode)", body).group(1).decode()
        s = body.index(b"stream\n") + len(b"stream\n")
        e = body.index(b"\nendstream", s)
        pages.append({"w": w, "h": h, "filter": filt, "stream": body[s:e],
                      "parms": re.search(rb"/DecodeParms[^>]*>>", body).group(0)
                      if b"/DecodeParms" in body else b""})
    return pages


def _unpredict(raw: bytes, w: int, h: int, c: int = 3) -> np.ndarray:
    """PNG 行预测器还原（0-3）：逐字节串行的朴素实现——Sub/Avg 重建依赖
    当前行已重建的左侧字节，必须串行；测试特意不复用 writer 的任何代码"""
    buf = memoryview(raw)
    stride = w * c
    out = np.empty((h, stride), np.uint8)
    prev = bytes(stride)
    for y in range(h):
        off = y * (1 + stride)
        ft = buf[off]
        row = buf[off + 1: off + 1 + stride]
        cur = bytearray(stride)
        for x in range(stride):
            a = cur[x - c] if x >= c else 0
            b = prev[x]
            if ft == 1:
                v = row[x] + a
            elif ft == 2:
                v = row[x] + b
            elif ft == 3:
                v = row[x] + (a + b) // 2
            else:
                v = row[x]
            cur[x] = v & 0xFF
        out[y] = np.frombuffer(bytes(cur), np.uint8)
        prev = cur
    return out.reshape(h, w, c)


def _mediaboxes(raw: bytes) -> list[tuple[float, float]]:
    return [tuple(float(x) for x in m)
            for m in re.findall(rb"/MediaBox \[0 0 ([\d.]+) ([\d.]+)\]", raw)]


def _make_arr(w: int, h: int, kind: str) -> np.ndarray:
    if kind == "gradient":  # 平滑渐变：逼出 Sub/Avg 过滤器
        y, x = np.mgrid[0:h, 0:w]
        return np.stack([x * 255 // max(w - 1, 1), y * 255 // max(h - 1, 1),
                         (x + y) * 255 // max(w + h - 2, 1)], axis=-1).astype(np.uint8)
    return np.zeros((h, w, 3), np.uint8)  # 大片平涂：逼出 None/Up


# ---- writer：无损与规格 ----

def test_flate_roundtrip_lossless_and_xref(tmp_path):
    pngs = []
    for i, kind in enumerate(("gradient", "flat")):
        p = tmp_path / f"p{i}.png"
        Image.fromarray(_make_arr(37, 23, kind)).save(str(p))
        pngs.append(p)
    pdf = tmp_path / "out.pdf"
    st = write_pdf(pngs, pdf)
    assert st["pages"] == 2 and st["bytes"] == pdf.stat().st_size

    raw = pdf.read_bytes()
    assert raw.startswith(b"%PDF-1.4") and raw.rstrip().endswith(b"%%EOF")
    assert _xref_points_ok(raw)
    pages = _image_pages(raw)
    assert len(pages) == 2
    assert re.search(rb"/Type /Pages /Count 2 ", raw)
    for pg, src in zip(pages, pngs):
        assert pg["filter"] == "FlateDecode" and b"/Predictor 15" in pg["parms"]
        with Image.open(src) as im:
            want = np.asarray(im.convert("RGB"), dtype=np.uint8)
        assert (pg["w"], pg["h"]) == (want.shape[1], want.shape[0])
        got = _unpredict(zlib.decompress(pg["stream"]),
                         want.shape[1], want.shape[0])
        assert np.array_equal(got, want), "Flate 页必须逐像素无损"


def test_jpeg_passthrough_bytes_identical(tmp_path):
    src = tmp_path / "j.jpg"
    Image.fromarray(_make_arr(40, 30, "gradient")).save(str(src), quality=87)
    pdf = tmp_path / "j.pdf"
    write_pdf([src], pdf)
    pages = _image_pages(pdf.read_bytes())
    assert len(pages) == 1 and pages[0]["filter"] == "DCTDecode"
    assert pages[0]["stream"] == src.read_bytes(), "JPEG 必须原样直嵌不重压"


def test_page_over_pdf_limit_scaled_not_cropped(tmp_path):
    """超 14400pt 的超宽页：页面等比缩小，图像流仍逐像素无损"""
    src = tmp_path / "wide.png"
    Image.fromarray(_make_arr(15000, 4, "gradient")).save(str(src))
    pdf = tmp_path / "wide.pdf"
    write_pdf([src], pdf)
    raw = pdf.read_bytes()
    (w, h), = _mediaboxes(raw)
    assert w <= 14400 and h <= 14400 and abs(w / h - 15000 / 4) < 0.01
    pg, = _image_pages(raw)
    assert (pg["w"], pg["h"]) == (15000, 4), "图像流尺寸不受页面缩放影响"
    got = _unpredict(zlib.decompress(pg["stream"]), 15000, 4)
    with Image.open(src) as im:
        assert np.array_equal(got, np.asarray(im.convert("RGB"), dtype=np.uint8))


# ---- 创建端点 ----

@pytest.fixture(scope="module")
def client():
    from sv.server.app import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def model_x2(client):
    for m in client.get("/api/models").json():
        if m.get("kind") != "interp" and 2 in (m.get("scale") or []):
            return m["id"]
    pytest.fail("无支持 x2 的模型")


@pytest.fixture(autouse=True)
def _tmp_settings(tmp_path, monkeypatch):
    from sv.server import settings as _settings

    monkeypatch.setattr(_settings, "SETTINGS_PATH", tmp_path / "s.json")


def test_create_batch_merge_pdf_params(client, model_x2, tmp_path):
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    for p in (a, b):
        Image.fromarray(_make_arr(12, 10, "flat")).save(str(p))
    r = client.post("/api/tasks", json={
        "inputs": [str(a), str(b)], "model_id": model_x2,
        "params": {"kind": "image", "scale": 2, "merge_pdf": True},
    })
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["output_path"].endswith(".pdf"), "任务主输出应指向 PDF"
    assert d["params"]["merge_pdf"] is True
    assert d["params"]["pdf_out"] == d["output_path"]
    assert len(d["params"]["images"]) == 2, "逐图 PNG 产物照常保留"


# ---- worker 端到端（假引擎 2x 最近邻）----

@pytest.fixture()
def fake_engine(monkeypatch):
    class _Nearest2x:
        scale = 2

        def load(self):
            pass

        def process(self, frame):
            return np.repeat(np.repeat(frame, 2, axis=0), 2, axis=1)

    eng = _Nearest2x()
    monkeypatch.setattr(
        "sv.server.worker._load_onnx_engine",
        lambda *a, **k: (eng, "fp32"))
    monkeypatch.setattr("sv.server.worker.model_file",
                        lambda *a, **k: Path("fake.onnx"))
    monkeypatch.setattr("sv.models.registry.file_for_scale",
                        lambda spec, scale, variant=None: Path("fake.onnx"))
    monkeypatch.setattr("sv.models.manager.ensure_files", lambda spec, needs: None)
    events: list[dict] = []
    monkeypatch.setattr("sv.server.worker.emit", events.append)
    eng.events = events
    return eng


def test_worker_job_merges_pdf(fake_engine, tmp_path):
    from sv.server.worker import _run_image_job

    srcs, images = [], []
    for i in range(3):
        p = tmp_path / f"in{i}.png"
        Image.fromarray(_make_arr(16 + i, 12, "gradient")).save(str(p))
        srcs.append(p)
        images.append({"in": str(p), "out": str(tmp_path / f"out{i}_2x.png")})
    pdf_out = tmp_path / "merged.pdf"
    task = {"id": "pdfjob1", "input_path": str(srcs[0]),
            "output_path": str(pdf_out)}
    rc = _run_image_job(
        task, {"kind": "image", "format": "png", "scale": 2, "target_scale": 2,
               "images": images, "merge_pdf": True, "pdf_out": str(pdf_out)}, SPEC)
    assert rc == 0
    assert pdf_out.exists()

    raw = pdf_out.read_bytes()
    assert _xref_points_ok(raw)
    pages = _image_pages(raw)
    assert len(pages) == 3, "页数=成功图片数，顺序=处理顺序"
    for i, pg in enumerate(pages):
        with Image.open(srcs[i]) as im:
            want = np.asarray(im.convert("RGB"), dtype=np.uint8)
        want = np.repeat(np.repeat(want, 2, axis=0), 2, axis=1)  # 2x 最近邻
        got = _unpredict(zlib.decompress(pg["stream"]),
                         want.shape[1], want.shape[0])
        assert np.array_equal(got, want), f"第 {i} 页与超分产物逐像素不一致"
    done = [e for e in fake_engine.events if e.get("type") == "done"][-1]
    assert done["out_bytes"] >= pdf_out.stat().st_size, "PDF 体积应计入产出"
    assert any("已无损合并 3 页" in e.get("line", "")
               for e in fake_engine.events if e.get("type") == "log")


def test_worker_pdf_failure_fails_task(fake_engine, tmp_path, monkeypatch):
    """PDF 合并异常必须显式失败（图片产物保留），不得静默吞掉"""
    from sv.server import worker as worker_mod

    src = tmp_path / "one.png"
    Image.fromarray(_make_arr(10, 8, "flat")).save(str(src))
    out = tmp_path / "one_2x.png"
    pdf_out = tmp_path / "one.pdf"
    task = {"id": "pdfjob2", "input_path": str(src), "output_path": str(out)}
    monkeypatch.setattr("sv.pdfmerge.write_pdf",
                        lambda paths, out: (_ for _ in ()).throw(OSError("disk full")))
    rc = worker_mod._run_image_job(
        task, {"kind": "image", "format": "png", "scale": 2, "target_scale": 2,
               "images": [{"in": str(src), "out": str(out)}],
               "merge_pdf": True, "pdf_out": str(pdf_out)}, SPEC)
    assert rc == 1
    assert out.exists(), "图片产物不受合并失败牵连"
    failed = [e for e in fake_engine.events if e.get("type") == "failed"][-1]
    assert "PDF 合并失败" in failed["error"]
