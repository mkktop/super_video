"""图片超分：创建端点分支（格式/命名/分辨率纪律）与 worker 单帧作业（含 EXIF 转正）。

推理用假引擎（2x 最近邻），不依赖 GPU/真实模型——覆盖的是胶水层：
参数解析、输出落盘、预览缩略图与事件上报。
"""
import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from sv.paths import TEMP_DIR

os.environ.setdefault("SV_DB", str(TEMP_DIR / "test_imgsr.db"))

SPEC = SimpleNamespace(
    id="imgsr-fake", engine="onnx", scale=[2],
    fp16=False, tile_hint=0,
    io={"color": "rgb", "range": "0-255", "pad": 2},
)


class _Nearest2x:
    scale = 2

    def __init__(self):
        self.seen_hw = None

    def load(self):
        pass

    def process(self, frame):
        self.seen_hw = frame.shape[:2]
        return np.repeat(np.repeat(frame, 2, axis=0), 2, axis=1)


@pytest.fixture()
def fake_engine(monkeypatch):
    eng = _Nearest2x()

    def _fake_load(weight, spec, scale, variant, precision, tile, warmup_hw, *, batch=1, log=None):
        return eng, "fp32"

    import sv.server.worker as worker_mod
    import sv.server.worker_image as worker_image_mod

    monkeypatch.setattr(worker_image_mod, "_load_onnx_engine", _fake_load)
    monkeypatch.setattr(worker_image_mod, "model_file", lambda *a, **k: Path("fake.onnx"))
    # 模块内局部导入：直接改注册表源
    import sv.models.registry as reg
    import sv.models.manager as mgr

    monkeypatch.setattr(reg, "file_for_scale", lambda spec, scale, variant=None: Path("fake.onnx"))
    monkeypatch.setattr(mgr, "ensure_files", lambda spec, needs: None)
    events: list[dict] = []
    monkeypatch.setattr(worker_image_mod, "emit", events.append)
    eng.events = events
    return eng


def _make_png(path: Path, w: int, h: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    arr = np.arange(w * h * 3, dtype=np.uint8).reshape(h, w, 3)
    Image.fromarray(arr).save(str(path))


# ---- worker 作业 ----

def test_image_job_scales_and_writes_png(tmp_path, fake_engine):
    src = tmp_path / "in" / "photo.png"
    _make_png(src, 32, 20)
    out = tmp_path / "out" / "photo_2x.png"
    task = {"id": "imgt1", "input_path": str(src), "output_path": str(out)}
    rc = fake_engine.__module__ and __import__("sv.server.worker", fromlist=["x"])._run_image_job(
        task, {"kind": "image", "format": "png", "scale": 2, "target_scale": 2}, SPEC)
    assert rc == 0
    assert out.exists(), "未写出输出文件"
    with Image.open(out) as im:
        assert im.size == (64, 40)
        assert im.format == "PNG"
    assert fake_engine.seen_hw == (20, 32), "送入引擎的应是 RGB 原图"
    types = [e.get("type") for e in fake_engine.events]
    assert types[-1] == "done"
    done = fake_engine.events[-1]
    assert done["frames"] == 1 and done["out_bytes"] > 0
    assert Path(done["preview"]).exists() and Path(done["src_preview"]).exists()


def test_image_job_exif_orientation_transposed(tmp_path, fake_engine):
    src = tmp_path / "rotated.jpg"
    # 宽>高的横存图 + Orientation=6（需顺时针转 90°）→ 解码后应为竖图
    arr = np.zeros((10, 30, 3), dtype=np.uint8)
    im = Image.fromarray(arr)
    ex = im.getexif()
    ex[274] = 6
    im.save(str(src), exif=ex)
    out = tmp_path / "rotated_2x.png"
    task = {"id": "imgt2", "input_path": str(src), "output_path": str(out)}
    from sv.server.worker import _run_image_job

    assert _run_image_job(task, {"kind": "image", "format": "png", "scale": 2}, SPEC) == 0
    assert fake_engine.seen_hw == (30, 10), "EXIF 方向应已转正后送入引擎"


def test_image_job_jpg_format(tmp_path, fake_engine):
    src = tmp_path / "p.png"
    _make_png(src, 8, 8)
    out = tmp_path / "p_2x.jpg"
    task = {"id": "imgt3", "input_path": str(src), "output_path": str(out)}
    from sv.server.worker import _run_image_job

    rc = _run_image_job(task, {
        "kind": "image", "format": "jpg", "jpg_quality": 200,
        "scale": 2, "target_scale": 2,
    }, SPEC)
    assert rc == 0
    with Image.open(out) as im:
        assert im.format == "JPEG"
    q = [e for e in fake_engine.events if e.get("type") == "done"]
    assert q and q[0]["out_bytes"] > 0


def test_image_job_bad_format_fails(tmp_path, fake_engine):
    src = tmp_path / "b.png"
    _make_png(src, 4, 4)
    task = {"id": "imgt4", "input_path": str(src),
            "output_path": str(tmp_path / "b_2x.webp")}
    from sv.server.worker import _run_image_job

    assert _run_image_job(task, {"kind": "image", "format": "webp", "scale": 2}, SPEC) == 1


def test_image_job_batch_loop(tmp_path, fake_engine):
    """批量=一个任务：引擎加载一次、逐图落盘、坏图跳过不拖垮整批。"""
    images = []
    expect_ok = []
    for i, name in enumerate(["good0.png", "broken.png", "good1.png", "good2.png"]):
        src = tmp_path / name
        out = tmp_path / f"{name}_2x.png"
        if name.startswith("good"):
            _make_png(src, 10 + i, 8)
            expect_ok.append(out)
        else:
            src.write_bytes(b"not an image")
        images.append({"in": str(src), "out": str(out)})
    task = {"id": "imgt5", "input_path": images[0]["in"], "output_path": images[0]["out"]}
    from sv.server.worker import _run_image_job

    rc = _run_image_job(task, {
        "kind": "image", "format": "png", "scale": 2, "target_scale": 2,
        "images": images,
    }, SPEC)
    assert rc == 0
    for o in expect_ok:
        assert o.exists(), f"{o.name} 应完成"
    assert not (tmp_path / "broken.png_2x.png").exists(), "坏图不应产出"
    done = [e for e in fake_engine.events if e.get("type") == "done"][0]
    assert done["frames"] == 3, "完成数应为 3（跳过坏图）"
    progs = [e for e in fake_engine.events if e.get("type") == "progress"]
    assert len(progs) == 4 and progs[-1]["frames"] == 4, "进度按处理位数走满 total"
    skipped = [e for e in fake_engine.events
               if e.get("type") == "log" and "跳过" in e.get("line", "")]
    assert skipped, "坏图应有跳过日志"


def test_image_job_all_bad_fails(tmp_path, fake_engine):
    (tmp_path / "bad.png").write_bytes(b"not an image")
    task = {"id": "imgt6", "input_path": str(tmp_path / "bad.png"),
            "output_path": str(tmp_path / "bad_2x.png")}
    from sv.server.worker import _run_image_job

    rc = _run_image_job(task, {
        "kind": "image", "format": "png", "scale": 2,
        "images": [{"in": str(tmp_path / "bad.png"),
                    "out": str(tmp_path / "bad_2x.png")}],
    }, SPEC)
    assert rc == 1


# 高度自适应权重档（MangaJaNai 系语义）：io.auto_variant=height + NNNNp 变体
TIER_SPEC = SimpleNamespace(
    id="tier-fake", engine="onnx", scale=[2], fp16=False, tile_hint=0,
    io={"color": "rgb", "range": "0-1", "pad": 1, "auto_variant": "height"},
    files=[{"name": f"{v}.onnx", "scale": 2, "variant": v}
           for v in ("1200p", "1300p")],
)


def _all_logs(events) -> list[str]:
    return [e.get("line", "") for e in events if e.get("type") == "log"]


def test_image_job_mixed_tier_batch_discloses(tmp_path, fake_engine):
    """批量分属多个理想档：统一按首图档处理，必须日志明示取舍（不静默近似）。"""
    images = []
    for name, h in (("tall.png", 1200), ("taller.png", 1260)):
        src = tmp_path / name
        _make_png(src, 40, h)
        images.append({"in": str(src), "out": str(tmp_path / f"{name}_2x.png")})
    task = {"id": "imgt7", "input_path": images[0]["in"],
            "output_path": images[0]["out"]}
    from sv.server.worker import _run_image_job

    rc = _run_image_job(task, {
        "kind": "image", "format": "png", "scale": 2, "images": images,
    }, TIER_SPEC)
    assert rc == 0
    for meta in images:
        assert Path(meta["out"]).exists()
    logs = _all_logs(fake_engine.events)
    # 1260p 的理想档是 1300p，与首图 1200p 不同 → 必须披露统一档与分批指引
    assert any("1200p/1300p" in l and "统一" in l and "分批" in l for l in logs), logs


def test_image_job_batch_same_tier_no_notice(tmp_path, fake_engine):
    """高度略差但同属一个理想档：不披露（1200 与 1250 都落 1200p，提示是噪音）。"""
    images = []
    for name, h in (("a.png", 1200), ("b.png", 1250)):
        src = tmp_path / name
        _make_png(src, 40, h)
        images.append({"in": str(src), "out": str(tmp_path / f"{name}_2x.png")})
    task = {"id": "imgt8", "input_path": images[0]["in"],
            "output_path": images[0]["out"]}
    from sv.server.worker import _run_image_job

    assert _run_image_job(task, {
        "kind": "image", "format": "png", "scale": 2, "images": images,
    }, TIER_SPEC) == 0
    logs = _all_logs(fake_engine.events)
    assert any(l.startswith("按源高度 1200p") for l in logs), logs
    assert not any("统一" in l for l in logs), logs


# ---- 创建端点 ----

@pytest.fixture(scope="module")
def client():
    from sv.server.app import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def model_x2(client):
    models = client.get("/api/models").json()
    for m in models:
        if m.get("kind") != "interp" and 2 in (m.get("scale") or []):
            return m["id"]
    pytest.fail("无支持 x2 的模型")


@pytest.fixture(autouse=True)
def _tmp_settings(tmp_path, monkeypatch):
    from sv.server import settings as _settings

    monkeypatch.setattr(_settings, "SETTINGS_PATH", tmp_path / "data" / "settings.json")


def test_create_image_task_default_naming(client, model_x2, tmp_path):
    src = tmp_path / "pic.png"
    _make_png(src, 16, 12)
    r = client.post("/api/tasks", json={
        "input": str(src), "model_id": model_x2,
        "params": {"kind": "image", "scale": 2},
    })
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["output_path"].endswith(f"{src.stem}_2x.png")
    assert d["params"]["kind"] == "image" and d["params"]["format"] == "png"
    assert (d["src_w"], d["src_h"], d["total_frames"]) == (16, 12, 1)


def test_create_image_task_jpg_and_custom_res(client, model_x2, tmp_path):
    src = tmp_path / "pic2.jpg"
    _make_png(src, 20, 10)  # 内容是 png 字节、扩展名 jpg——PIL 不挑扩展名也能开
    r = client.post("/api/tasks", json={
        "input": str(src), "model_id": model_x2,
        "params": {"kind": "image", "scale": 2, "format": "jpg",
                   "jpg_quality": 80, "target_w": 36, "target_h": 18},
    })
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["output_path"].endswith("_36x18.jpg")
    assert d["params"]["jpg_quality"] == 80
    assert d["params"]["target_w"] == 36 and d["params"]["target_h"] == 18


def test_create_image_task_rejects_bad_params(client, model_x2, tmp_path):
    src = tmp_path / "x.png"
    _make_png(src, 10, 10)
    base = {"input": str(src), "model_id": model_x2}
    assert client.post("/api/tasks", json={
        **base, "params": {"kind": "image", "scale": 2, "format": "webp"}}).status_code == 400
    # 自定义分辨率低于源 → 400
    assert client.post("/api/tasks", json={
        **base, "params": {"kind": "image", "scale": 2,
                           "target_w": 8, "target_h": 8}}).status_code == 400
    # 超出原生上限 → 400
    assert client.post("/api/tasks", json={
        **base, "params": {"kind": "image", "scale": 2,
                           "target_w": 9999, "target_h": 9999}}).status_code == 400
    # 非整数 tile → 400
    assert client.post("/api/tasks", json={
        **base, "params": {"kind": "image", "scale": 2, "tile": 33}}).status_code == 400


def test_create_image_task_batch_single_task(client, model_x2, tmp_path):
    """inputs 列表 → 1 个任务；同名不同目录的图自动加序号去重。"""
    a1 = tmp_path / "d1" / "same.png"
    a2 = tmp_path / "d2" / "same.png"
    b = tmp_path / "d1" / "other.png"
    for p in (a1, a2, b):
        _make_png(p, 12, 10)
    r = client.post("/api/tasks", json={
        "inputs": [str(a1), str(a2), str(b), str(a1)],  # 重复路径也去重
        "model_id": model_x2,
        "params": {"kind": "image", "scale": 2},
    })
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["total_frames"] == 3, "3 张有效图（重复路径去重）"
    imgs = d["params"]["images"]
    assert len(imgs) == 3
    outs = {i["out"] for i in imgs}
    assert len(outs) == 3, "同名输出必须去重不互相覆盖"
    assert any("_2x_2." in o for o in outs), "同名图应有 _2 序号变体"
    # 批量 + 自定义分辨率 → 400
    r2 = client.post("/api/tasks", json={
        "inputs": [str(a1), str(b)],
        "model_id": model_x2,
        "params": {"kind": "image", "scale": 2, "target_w": 20, "target_h": 16},
    })
    assert r2.status_code == 400
    # 批量 + 指定单一 output → 400
    r3 = client.post("/api/tasks", json={
        "inputs": [str(a1), str(b)], "output": str(tmp_path / "one.png"),
        "model_id": model_x2,
        "params": {"kind": "image", "scale": 2},
    })
    assert r3.status_code == 400
    # 混入非图片扩展名 → 400
    fake = tmp_path / "clip.mp4"
    fake.write_bytes(b"x")
    r4 = client.post("/api/tasks", json={
        "inputs": [str(a1), str(fake)],
        "model_id": model_x2,
        "params": {"kind": "image", "scale": 2},
    })
    assert r4.status_code == 400


def _set_outdir(client, d: str):
    r = client.put("/api/settings", json={"output_dir": d})
    assert r.status_code == 200, r.text


def test_create_image_batch_plain_names_in_clean_dir(client, model_x2, tmp_path):
    """输出目录干净时批量沿用原文件名；同名图（不同目录）后者退 _倍率 后缀。"""
    outdir = tmp_path / "clean_out"
    _set_outdir(client, str(outdir))
    a1 = tmp_path / "a.png"
    a2 = tmp_path / "sub" / "a.png"
    for p in (a1, a2):
        _make_png(p, 8, 8)
    r = client.post("/api/tasks", json={
        "inputs": [str(a1), str(a2)], "model_id": model_x2,
        "params": {"kind": "image", "scale": 2},
    })
    assert r.status_code == 201, r.text
    imgs = r.json()["params"]["images"]
    outs = [i["out"].replace("\\", "/") for i in imgs]
    assert outs[0].endswith("/clean_out/a.png"), "干净目录应沿用原名"
    assert outs[1].endswith("/clean_out/a_2x.png"), "同名撞车退后缀"


def test_create_image_same_dir_diff_ext_plain(client, model_x2, tmp_path):
    """同目录但换格式（jpg → png）：原名不与源冲突，沿用 photo.png。"""
    src = tmp_path / "photo.jpg"
    _make_png(src, 10, 10)  # PIL 按内容识别
    r = client.post("/api/tasks", json={
        "input": str(src), "model_id": model_x2,
        "params": {"kind": "image", "scale": 2, "format": "png"},
    })
    assert r.status_code == 201, r.text
    out = r.json()["output_path"].replace("\\", "/")
    assert out.endswith("/photo.png"), "换格式输出应沿用原名（不会盖到源 .jpg）"
    assert src.exists(), "源文件必须保留"


def test_corrupt_image_gives_400(client, model_x2, tmp_path):
    src = tmp_path / "broken.png"
    src.write_bytes(b"\x89PNG broken not an image")
    r = client.post("/api/tasks", json={
        "input": str(src), "model_id": model_x2,
        "params": {"kind": "image", "scale": 2}})
    assert r.status_code == 400
    assert "无法读取图片" in r.json()["detail"]
