"""图片/漫画任务断点续跑 + 十万页扩容配套。

关键语义：
- 逐页产物即 checkpoint：取消/失败保留输出（runner 清理链豁免），重跑按
  「产物 mtime ≥ 任务创建时间」跳过已完成页——中断前写出的跳过续跑，
  上一轮跑完的旧产物（重跑同文件夹刻意沿用同名逐页覆盖）必须重做；
- resume 端点对图片系任务不删输出（删了就丢断点），断点守卫=有任何一页
  产物在；视频任务沿用分段工作目录守卫；
- 文件夹扫描上限 10 万张；超大清单列表响应裁剪（首页+images_count）；
- 创建期尺寸只读头部（EXIF 方向横竖互换直接换算），不解码像素。
"""
import os
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from sv.paths import TEMP_DIR

os.environ.setdefault("SV_DB", str(TEMP_DIR / "test_image_resume.db"))

SPEC = SimpleNamespace(
    id="imgsr-fake", engine="onnx", scale=[2],
    fp16=False, tile_hint=0,
    io={"color": "rgb", "range": "0-255", "pad": 2},
)


@pytest.fixture(scope="module")
def client():
    p = Path(os.environ["SV_DB"])
    if p.exists():
        p.unlink()
    from sv.server.app import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _tmp_settings(tmp_path, monkeypatch):
    from sv.server import settings as _settings

    monkeypatch.setattr(_settings, "SETTINGS_PATH",
                        tmp_path / "data" / "settings.json")


class _Lane2x:
    """假 2x 引擎：只计数，输出 = 输入像素 +1（可区分新旧产物）。"""

    def __init__(self):
        self.seen = 0

    def process(self, frame):
        self.seen += 1
        return (frame.astype(np.int16) + 1).clip(0, 255).astype(np.uint8)


@pytest.fixture()
def fake_engine(monkeypatch):
    eng = _Lane2x()
    events: list[dict] = []
    timeline: list[str] = []

    def _fake_load(weight, spec, scale, variant, precision, tile, warmup_hw, *,
                   batch=1, log=None, slot="main"):
        timeline.append(slot)
        return eng, "fp32"

    import sv.models.manager as mgr
    import sv.models.registry as reg
    import sv.server.worker_image as worker_image_mod

    monkeypatch.setattr(worker_image_mod, "_load_onnx_engine", _fake_load)
    monkeypatch.setattr(worker_image_mod, "_release_slot",
                        lambda slot: timeline.append(f"release:{slot}"))
    monkeypatch.setattr(worker_image_mod, "model_file",
                        lambda *a, **k: Path("fake.onnx"))
    monkeypatch.setattr(reg, "file_for_scale",
                        lambda spec, scale, variant=None: Path("fake.onnx"))
    monkeypatch.setattr(mgr, "ensure_files", lambda spec, needs: None)
    monkeypatch.setattr(worker_image_mod, "emit", events.append)
    return SimpleNamespace(eng=eng, events=events, timeline=timeline)


def _gray_png(path: Path, v: int = 100, w: int = 12, h: int = 12):
    path.parent.mkdir(parents=True, exist_ok=True)
    a = np.full((h, w, 3), v, dtype=np.uint8)
    Image.fromarray(a).save(str(path))


def _mk_task(tmp_path, n, created_at=None):
    """构造 n 页任务（真源图 + 假输出路径），返回 (task, images)。"""
    images = []
    for i in range(n):
        src = tmp_path / f"p{i}.png"
        _gray_png(src, v=100 + i)
        images.append({"in": str(src), "out": str(tmp_path / f"p{i}_2x.png")})
    task = {"id": f"resume{uuid.uuid4().hex[:8]}",
            "model_id": SPEC.id,
            "input_path": images[0]["in"], "output_path": images[0]["out"],
            "created_at": created_at if created_at is not None else time.time()}
    return task, images


# ---- worker：断点续跑跳过语义 ----

@pytest.mark.parametrize("async_save", [False, True])
def test_run_image_job_skips_pages_done_after_creation(tmp_path, fake_engine, async_save):
    """中断前写出的产物（mtime ≥ 创建时间）跳过：引擎只推理剩余页，
    记账按全量算（ok/进度/PDF 页序都含跳过页）。"""
    task, images = _mk_task(tmp_path, 3)
    # 模拟上次中断前完成了前两页
    for m in images[:2]:
        _gray_png(Path(m["out"]), v=9)
    os.utime(images[0]["out"], (time.time(), time.time()))
    os.utime(images[1]["out"], (time.time(), time.time()))

    from sv.server.worker import _run_image_job

    rc = _run_image_job(task, {
        "kind": "manga", "format": "png", "scale": 2, "target_scale": 2,
        "images": images, "async_save": async_save,
    }, SPEC)
    assert rc == 0, fake_engine.events
    assert fake_engine.eng.seen == 1, "只应推理未完成的第 3 页"
    done = [e for e in fake_engine.events if e.get("type") == "done"]
    assert done and done[-1]["frames"] == 3, "跳过页计入完成数"
    skip_log = [e for e in fake_engine.events
                if e.get("type") == "log" and "断点续跑" in e.get("line", "")]
    assert skip_log and "2 页" in skip_log[-1]["line"]
    # 第 3 页是本趟真产物（源值 102 +1 标记），跳过页保留原值
    with Image.open(images[2]["out"]) as im:
        assert np.asarray(im)[0, 0, 0] == 103
    with Image.open(images[0]["out"]) as im:
        assert np.asarray(im)[0, 0, 0] == 9


def test_run_image_job_redoes_stale_outputs(tmp_path, fake_engine):
    """早于任务创建时间的同名产物=上一轮旧输出（重跑覆盖语义）：必须重做。"""
    created = time.time()
    task, images = _mk_task(tmp_path, 2, created_at=created)
    _gray_png(Path(images[0]["out"]), v=9)
    old = created - 3600
    os.utime(images[0]["out"], (old, old))

    from sv.server.worker import _run_image_job

    rc = _run_image_job(task, {
        "kind": "manga", "format": "png", "scale": 2, "target_scale": 2,
        "images": images,
    }, SPEC)
    assert rc == 0, fake_engine.events
    assert fake_engine.eng.seen == 2, "旧产物不构成断点，两页都要重做"
    with Image.open(images[0]["out"]) as im:
        assert np.asarray(im)[0, 0, 0] == 101, "旧产物被本趟覆盖"


def test_run_image_job_all_done_never_builds_engine(tmp_path, fake_engine):
    """全部页沿用断点产物：引擎不构建，任务直接收尾，预览从磁盘回读。"""
    task, images = _mk_task(tmp_path, 2)
    for m in images:
        _gray_png(Path(m["out"]), v=9)

    from sv.server.worker import _run_image_job

    rc = _run_image_job(task, {
        "kind": "manga", "format": "png", "scale": 2, "target_scale": 2,
        "images": images,
    }, SPEC)
    assert rc == 0, fake_engine.events
    assert fake_engine.eng.seen == 0 and not fake_engine.timeline
    done = [e for e in fake_engine.events if e.get("type") == "done"]
    assert done and done[-1]["frames"] == 2
    assert (TEMP_DIR / "previews" / f"{task['id']}.jpg").exists(), \
        "全跳过路径仍要产出预览缩略图（磁盘回读兜底）"


def test_run_image_job_pdf_includes_skipped_pages(tmp_path, fake_engine, monkeypatch):
    """merge_pdf 续跑：跳过页按原页序进 PDF，缺页不丢序。"""
    import sv.pdfmerge as pdfmerge_mod

    task, images = _mk_task(tmp_path, 3)
    _gray_png(Path(images[0]["out"]), v=9)  # 第 1 页已完成
    pdf_args: list = []

    def _fake_pdf(written, out):
        pdf_args.append([Path(w).name for w in written])
        Path(out).write_bytes(b"%PDF-fake")
        return {"pages": len(written)}

    monkeypatch.setattr(pdfmerge_mod, "write_pdf", _fake_pdf)
    from sv.server.worker import _run_image_job

    rc = _run_image_job(task, {
        "kind": "manga", "format": "png", "scale": 2, "target_scale": 2,
        "images": images, "merge_pdf": True, "pdf_out": str(tmp_path / "book.pdf"),
    }, SPEC)
    assert rc == 0, fake_engine.events
    assert pdf_args and pdf_args[0] == [Path(images[i]["out"]).name for i in range(3)], \
        "PDF 按原页序含全部成功页（跳过+新做）"


def test_split_pass_all_bw_done_skips_release(tmp_path, fake_engine):
    """分趟模式黑白页全部沿用断点：不建黑白引擎也无需趟末释放。"""
    task, images = _mk_task(tmp_path, 2)
    _gray_png(Path(images[0]["out"]), v=9)
    _gray_png(Path(images[1]["out"]), v=9)
    from sv.models.registry import get_model

    for mid in ("illustrationjanai-2x", "mangajanai"):
        try:
            color_spec = get_model(mid)
            break
        except KeyError:
            continue
    else:
        pytest.skip("注册表中无 x2 彩模")

    from sv.server.worker import _run_image_job

    rc = _run_image_job(task, {
        "kind": "manga", "format": "png", "scale": 2, "target_scale": 2,
        "images": images, "model_id_color": color_spec.id, "mix_pass": "split",
    }, SPEC)
    assert rc == 0, fake_engine.events
    assert "release:main" not in fake_engine.timeline, "黑白引擎未构建，不应触发趟末释放"


# ---- resume 端点 ----

@pytest.fixture(scope="module")
def any_x2_model(client):
    models = {m["id"]: m for m in client.get("/api/models").json()}
    for mid in ("mangajanai", "illustrationjanai-2x", "realesr-animevideov3"):
        if mid in models and 2 in models[mid]["scale"]:
            return mid
    pytest.fail("注册表中无 x2 模型")


def _mk_api_task(client, tmp_path, model_id):
    src = tmp_path / "page.png"
    _gray_png(src)
    r = client.post("/api/tasks", json={
        "input": str(src), "model_id": model_id,
        "params": {"kind": "manga", "scale": 2},
    })
    assert r.status_code == 201, r.text
    return r.json()


def test_resume_image_task_keeps_outputs(client, tmp_path, any_x2_model):
    """图片任务续跑：已有产物保留（就是断点），不清输出路径。"""
    from sv.server import db as svdb

    d = _mk_api_task(client, tmp_path, any_x2_model)
    out = Path(d["output_path"])
    _gray_png(out, v=9)
    svdb.update_task(d["id"], status="canceled", progress_frames=1)
    r = client.post(f"/api/tasks/{d['id']}/resume")
    assert r.status_code == 200, r.text
    assert out.exists(), "续跑不得删除图片产物（断点依据）"
    assert svdb.get_task(d["id"])["status"] == "queued"


def test_resume_image_task_without_outputs_409(client, tmp_path, any_x2_model):
    """跑过但产物全没了：与视频同款明确报错，不悄悄从头重跑。"""
    from sv.server import db as svdb

    d = _mk_api_task(client, tmp_path, any_x2_model)
    svdb.update_task(d["id"], status="canceled", progress_frames=5)
    r = client.post(f"/api/tasks/{d['id']}/resume")
    assert r.status_code == 409
    assert "产物" in r.json()["detail"]


def test_resume_image_task_never_started_ok(client, tmp_path, any_x2_model):
    """排队期取消（零进度、无产物）：续跑=正常从头跑。"""
    from sv.server import db as svdb

    d = _mk_api_task(client, tmp_path, any_x2_model)
    svdb.update_task(d["id"], status="canceled")
    r = client.post(f"/api/tasks/{d['id']}/resume")
    assert r.status_code == 200, r.text


# ---- 十万页扩容与列表裁剪 ----

def test_folder_scan_cap_raised(tmp_path, client):
    """3001 张（旧上限之上）能扫过：十万页量级的门槛放行。"""
    d = tmp_path / "book"
    d.mkdir()
    for i in range(3001):
        (d / f"p{i:05d}.png").touch()
    r = client.post("/api/images/scan", json={"folder": str(d)})
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 3001


def test_folder_scan_over_cap_rejected(tmp_path, client, monkeypatch):
    """超上限仍明确报数（用假上限避免真建十万文件）。"""
    from sv.server.routes import tasks as tasks_mod

    monkeypatch.setattr(tasks_mod, "_FOLDER_SCAN_CAP", 3)
    d = tmp_path / "tiny"
    d.mkdir()
    for i in range(4):
        (d / f"p{i}.png").touch()
    r = client.post("/api/images/scan", json={"folder": str(d)})
    assert r.status_code == 400
    assert "3" in r.json()["detail"]


def test_tasks_list_slims_huge_image_manifest(client):
    """列表响应裁掉超大清单（首页+images_count），详情接口保持全量。"""
    from sv.server import db as svdb
    from sv.server.routes.tasks import _IMAGES_TRIM_AT

    imgs = [{"in": f"p{i}.png", "out": f"p{i}_2x.png"} for i in range(_IMAGES_TRIM_AT + 5)]
    t = svdb.new_task("p0.png", imgs[0]["out"], "mangajanai",
                      {"kind": "manga", "images": imgs})
    listed = [x for x in client.get("/api/tasks").json() if x["id"] == t["id"]]
    assert listed, "新任务应出现在列表里"
    p = listed[0]["params"]
    assert p["images_count"] == _IMAGES_TRIM_AT + 5
    assert len(p["images"]) == 1 and p["images"][0]["in"] == "p0.png"
    detail = client.get(f"/api/tasks/{t['id']}").json()
    assert len(detail["params"]["images"]) == _IMAGES_TRIM_AT + 5, "详情不裁剪"


def test_slim_keeps_small_manifest():
    """小清单原样透传（现有行为零变化）。"""
    from sv.server.routes.tasks import _slim_image_manifest

    t = {"params": {"kind": "image",
                    "images": [{"in": f"p{i}.png", "out": f"o{i}.png"} for i in range(50)]}}
    _slim_image_manifest(t)
    assert len(t["params"]["images"]) == 50
    assert "images_count" not in t["params"]


def test_input_exists_sampling():
    """超大清单抽样判源在：抽样点缺失才置灰，小清单保持全量精确。"""
    from sv.server.routes.tasks import _input_exists

    big = {"params": {"images": [{"in": f"p{i}.png"} for i in range(2000)]}}
    assert _input_exists(big) is False  # 文件都不存在
    small = {"params": {"images": [{"in": __file__}, {"in": "nope.png"}]}}
    assert _input_exists(small) is False


def test_create_reads_size_from_header_with_exif(client, tmp_path, any_x2_model):
    """创建期尺寸=头部+EXIF 方向换算（不解码像素也能拿对宽高）。"""
    src = tmp_path / "portrait.png"
    a = np.full((10, 30, 3), 128, dtype=np.uint8)  # 存储为 30 宽 10 高
    exif = Image.Exif()
    exif[274] = 6  # 顺时针 90°：转正后 10 宽 30 高
    Image.fromarray(a).save(str(src), exif=exif)
    r = client.post("/api/tasks", json={
        "input": str(src), "model_id": any_x2_model,
        "params": {"kind": "manga", "scale": 2},
    })
    assert r.status_code == 201, r.text
    assert (r.json()["src_w"], r.json()["src_h"]) == (10, 30)
