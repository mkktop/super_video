"""任务删除清理链：工作目录/预览图随任务删除后台清理；启动清扫只动超龄孤儿目录。"""
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sv.paths import TEMP_DIR

os.environ["SV_DB"] = str(TEMP_DIR / "test_task_cleanup.db")


@pytest.fixture(scope="module")
def client():
    p = Path(os.environ["SV_DB"])
    if p.exists():
        p.unlink()
    from sv.server import app as appmod

    with TestClient(appmod.app) as c:  # 触发 lifespan: init_db + 孤儿清扫
        yield c


def _mk_canceled_task() -> str:
    from sv.server import db as svdb

    t = svdb.new_task("x.mp4", "y.mp4", "realesr-animevideov3", {})
    svdb.update_task(t["id"], status="canceled")
    return t["id"]


def test_delete_purges_task_files(client):
    """删除任务 → 工作目录(segmented/chunked)与预览图后台清掉。"""
    tid = _mk_canceled_task()
    seg = TEMP_DIR / "segmented" / tid
    seg.mkdir(parents=True, exist_ok=True)
    (seg / "seg_000.mp4").write_bytes(b"x" * 16)
    chk = TEMP_DIR / "chunked" / tid
    chk.mkdir(parents=True, exist_ok=True)
    pv = TEMP_DIR / "previews"
    pv.mkdir(parents=True, exist_ok=True)
    (pv / f"{tid}.jpg").write_bytes(b"j")
    (pv / f"{tid}_src.jpg").write_bytes(b"j")

    r = client.delete(f"/api/tasks/{tid}")
    assert r.status_code == 200

    for _ in range(50):  # 后台线程清理，轮询等待
        if not seg.exists() and not (pv / f"{tid}.jpg").exists():
            break
        time.sleep(0.1)
    assert not seg.exists()
    assert not chk.exists()
    assert not (pv / f"{tid}.jpg").exists()
    assert not (pv / f"{tid}_src.jpg").exists()


def test_sweep_orphans_only(client):
    """清扫只删「无任务行 + 超龄」的目录；在库任务目录与新目录保留。"""
    from sv.server import app as appmod

    old = TEMP_DIR / "segmented" / "orphan_old"
    old.mkdir(parents=True, exist_ok=True)
    os.utime(old, (time.time() - 7200,) * 2)
    fresh = TEMP_DIR / "segmented" / "orphan_fresh"
    fresh.mkdir(parents=True, exist_ok=True)
    tid = _mk_canceled_task()
    keep = TEMP_DIR / "segmented" / tid
    keep.mkdir(parents=True, exist_ok=True)
    os.utime(keep, (time.time() - 7200,) * 2)  # 超龄但在库（可续跑）→ 保留

    appmod.sweep_orphan_workdirs()

    assert not old.exists()
    assert fresh.exists()
    assert keep.exists()
    fresh.rmdir()  # 清理测试残留


def test_delete_running_rejected(client):
    """运行中任务不可删除（走 409，不触发清理）。"""
    from sv.server import db as svdb

    tid = _mk_canceled_task()
    svdb.update_task(tid, status="running")
    assert client.delete(f"/api/tasks/{tid}").status_code == 409
    svdb.update_task(tid, status="canceled")
    assert client.delete(f"/api/tasks/{tid}").status_code == 200
