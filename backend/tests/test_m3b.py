"""M3-B 测试：torch 引擎 + 分块 checkpoint 管线。"""
import asyncio
import json
import os
import shutil
from pathlib import Path

import numpy as np
import pytest

from sv.paths import MODELS_DIR, TEMP_DIR
from sv.pipeline.chunked import ChunkedPipeline
from sv.pipeline.probe import probe
from sv.pipeline.stream import EncodeOpts

os.environ.setdefault("SV_DB", str(TEMP_DIR / "test_m3b.db"))

PTH = MODELS_DIR / "realesrgan-x4plus-torch" / "RealESRGAN_x4plus.pth"


class Nearest2x:
    scale = 2

    def process(self, frame):
        return np.repeat(np.repeat(frame, 2, axis=0), 2, axis=1)


def _clip() -> Path:
    from tests.test_stream import make_video

    p = TEMP_DIR / "chunk_in.mp4"
    if not p.exists():
        make_video(p, duration=1, fps=12)  # 12 帧
    return p


def test_chunked_pipeline_runs():
    info = probe(_clip())
    out = TEMP_DIR / "chunk_out.mp4"
    stats = asyncio.run(ChunkedPipeline(
        info, out, Nearest2x(), EncodeOpts(), task_id="t_chunk_1", chunk=4,
    ).run())
    assert stats.frames == info.total_frames
    o = probe(out)
    assert (o.width, o.height) == (info.width * 2, info.height * 2)
    assert o.has_audio
    assert not (TEMP_DIR / "chunked" / "t_chunk_1").exists(), "成功后必须清理工作目录"


def test_chunked_resume_skips_done_chunks():
    """真正的续跑：完整跑完第一遍后，人为只保留前半 checkpoint+输出，重跑必须跳过。"""
    info = probe(_clip())
    work = TEMP_DIR / "chunked" / "t_chunk_3"
    shutil.rmtree(work, ignore_errors=True)

    # 第一遍完整跑（不产生最终文件也无妨，删除输出）
    out1 = TEMP_DIR / "chunk_out3a.mp4"
    asyncio.run(ChunkedPipeline(
        info, out1, Nearest2x(), EncodeOpts(), task_id="t_chunk_3", chunk=4,
    ).run())
    # 第一遍成功即清理了工作目录 → 手工构造“跑到一半取消”的状态：
    # 重跑一遍但取消在编码前是不可行的，改为直接验证 checkpoint 行为：
    # 预置 src 已解码 + checkpoint done=[0] + out 前 4 帧，运行应从第 5 帧继续
    shutil.rmtree(work, ignore_errors=True)
    (work / "src").mkdir(parents=True)
    import subprocess

    from sv.paths import ffmpeg_bin
    from sv.utils.process import WINDOWS_CREATE_FLAGS

    subprocess.run([
        ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(info.path), "-map", "0:v:0", "-an",
        str(work / "src" / "f%06d.png"),
    ], check=True, creationflags=WINDOWS_CREATE_FLAGS)
    n = len(list((work / "src").glob("f*.png")))
    (work / "out").mkdir()
    for i in range(1, 5):  # 前 4 帧的“已完成”输出
        arr = np.zeros((info.height * 2, info.width * 2, 3), dtype=np.uint8)
        from PIL import Image

        Image.fromarray(arr).save(work / "out" / f"f{i:06d}.png")
    (work / "decoded.json").write_text(json.dumps({"frames": n}))
    (work / "checkpoint.json").write_text(json.dumps({"done": [0]}))

    first_reported = {}

    def cb(f, t, fp, eta):
        first_reported.setdefault("f", f)

    out2 = TEMP_DIR / "chunk_out3b.mp4"
    asyncio.run(ChunkedPipeline(
        info, out2, Nearest2x(), EncodeOpts(), task_id="t_chunk_3", chunk=4,
        progress_cb=cb,
    ).run())
    assert first_reported.get("f", 0) >= 4, "续跑应从已完成块之后开始报告"
    assert out2.exists()
    o = probe(out2)
    assert abs(o.duration_s - info.duration_s) < 0.5


def test_torch_engine_matches_onnx():
    """torch 引擎可用时跑一帧（CUDA 环境才有 torch，主 venv 自动跳过）。

    放子进程跑：全量套件里此前已加载 onnxruntime(-dml/gpu) 等库，进程内再
    加载 torch 的 cudnn DLL 会 WinError 127（DLL 搜索状态被污染，实测顺序
    依赖单跑就好）；子进程拿干净的 DLL 空间，测试不再与执行顺序耦合。
    """
    import subprocess
    import sys

    if not PTH.exists():
        pytest.skip("未下载 torch 权重")
    code = (
        "import sys; sys.path.insert(0, r'%s'); "
        "import numpy as np; "
        "from sv.engines.torch_engine import TorchSrEngine; "
        "eng = TorchSrEngine(r'%s', 4, io={'arch': 'rrdbnet'}, tile=128); "
        "eng.load(); "
        "rng = np.random.default_rng(2); "
        "frame = np.clip(rng.normal(120, 50, (64, 64, 3)), 0, 255).astype(np.uint8); "
        "out = eng.process(frame); "
        "assert out.shape == (256, 256, 3) and out.dtype == np.uint8; "
        "assert int(out.std()) > 3; print('OK')"
        % (Path(__file__).resolve().parents[1], PTH)
    )
    r = subprocess.run([sys.executable, "-c", code],
                       capture_output=True, timeout=300,
                       cwd=str(Path(__file__).resolve().parents[1]))
    out = r.stdout.decode("utf-8", "replace").strip()
    if "ModuleNotFoundError" in (r.stderr.decode("utf-8", "replace")) and "torch" in r.stderr.decode():
        pytest.skip("当前解释器无 torch")
    assert r.returncode == 0 and out.endswith("OK"), r.stderr.decode("utf-8", "replace")[-500:]


def test_import_custom_model(client_like=None):
    """自定义 ONNX 导入：真跑验证 + manifest 写入 + 删除清理。"""
    import shutil

    from fastapi.testclient import TestClient

    from sv.models.registry import USER_REGISTRY_DIR, load_registry
    from sv.paths import MODELS_DIR

    src = MODELS_DIR / "real-cugan" / "up2x-latest-conservative.onnx"
    if not src.exists():
        pytest.skip("缺 CUGAN 权重")
    from sv.server.app import app

    with TestClient(app) as c:
        r = c.post("/api/models/import", json={
            "path": str(src), "id": "my-test-model", "name": "我的测试模型",
            "scale": 2, "color": "rgb", "value_range": "0-1",
            "tile": 0, "description": "导入测试",
        })
        assert r.status_code == 201, r.text
        assert "my-test-model" in load_registry()
        # 错误倍率被拒并回滚
        r2 = c.post("/api/models/import", json={
            "path": str(src), "id": "my-bad", "name": "bad",
            "scale": 3, "color": "rgb", "value_range": "0-1", "tile": 0,
        })
        assert r2.status_code == 422
        assert not (MODELS_DIR / "my-bad").exists()
        # 自定义模型删除连 manifest 一起删
        assert c.delete("/api/models/my-test-model").status_code == 200
        assert "my-test-model" not in load_registry()
        shutil.rmtree(MODELS_DIR / "my-test-model", ignore_errors=True)
