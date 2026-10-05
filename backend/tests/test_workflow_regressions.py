"""输出保护、默认路径预览、WS 慢消费者重连的回归测试（不运行推理）。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from sv.server import app as app_module, db, settings
from sv.server.events import EventBus
from sv.server.routes import tasks


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SV_DB", str(tmp_path / "tasks.db"))
    db.init_db()
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "settings.json")
    info = SimpleNamespace(width=640, height=360, fps=24, total_frames=24)
    monkeypatch.setattr(tasks, "probe", lambda _: info)
    monkeypatch.setattr(tasks, "validate_m0", lambda _: None)
    with TestClient(app_module.app, raise_server_exceptions=True) as c:
        # 保持新任务排队，避免启动模型下载和推理。
        monkeypatch.setattr(db, "next_queued", lambda: None)
        yield c


def body(src, **extra):
    return {"input": str(src), "model_id": "realesr-animevideov3",
            "params": {"scale": 2, "target_scale": 2}, **extra}


def test_auto_name_skips_all_existing_suffixes(tmp_path):
    for name in ("clip.mp4", "clip_2x.mp4", "clip_2x_2.mp4"):
        (tmp_path / name).write_bytes(b"keep")
    out = tasks._sr_output_name(tmp_path, "clip", "mp4", "2x", set())
    assert Path(out).name == "clip_2x_3.mp4"
    assert all(p.read_bytes() == b"keep" for p in tmp_path.iterdir())


def test_overwrite_rejects_active_output_even_with_path_alias(client, tmp_path):
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"source")
    out = tmp_path / "out.mp4"
    first = client.post("/api/tasks", json=body(src, output=str(out)))
    assert first.status_code == 201
    alias = str(tmp_path / "sub" / ".." / "out.mp4")
    second = client.post("/api/tasks", json=body(src, output=alias, overwrite=True))
    assert second.status_code == 409
    assert len(db.list_tasks()) == 1


def test_explicit_overwrite_only_allows_inactive_file(client, tmp_path):
    src, out = tmp_path / "clip.mp4", tmp_path / "out.mp4"
    src.write_bytes(b"source")
    out.write_bytes(b"old")
    assert client.post("/api/tasks", json=body(src, output=str(out))).status_code == 409
    assert client.post("/api/tasks", json=body(src, output=str(out), overwrite=True)).status_code == 201
    assert client.post("/api/tasks", json=body(src, output=str(src), overwrite=True)).status_code == 400
    assert src.read_bytes() == b"source"


def test_concurrent_creation_reserves_distinct_output_paths(client, tmp_path):
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"source")
    request = tasks.TaskCreate(**body(src))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: tasks.create_task(request), range(4)))
    assert len({t["output_path"] for t in results}) == 4
    assert len(db.list_tasks()) == 4


def test_preview_and_creation_use_template_without_reserving(client, tmp_path):
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"source")
    settings.save({"output_name_template": "{name}_{model}_{res}"})
    request = body(src)
    suggested = client.post("/api/tasks/output-path", json=request)
    assert suggested.status_code == 200
    path = suggested.json()["output"]
    assert Path(path).name == "clip_realesr-animevideov3_1280x720.mp4"
    assert db.list_tasks() == []
    created = client.post("/api/tasks", json=request)
    assert created.status_code == 201
    assert created.json()["output_path"] == path
    # 第一次创建后，新预览必须避让该活动任务，且不生成磁盘产物。
    again = client.post("/api/tasks/output-path", json=request).json()["output"]
    assert again != path
    assert not Path(path).exists()


def test_batch_image_outputs_are_all_reserved(tmp_path):
    db.init_db()
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    db.new_task("input", str(a), "model", {"images": [{"out": str(a)}, {"out": str(b)}]})
    chosen = tasks._sr_output_name(tmp_path, "b", "png", "2x", tasks._active_output_keys())
    assert Path(chosen) != b


def test_overflow_signals_disconnect_instead_of_dead_subscription():
    async def run():
        bus = EventBus()
        slow, fast = bus.subscribe(), bus.subscribe()
        for i in range(257):
            bus.publish({"type": "progress", "frames": i})
            fast.get_nowait()
        assert slow.get_nowait() == {"type": "_bus_overflow"}
        assert slow not in bus._queues
        bus.publish({"type": "task_status", "status": "done"})
        assert fast.get_nowait()["status"] == "done"
    asyncio.run(run())


def test_websocket_closes_with_retry_code_on_overflow(monkeypatch):
    bus = EventBus()
    subscribe = bus.subscribe

    def overflowing_subscribe():
        q = subscribe()
        for i in range(257):
            bus.publish({"type": "progress", "frames": i})
        return q

    monkeypatch.setattr(bus, "subscribe", overflowing_subscribe)
    monkeypatch.setattr(app_module, "bus", bus)
    with TestClient(app_module.app) as c:
        with c.websocket_connect("/ws") as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
            assert exc.value.code == 1013
