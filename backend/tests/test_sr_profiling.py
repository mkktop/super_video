"""sr_profiling 开关：性能日志落盘 / sr-log 接口 / fps_avg 平均速度字段。"""
import os
import subprocess
import time

import pytest

from sv.paths import TEMP_DIR, ffmpeg_bin
from sv.server import db
from sv.server import settings as settings_mod
from sv.server import worker as worker_mod
from sv.server import worker_common
from sv.server.runner import fps_avg
from sv.utils.process import WINDOWS_CREATE_FLAGS


# ---- 设置项 ----


def test_sr_profiling_settings_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", tmp_path / "settings.json")
    assert settings_mod.load()["sr_profiling"] is False  # 默认关
    assert settings_mod.save({"sr_profiling": True})["sr_profiling"] is True
    assert settings_mod.load()["sr_profiling"] is True  # 落盘可回读
    with pytest.raises(ValueError):
        settings_mod.save({"sr_profiling": "yes"})


# ---- fps_avg 口径（runner 完成事件落定） ----


def test_fps_avg_math():
    assert fps_avg(300, 10.0) == 30.0
    assert fps_avg(0, 10.0) == 0.0
    assert fps_avg(100, 0) == 0.0
    assert fps_avg(1, 3.0) == 0.33  # 两位小数，够对比基准用


# ---- worker 日志写入 / 明细并入 ----


def test_prof_write_and_collect(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_common, "SR_LOG_DIR", tmp_path)
    worker_mod._prof_write("t1", "头部\n")
    worker_mod._prof_write("t1", "尾部\n")
    f = tmp_path / "t1.log"
    assert f.read_text(encoding="utf-8") == "头部\n尾部\n"

    # _prof_collect：工作目录里的 perf_stages.jsonl 并入日志，目录保留原样处理
    work = tmp_path / "seg_t2"
    work.mkdir()
    (work / "perf_stages.jsonl").write_text('{"seg":0,"frames":8}\n', encoding="utf-8")
    worker_mod._prof_write("t2", "==== 头 ====\n")
    worker_mod._prof_collect("t2", work)
    body = (tmp_path / "t2.log").read_text(encoding="utf-8")
    assert "==== 头 ====" in body and '{"seg":0,"frames":8}' in body
    # 无明细文件时不炸也不写
    worker_mod._prof_collect("t3", tmp_path)


def test_sr_log_api_and_annotation(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_common, "SR_LOG_DIR", tmp_path)
    from fastapi.testclient import TestClient

    from sv.server import app as app_mod
    from sv.server.routes import tasks as tasks_mod

    monkeypatch.setattr(tasks_mod, "SR_LOG_DIR", tmp_path)
    db.init_db()
    task = db.new_task("in.mp4", "out.mp4", "realesr-animevideov3", {})
    assert task["fps_avg"] == 0  # 新库迁移出 fps_avg 列且默认 0

    with TestClient(app_mod.app) as client:  # lifespan：init_db + runner 起调度
        listed = {t["id"]: t for t in client.get("/api/tasks").json()}
        assert listed[task["id"]]["has_sr_log"] is False

        worker_mod._prof_write(task["id"], "==== 运行开始 ====\n")
        listed = {t["id"]: t for t in client.get("/api/tasks").json()}
        assert listed[task["id"]]["has_sr_log"] is True

        r = client.get(f"/api/tasks/{task['id']}/sr-log")
        assert r.status_code == 200 and "运行开始" in r.text
        assert client.get("/api/tasks/nope/sr-log").status_code == 404


# ---- 启动 GC：性能日志只留最近 N 份 ----


def test_gc_sr_logs(tmp_path, monkeypatch):
    from sv.server.app import gc_sr_logs

    monkeypatch.setattr(worker_common, "SR_LOG_DIR", tmp_path)
    from sv.server import app as app_mod
    from sv.server.routes import tasks as tasks_mod

    monkeypatch.setattr(tasks_mod, "SR_LOG_DIR", tmp_path)
    for i in range(5):
        p = tmp_path / f"t{i}.log"
        p.write_text("x", encoding="utf-8")
        os.utime(p, (time.time() - 100 + i, time.time() - 100 + i))
    assert gc_sr_logs(keep=3) == 2
    remain = {p.name for p in tmp_path.iterdir()}
    assert remain == {"t2.log", "t3.log", "t4.log"}  # 最旧的 t0/t1 被清


# ---- 端到端：开关开启时跑真任务，日志落盘且工作目录被清理 ----


@pytest.mark.skipif(not __import__("conftest").dml_available(),
                    reason="需要 DML 可用（真模型推理）")
def test_worker_writes_prof_log(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_common, "SR_LOG_DIR", tmp_path)
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", tmp_path / "settings.json")
    settings_mod.save({"sr_profiling": True})

    clip = TEMP_DIR / "srprof_in.mp4"
    subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=24",
         "-f", "lavfi", "-i", "sine=frequency=440",
         "-t", "2", "-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(clip)],
        check=True, creationflags=WINDOWS_CREATE_FLAGS,
    )
    db.init_db()
    task = db.new_task(str(clip), str(tmp_path / "out.mp4"),
                       "realesr-animevideov3", {})
    rc = worker_mod.main(task["id"])
    assert rc == 0

    log = (tmp_path / f"{task['id']}.log").read_text(encoding="utf-8")
    assert "运行开始" in log and "引擎加载" in log
    assert "分段耗时明细" in log  # perf_stages.jsonl 已并入
    assert "运行结束" in log and "fps" in log
    # 剖析模式下工作目录在收尾后被清掉（不留垃圾）
    assert not (TEMP_DIR / "segmented" / task["id"]).exists()
    (tmp_path / "out.mp4").unlink(missing_ok=True)
