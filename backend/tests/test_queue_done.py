"""队列完成后动作：布防/倒计时/关机休眠执行、三类撤销路径、设置校验。"""
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sv.paths import ffmpeg_bin
from sv.utils.process import WINDOWS_CREATE_FLAGS


@pytest.fixture(scope="module")
def tiny_clip(tmp_path_factory):
    d = tmp_path_factory.mktemp("qd_clip")
    p = d / "tiny.mp4"
    subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=24",
         "-t", "0.5", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(p)],
        check=True, creationflags=WINDOWS_CREATE_FLAGS,
    )
    return p


@pytest.fixture()
def env(tmp_path, monkeypatch, tiny_clip):
    """隔离设置文件与任务库；记录总线事件与电源动作（不真关机）。"""
    monkeypatch.setenv("SV_DB", str(tmp_path / "qd.db"))
    from sv.server import settings as sv_settings

    monkeypatch.setattr(sv_settings, "SETTINGS_PATH", tmp_path / "settings.json")

    from sv.server import db as sv_db

    sv_db.init_db()

    from sv.server import runner as runner_mod
    import sv.server.app as app_mod  # noqa: ICN001 — 需要 bus/runner 单例所在的模块本体

    events: list[dict] = []
    orig_publish = app_mod.bus.publish

    def rec_publish(ev):
        events.append(ev)
        orig_publish(ev)

    monkeypatch.setattr(app_mod.bus, "publish", rec_publish)

    fired: list[str] = []
    monkeypatch.setattr(runner_mod, "_execute_power_action",
                        lambda action: (fired.append(action), True)[1])
    monkeypatch.setattr(runner_mod, "QUEUE_ACTION_GRACE_S", 60)

    # 假 worker：任务即领即 done（跑真实调度循环，跳过推理）
    async def fake_run_one(self, task):
        sv_db.update_task(task["id"], status="done")
        self.bus.publish({"type": "task_status", "task_id": task["id"],
                          "status": "done"})

    monkeypatch.setattr(runner_mod.Runner, "_run_one", fake_run_one)

    with TestClient(app_mod.app) as c:
        yield {
            "client": c, "events": events, "fired": fired,
            "runner": app_mod.runner, "mod": runner_mod,
        }


def _events_of(env, etype):
    return [e for e in env["events"] if e.get("type") == etype]


def _post_task(env, tiny_clip, model="realesr-animevideov3"):
    """建任务（假 worker 秒完成，不真推理；probe 走真实小视频）。"""
    c = env["client"]
    r = c.post("/api/tasks", json={"input": str(tiny_clip), "model_id": model,
                                   "params": {}})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _wait(cond, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.1)
    return False


def test_settings_validation(env):
    c = env["client"]
    assert c.put("/api/settings", json={"queue_done_action": "reboot"}).status_code == 400
    for v in ("none", "notify", "shutdown", "sleep"):
        r = c.put("/api/settings", json={"queue_done_action": v})
        assert r.status_code == 200 and r.json()["queue_done_action"] == v
    # 默认值
    from sv.server.settings import DEFAULTS

    assert DEFAULTS["queue_done_action"] == "none"


def test_has_active(tmp_path, monkeypatch):
    monkeypatch.setenv("SV_DB", str(tmp_path / "ha.db"))
    from sv.server import db

    db.init_db()
    assert db.has_active() is False
    t = db.new_task("i", "o", "m", {})
    assert db.has_active() is True
    db.update_task(t["id"], status="done")
    assert db.has_active() is False


def test_none_action_never_arms(env, tiny_clip):
    _post_task(env, tiny_clip)
    assert _wait(lambda: any(
        e.get("type") == "task_status" and e.get("status") == "done"
        for e in env["events"]))
    time.sleep(0.8)  # 超过布防检查点，确认零事件
    assert not _events_of(env, "queue_done")
    assert env["fired"] == []


def test_shutdown_fires_after_grace(env, tiny_clip, monkeypatch):
    monkeypatch.setattr(env["mod"], "QUEUE_ACTION_GRACE_S", 0.3)
    assert env["client"].put("/api/settings",
                             json={"queue_done_action": "shutdown"}).status_code == 200
    _post_task(env, tiny_clip)
    assert _wait(lambda: _events_of(env, "queue_done")), "倒计时应布防"
    ev = _events_of(env, "queue_done")[-1]
    assert ev["action"] == "shutdown" and ev["grace_s"] == 0.3
    assert _wait(lambda: env["fired"] == ["shutdown"]), "倒计时走完应执行关机动作"
    assert _wait(lambda: _events_of(env, "queue_done_fired"))


def test_manual_cancel_during_grace(env, tiny_clip):
    assert env["client"].put("/api/settings",
                             json={"queue_done_action": "shutdown"}).status_code == 200
    _post_task(env, tiny_clip)
    assert _wait(lambda: _events_of(env, "queue_done"))
    r = env["client"].post("/api/queue-done/cancel").json()
    assert r["ok"] is True
    assert _wait(lambda: _events_of(env, "queue_done_canceled")), "应广播取消"
    time.sleep(1.0)  # 宽限期名义 60s；确认不再执行
    assert env["fired"] == []
    # 无倒计时时幂等成功
    assert env["client"].post("/api/queue-done/cancel").json()["ok"] is False


def test_new_task_cancels_countdown(env, tiny_clip):
    assert env["client"].put("/api/settings",
                             json={"queue_done_action": "sleep"}).status_code == 200
    _post_task(env, tiny_clip)
    assert _wait(lambda: _events_of(env, "queue_done"))
    _post_task(env, tiny_clip)  # 新任务入队：撤销 + 假 worker 又跑完 → 重新布防
    assert _wait(lambda: len(_events_of(env, "queue_done_canceled")) >= 1)
    assert _wait(lambda: len(_events_of(env, "queue_done")) >= 2), "队列再次排空应重新布防"
    assert env["fired"] == []  # 60s 宽限期内测试结束，未执行


def test_setting_change_to_none_cancels(env, tiny_clip):
    assert env["client"].put("/api/settings",
                             json={"queue_done_action": "shutdown"}).status_code == 200
    _post_task(env, tiny_clip)
    assert _wait(lambda: _events_of(env, "queue_done"))
    assert env["client"].put("/api/settings",
                             json={"queue_done_action": "none"}).status_code == 200
    assert _wait(lambda: _events_of(env, "queue_done_canceled"))
    time.sleep(0.8)
    assert env["fired"] == []


def test_notify_action_immediate_no_power(env, tiny_clip):
    assert env["client"].put("/api/settings",
                             json={"queue_done_action": "notify"}).status_code == 200
    _post_task(env, tiny_clip)
    assert _wait(lambda: _events_of(env, "queue_done"))
    ev = _events_of(env, "queue_done")[-1]
    assert ev["action"] == "notify" and ev["grace_s"] == 0
    assert _wait(lambda: _events_of(env, "queue_done_fired"))
    assert env["fired"] == []  # 通知不碰电源
