"""sidecar 集成测试：串行队列 / 取消 / 任务参数独立性 / 产物正确性。"""
import os
import subprocess
import time
from pathlib import Path

import psutil
import pytest
from fastapi.testclient import TestClient

from sv.paths import TEMP_DIR, ffmpeg_bin
from sv.utils.process import WINDOWS_CREATE_FLAGS

os.environ["SV_DB"] = str(TEMP_DIR / "test_sidecar.db")


def make_clip(path: Path, w: int, h: int, duration: float):
    subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate=24",
         "-f", "lavfi", "-i", "sine=frequency=440",
         "-t", str(duration), "-c:v", "libx264", "-crf", "22", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(path)],
        check=True, creationflags=WINDOWS_CREATE_FLAGS,
    )


@pytest.fixture(scope="module")
def client():
    if os.environ["SV_DB"] and Path(os.environ["SV_DB"]).exists():
        Path(os.environ["SV_DB"]).unlink()
    from sv.server.app import app

    with TestClient(app) as c:  # 触发 lifespan: init_db + runner
        yield c


@pytest.fixture(scope="module")
def clips():
    TEMP_DIR.mkdir(exist_ok=True)
    tiny = TEMP_DIR / "srv_tiny.mp4"
    medium = TEMP_DIR / "srv_medium.mp4"
    make_clip(tiny, 160, 90, 2)
    make_clip(medium, 640, 360, 12)
    return {"tiny": tiny, "medium": medium}


def wait_status(client, task_id, statuses, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = client.get(f"/api/tasks/{task_id}").json()
        if t["status"] in statuses:
            return t
        time.sleep(0.3)
    raise TimeoutError(f"任务 {task_id} 停留在 {t['status']}: {t.get('error')}")


def test_health_and_models(client):
    assert client.get("/api/health").json()["ok"]
    models = client.get("/api/models").json()
    ids = {m["id"] for m in models}
    assert "realesr-animevideov3" in ids
    hw = client.get("/api/hardware").json()
    assert hw["ram_gb"] > 0


def test_serial_queue_no_concurrency(client, clips):
    """三个任务严格串行：任意时刻 running 的任务数 <= 1。"""
    ids = []
    for i in range(3):
        out = TEMP_DIR / f"srv_serial_{i}.mp4"
        r = client.post("/api/tasks", json={
            "input": str(clips["tiny"]), "output": str(out),
            "model_id": "realesr-animevideov3", "params": {},
        })
        assert r.status_code == 201, r.text
        ids.append(r.json()["id"])

    # 轮询观察并发度
    max_running = 0
    while True:
        tasks = {t["id"]: t for t in client.get("/api/tasks").json()}
        running = [t for t in tasks.values() if t["status"] == "running"]
        max_running = max(max_running, len(running))
        if all(tasks[i]["status"] in ("done", "failed", "canceled") for i in ids):
            break
        time.sleep(0.15)

    for i in ids:
        t = wait_status(client, i, ("done",))
        assert t["status"] == "done", t.get("error")
        assert Path(t["output_path"]).exists()
        assert t["out_bytes"] > 0
    assert max_running == 1, "队列必须严格串行"


def test_per_task_params(client, clips):
    """同输入两个任务不同参数（crf/codec 各自独立），产物都正确。"""
    r1 = client.post("/api/tasks", json={
        "input": str(clips["tiny"]),
        "model_id": "realesr-animevideov3",
        "params": {"crf": 18, "codec": "h264"},
    })
    r2 = client.post("/api/tasks", json={
        "input": str(clips["tiny"]),
        "model_id": "realesr-animevideov3",
        "params": {"crf": 30, "codec": "h265"},
    })
    assert r1.status_code == 201 and r2.status_code == 201
    t1, t2 = r1.json(), r2.json()
    assert t1["params"]["crf"] == 18 and t2["params"]["crf"] == 30
    assert t2["params"]["codec"] == "h265"
    wait_status(client, t1["id"], ("done",))
    wait_status(client, t2["id"], ("done",))
    assert Path(t1["output_path"]).exists() and Path(t2["output_path"]).exists()


def test_invalid_input_rejected(client, clips):
    r = client.post("/api/tasks", json={
        "input": str(TEMP_DIR / "not_exist.mp4"),
        "model_id": "realesr-animevideov3", "params": {},
    })
    assert r.status_code == 400
    r = client.post("/api/tasks", json={
        "input": str(clips["tiny"]),
        "model_id": "realesr-animevideov3",
        "params": {"scale": 3},
    })
    assert r.status_code == 400  # animevideov3 只有 x4


def test_cancel_running_kills_tree(client, clips):
    """取消运行中任务：状态 canceled、半成品清理、无残留子进程。"""
    me = psutil.Process()
    children_before = len(me.children(recursive=True))

    out = TEMP_DIR / "srv_cancel_out.mp4"
    r = client.post("/api/tasks", json={
        "input": str(clips["medium"]), "output": str(out),
        "model_id": "realesr-animevideov3", "params": {},
    })
    task_id = r.json()["id"]

    t = wait_status(client, task_id, ("running", "done"), timeout=60)
    if t["status"] == "running":  # 12s 片可能瞬间跑完则跳过取消
        time.sleep(2)  # 让它处理一部分
        assert client.post(f"/api/tasks/{task_id}/cancel").status_code == 200
        t = wait_status(client, task_id, ("canceled", "done"), timeout=30)
        assert t["status"] == "canceled"
        assert not out.exists(), "取消后半成品输出必须被清理"
        time.sleep(2)  # 等进程树彻底回收
        after = me.children(recursive=True)
        assert len(after) <= children_before + 1, f"取消后残留子进程: {[p.name() for p in after]}"


def test_delete_rules(client, clips):
    r = client.post("/api/tasks", json={
        "input": str(clips["tiny"]),
        "model_id": "realesr-animevideov3", "params": {},
    })
    tid = r.json()["id"]
    t = wait_status(client, tid, ("done",))
    assert client.delete(f"/api/tasks/{tid}").status_code == 200
    assert client.get(f"/api/tasks/{tid}").status_code == 404
