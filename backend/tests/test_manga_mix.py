"""漫画混装双模型：创建期彩色/黑白识别、校验纪律、worker 双引擎分派。

识别与分派覆盖的是胶水层：_page_is_color 判据（灰/纸色偏/彩页）、lane 落库、
倍率纪律、按 slot 构建两台引擎且彩页走彩模（假引擎输出带区分标记）。
推理用假引擎，不依赖 GPU/真实模型。
"""
import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from sv.paths import TEMP_DIR

os.environ.setdefault("SV_DB", str(TEMP_DIR / "test_manga_mix.db"))

SPEC = SimpleNamespace(
    id="imgsr-fake", engine="onnx", scale=[2],
    fp16=False, tile_hint=0,
    io={"color": "rgb", "range": "0-255", "pad": 2},
)


def _gray_png(path: Path, w: int = 40, h: int = 40):
    """纯灰渐变页（黑白漫画：网点/线条都是 R=G=B）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    g = np.arange(w * h, dtype=np.uint8).reshape(h, w)
    Image.fromarray(np.stack([g, g, g], axis=-1)).save(str(path))


def _yellowed_png(path: Path, w: int = 40, h: int = 40):
    """均匀发黄的旧扫描页：整页恒定通道偏移（R>G>B），极差恒定 std≈0。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    g = np.arange(w * h, dtype=np.int16).reshape(h, w)
    a = np.stack([g + 25, g + 10, g - 15], axis=-1).clip(0, 255).astype(np.uint8)
    Image.fromarray(a).save(str(path))


def _color_png(path: Path, w: int = 40, h: int = 40):
    """典型彩页：白底（极差 0）+ 高饱和色块（极差大）并存。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    a = np.full((h, w, 3), 245, dtype=np.uint8)  # 白底
    a[: h // 3] = (200, 40, 40)  # 红色块
    a[h // 3: 2 * h // 3] = (40, 90, 200)  # 蓝色块
    Image.fromarray(a).save(str(path))


# ---- 识别判据 ----

def test_page_is_color_gray_and_tone():
    from sv.server.routes.tasks import _page_is_color

    assert _page_is_color(Image.new("RGB", (64, 64), 128)) is False
    g = np.arange(64 * 64, dtype=np.uint8).reshape(64, 64)
    gray = Image.fromarray(np.stack([g, g, g], axis=-1))
    assert _page_is_color(gray) is False, "纯灰渐变（网点）不是彩色"


def test_page_is_color_yellowed_paper():
    """均匀发黄的旧扫描件是黑白：通道极差恒定，std≈0，不误判。"""
    from sv.server.routes.tasks import _page_is_color

    y = (30 + np.arange(64 * 64) % 180).reshape(64, 64)  # 动态范围不溢出
    a = np.stack([y + 25, y + 10, y - 15], axis=-1).clip(0, 255).astype(np.uint8)
    assert _page_is_color(Image.fromarray(a)) is False


def test_page_is_color_real_page():
    from sv.server.routes.tasks import _page_is_color

    a = np.full((64, 64, 3), 245, dtype=np.uint8)
    a[:21] = (200, 40, 40)
    a[21:42] = (40, 90, 200)
    assert _page_is_color(Image.fromarray(a)) is True


# ---- 创建端：校验与 lane 落库 ----

@pytest.fixture(scope="module")
def client():
    from sv.server.app import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def manga_models(client):
    """真实注册表里挑一对共同支持 x2 的模型：主(黑白) + 彩模。"""
    models = {m["id"]: m for m in client.get("/api/models").json()}
    for bw_id in ("mangajanai", "illustrationjanai-4x-dat2", "illustrationjanai-4x"):
        for color_id in ("illustrationjanai-2x", "mangajanai"):
            if bw_id in models and color_id in models and bw_id != color_id \
                    and 2 in models[bw_id]["scale"] and 2 in models[color_id]["scale"]:
                return {"bw": bw_id, "color": color_id}
    pytest.fail("注册表中找不到共同支持 x2 的一对模型")


@pytest.fixture(autouse=True)
def _tmp_settings(tmp_path, monkeypatch):
    from sv.server import settings as _settings

    monkeypatch.setattr(_settings, "SETTINGS_PATH", tmp_path / "data" / "settings.json")


def test_create_mix_task_lanes_persisted(client, manga_models, tmp_path):
    """混装任务：创建期逐页识别，彩页 lane=color 落库、灰页不带字段。"""
    gray = tmp_path / "p1.png"
    color = tmp_path / "p2.png"
    _gray_png(gray)
    _color_png(color)
    r = client.post("/api/tasks", json={
        "inputs": [str(gray), str(color)],
        "model_id": manga_models["bw"],
        "params": {"kind": "manga", "scale": 2,
                   "model_id_color": manga_models["color"]},
    })
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["params"]["model_id_color"] == manga_models["color"]
    lanes = [m.get("lane") for m in d["params"]["images"]]
    assert lanes == [None, "color"], "灰页缺省 bw、彩页标 color"


def test_create_mix_task_rejects_same_and_unknown(client, manga_models, tmp_path):
    src = tmp_path / "p.png"
    _gray_png(src)
    base = {"inputs": [str(src)], "params": {"kind": "manga", "scale": 2}}
    r = client.post("/api/tasks", json={
        **base, "model_id": manga_models["bw"],
        "params": {**base["params"], "model_id_color": manga_models["bw"]}})
    assert r.status_code == 400 and "相同" in r.json()["detail"]
    r = client.post("/api/tasks", json={
        **base, "model_id": manga_models["bw"],
        "params": {**base["params"], "model_id_color": "no-such-model"}})
    assert r.status_code == 404 and "彩色页模型" in r.json()["detail"]


def test_create_mix_task_rejects_scale_mismatch(client, manga_models, tmp_path):
    """两模型需共同支持同一倍率：彩模只支持 x2 时提交 x4 → 400。"""
    src = tmp_path / "p.png"
    _gray_png(src)
    models = {m["id"]: m for m in client.get("/api/models").json()}
    color = models[manga_models["color"]]
    bad_scale = next((s for s in (4,) if s not in color["scale"]), None)
    if bad_scale is None or bad_scale not in models[manga_models["bw"]]["scale"]:
        pytest.skip("注册表中该模型对不存在倍率错配场景")
    r = client.post("/api/tasks", json={
        "inputs": [str(src)], "model_id": manga_models["bw"],
        "params": {"kind": "manga", "scale": bad_scale,
                   "model_id_color": manga_models["color"]},
    })
    assert r.status_code == 400 and "彩色页模型" in r.json()["detail"]


# ---- worker：双引擎按页分派 ----

class _Lane2x:
    """假引擎：2x 最近邻；color 车带的实例把红通道拉满作输出标记。"""

    scale = 2

    def __init__(self, mark_color: bool):
        self.mark_color = mark_color
        self.seen = 0

    def load(self):
        pass

    def process(self, frame):
        self.seen += 1
        out = np.repeat(np.repeat(frame, 2, axis=0), 2, axis=1)
        if self.mark_color:
            out = out.copy()
            out[:, :, 0] = 255
        return out


@pytest.fixture()
def dual_engine(monkeypatch):
    """按 slot 返回两台可区分的假引擎，记录构建过的 slot。"""
    bw = _Lane2x(mark_color=False)
    color = _Lane2x(mark_color=True)
    slots: list[str] = []
    events: list[dict] = []

    def _fake_load(weight, spec, scale, variant, precision, tile, warmup_hw, *,
                   batch=1, log=None, slot="main"):
        slots.append(slot)
        eng = color if slot == "color" else bw
        return eng, "fp32"

    import sv.models.manager as mgr
    import sv.models.registry as reg
    import sv.server.worker_image as worker_image_mod

    monkeypatch.setattr(worker_image_mod, "_load_onnx_engine", _fake_load)
    monkeypatch.setattr(worker_image_mod, "model_file",
                        lambda *a, **k: Path("fake.onnx"))
    monkeypatch.setattr(reg, "file_for_scale",
                        lambda spec, scale, variant=None: Path("fake.onnx"))
    monkeypatch.setattr(mgr, "ensure_files", lambda spec, needs: None)
    monkeypatch.setattr(worker_image_mod, "emit", events.append)
    return SimpleNamespace(bw=bw, color=color, slots=slots, events=events)


def _color_spec():
    """彩模 spec：从真实注册表取（worker 按 get_model 解析）。"""
    from sv.models.registry import get_model

    for mid in ("illustrationjanai-2x", "mangajanai"):
        try:
            return get_model(mid)
        except KeyError:
            continue
    pytest.fail("注册表中无可用彩模")


def test_image_job_mix_dispatches_by_lane(tmp_path, dual_engine):
    """彩页走彩模引擎（输出红通道被标记）、灰页走主引擎；两个 slot 都构建。"""
    gray = tmp_path / "bw.png"
    color = tmp_path / "color.png"
    _gray_png(gray, 10, 10)
    _color_png(color, 10, 10)
    out_gray = tmp_path / "bw_2x.png"
    out_color = tmp_path / "color_2x.png"
    images = [
        {"in": str(gray), "out": str(out_gray)},
        {"in": str(color), "out": str(out_color), "lane": "color"},
    ]
    from sv.server.worker import _run_image_job

    task = {"id": "mix1", "model_id": SPEC.id,
            "input_path": images[0]["in"], "output_path": images[0]["out"]}
    color_spec = _color_spec()
    rc = _run_image_job(task, {
        "kind": "manga", "format": "png", "scale": 2, "target_scale": 2,
        "images": images, "model_id_color": color_spec.id,
    }, SPEC)
    assert rc == 0, dual_engine.events
    assert set(dual_engine.slots) == {"main", "color"}, dual_engine.slots
    with Image.open(out_gray) as im:
        a = np.asarray(im)
        assert a[:, :, 0].max() < 255 or a[:, :, 0].max() == a[:, :, 2].max(), \
            "灰页不应被彩模标记"
        assert a.shape[:2] == (20, 20)
    with Image.open(out_color) as im:
        a = np.asarray(im)
        assert (a[:, :, 0] == 255).all(), "彩页应走彩模引擎（红通道标记）"
    assert dual_engine.bw.seen >= 1 and dual_engine.color.seen == 1
    mixed = [e for e in dual_engine.events
             if e.get("type") == "log" and "混装分派" in e.get("line", "")]
    assert mixed and "彩色 1 页 / 黑白 1 页" in mixed[0]["line"]


def test_image_job_pure_bw_skips_color_engine(tmp_path, dual_engine):
    """纯黑白本：彩模引擎不构建（省显存），只出 main 槽。"""
    gray = tmp_path / "p1.png"
    _gray_png(gray, 10, 10)
    out = tmp_path / "p1_2x.png"
    from sv.server.worker import _run_image_job

    task = {"id": "mix2", "model_id": SPEC.id,
            "input_path": str(gray), "output_path": str(out)}
    color_spec = _color_spec()
    rc = _run_image_job(task, {
        "kind": "manga", "format": "png", "scale": 2,
        "images": [{"in": str(gray), "out": str(out)}],
        "model_id_color": color_spec.id,
    }, SPEC)
    assert rc == 0, dual_engine.events
    assert dual_engine.slots == ["main"], dual_engine.slots
    assert dual_engine.color.seen == 0


def test_engine_cache_slots_independent(monkeypatch):
    """槽位缓存：main 与 color 各自独立复用/换签名，互不清退。"""
    import sv.server.worker_engine as we

    we._ENGINE_CACHE.clear()
    calls: list[str] = []

    class _Eng:
        def __init__(self, tag):
            self.tag = tag

    def _fake_build(engine_tag):
        def _load(weight, spec, scale, variant, precision, tile, warmup_hw, *,
                  batch=1, log=None, slot="main"):
            calls.append(slot)
            return _Eng(engine_tag), "fp32"
        return _load

    # 精准 patch OnnxSrEngine 构造路径做不到（_load_onnx_engine 内部直用），
    # 这里直接检验槽位簿记：手工填充两槽后 _release_aux_slots 只清 color
    we._ENGINE_CACHE["main"] = {"sig": "s1", "engine": _Eng("bw"), "precision": "fp32"}
    we._ENGINE_CACHE["color"] = {"sig": "s2", "engine": _Eng("color"), "precision": "fp32"}
    we._release_aux_slots()
    assert list(we._ENGINE_CACHE) == ["main"]
    we._ENGINE_CACHE.clear()
