"""处理时机（定时/闲时）：时段判断单测、设置校验、runner 闸门挂起/放行/状态透出。"""
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from sv.paths import TEMP_DIR, ffmpeg_bin
from sv.server.runner import _in_window, _parse_hhmm, _windows_idle_seconds
from sv.utils.process import WINDOWS_CREATE_FLAGS


# ---- HH:MM 解析与时段判断 ----


def test_parse_hhmm():
    assert _parse_hhmm("22:00") == (22, 0)
    assert _parse_hhmm("8:05") == (8, 5)
    assert _parse_hhmm("00:00") == (0, 0)
    for bad in ("24:00", "12:60", "ab:cd", "2200", "", None, "12:0:3"):
        assert _parse_hhmm(bad) is None, bad


def test_in_window_regular():
    assert _in_window((12, 0), (9, 0), (18, 0)) is True
    assert _in_window((8, 59), (9, 0), (18, 0)) is False
    assert _in_window((18, 0), (9, 0), (18, 0)) is False  # 右开区间
    assert _in_window((9, 0), (9, 0), (18, 0)) is True


def test_in_window_cross_midnight():
    """22:00~08:00 夜间段：23 点/凌晨 3 点在内，中午不在。"""
    assert _in_window((23, 0), (22, 0), (8, 0)) is True
    assert _in_window((3, 0), (22, 0), (8, 0)) is True
    assert _in_window((12, 0), (22, 0), (8, 0)) is False
    assert _in_window((8, 0), (22, 0), (8, 0)) is False  # 结束点即出窗


def test_in_window_degenerate_all_day():
    assert _in_window((0, 0), (8, 0), (8, 0)) is True
    assert _in_window((23, 59), (8, 0), (8, 0)) is True


def test_idle_seconds_smoke():
    """无头环境下拿不到输入时间轴时按 0（不空闲）——闸门永不误挂起。"""
    v = _windows_idle_seconds()
    assert isinstance(v, float) and v >= 0


# ---- 设置校验 ----


def test_settings_validation(client):
    c = client["client"]
    assert c.put("/api/settings", json={"queue_schedule": "sometimes"}).status_code == 400
    assert c.put("/api/settings", json={"schedule_start": "25:00"}).status_code == 400
    assert c.put("/api/settings", json={"schedule_end": "8点"}).status_code == 400
    assert c.put("/api/settings", json={"idle_minutes": 0}).status_code == 400
    assert c.put("/api/settings", json={"idle_minutes": True}).status_code == 400
    r = c.put("/api/settings", json={"queue_schedule": "window",
                                     "schedule_start": "22:30",
                                     "schedule_end": "07:15",
                                     "idle_minutes": 30})
    assert r.status_code == 200
    assert r.json()["queue_schedule"] == "window"
    assert r.json()["schedule_start"] == "22:30"


# ---- runner 闸门集成：挂起 → 放行 → 状态透出 ----


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SV_DB", str(tmp_path / "qs.db"))
    from sv.server import settings as sv_settings

    monkeypatch.setattr(sv_settings, "SETTINGS_PATH", tmp_path / "settings.json")

    from sv.server import db as sv_db

    sv_db.init_db()

    from sv.server import runner as runner_mod
    import sv.server.app as app_mod

    # 闲时判定恒为"不空闲"（键鼠刚动过）：闸门确定性地关闭
    monkeypatch.setattr(runner_mod, "_windows_idle_seconds", lambda: 0.0)

    async def fake_run_one(self, task):
        sv_db.update_task(task["id"], status="done")
        self.bus.publish({"type": "task_status", "task_id": task["id"],
                          "status": "done"})

    monkeypatch.setattr(runner_mod.Runner, "_run_one", fake_run_one)

    clip = tmp_path / "tiny.mp4"
    subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=24", "-t", "0.5",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)],
        check=True, creationflags=WINDOWS_CREATE_FLAGS,
    )
    with TestClient(app_mod.app) as c:
        yield {"client": c, "clip": str(clip)}


def _stats_gate(c):
    return c.get("/api/stats").json()["queue_gate"]


def test_gate_suspends_and_resumes(client):
    c = client["client"]
    assert c.put("/api/settings", json={"queue_schedule": "idle", "idle_minutes": 1}).status_code == 200
    r = c.post("/api/tasks", json={"input": client["clip"],
                                   "model_id": "realesr-animevideov3", "params": {}})
    assert r.status_code == 201
    tid = r.json()["id"]
    # 闸门关着（idle=0 < 1 分钟）：任务停在 queued，不开始
    deadline = time.time() + 8
    while time.time() < deadline and not _stats_gate(c).get("active") is False:
        time.sleep(0.3)
    assert _stats_gate(c)["active"] is False
    assert "空闲" in _stats_gate(c)["reason"]
    t = c.get(f"/api/tasks/{tid}").json()
    assert t["status"] == "queued", "闸门挂起期间不得开始任务"

    # 改回立即处理：任务被领走并（假 worker）完成，闸门回开放态
    assert c.put("/api/settings", json={"queue_schedule": "always"}).status_code == 200
    deadline = time.time() + 10
    status = ""
    while time.time() < deadline:
        status = c.get(f"/api/tasks/{tid}").json()["status"]
        if status == "done":
            break
        time.sleep(0.3)
    assert status == "done", f"放行后应跑完，当前 {status}"
    deadline = time.time() + 8
    while time.time() < deadline and _stats_gate(c).get("active") is not True:
        time.sleep(0.3)
    assert _stats_gate(c)["active"] is True


def test_window_mode_gate_reason(client, monkeypatch):
    """window 模式闸门关闭：原因带时段文案（_in_window 判定另有单测，这里桩掉保确定性）。"""
    monkeypatch.setattr("sv.server.runner._in_window", lambda *a: False)
    c = client["client"]
    assert c.put("/api/settings", json={"queue_schedule": "window",
                                        "schedule_start": "22:00",
                                        "schedule_end": "08:00"}).status_code == 200
    deadline = time.time() + 8
    while time.time() < deadline and _stats_gate(c).get("active") is not False:
        time.sleep(0.3)
    g = _stats_gate(c)
    assert g["active"] is False
    assert "时段" in g["reason"] and "22:00" in g["reason"]
