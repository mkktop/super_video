"""db.schema 版本化迁移：user_version 从 0→1 的补列与幂等。"""
import sqlite3

import pytest

from sv.server import db


def _columns(conn: sqlite3.Connection) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}


def _user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def test_fresh_db_gets_version_mark(tmp_path, monkeypatch):
    """全新库：建表 + 补列 + 置 user_version=1。"""
    monkeypatch.setenv("SV_DB", str(tmp_path / "fresh.db"))
    db.init_db()
    conn = sqlite3.connect(tmp_path / "fresh.db")
    cols = _columns(conn)
    assert {"preview_src", "elapsed_s", "queue_order", "fps_avg"} <= cols
    assert _user_version(conn) == 1
    conn.close()


def test_legacy_db_backfill_and_mark(tmp_path, monkeypatch):
    """v0 老库（无后补列、无版本标记）：补列 + queue_order 回填 + 置版本。"""
    p = tmp_path / "legacy.db"
    conn = sqlite3.connect(p)
    conn.executescript("""
        CREATE TABLE tasks (
          id TEXT PRIMARY KEY, created_at REAL, updated_at REAL,
          input_path TEXT, output_path TEXT, model_id TEXT,
          params TEXT DEFAULT '{}', status TEXT DEFAULT 'queued',
          src_w INTEGER, src_h INTEGER, fps REAL, total_frames INTEGER,
          progress_frames INTEGER, fps_run REAL, eta_sec REAL,
          error TEXT, preview_path TEXT, out_bytes INTEGER
        );
    """)
    conn.execute(
        "INSERT INTO tasks (id, created_at, updated_at, input_path, output_path, model_id,"
        " status) VALUES ('t1', 100.0, 100.0, 'in', 'out', 'm', 'done')"
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("SV_DB", str(p))
    db.init_db()

    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    cols = _columns(conn)
    assert {"preview_src", "elapsed_s", "queue_order", "fps_avg"} <= cols
    row = conn.execute("SELECT * FROM tasks WHERE id='t1'").fetchone()
    assert row["queue_order"] == 100.0  # 回填为创建时间，保持 FIFO
    assert _user_version(conn) == 1
    conn.close()

    # 幂等：再次 init_db 不重放迁移也不炸
    db.init_db()
    conn = sqlite3.connect(p)
    assert _user_version(conn) == 1
    conn.close()


def test_task_roundtrip_after_migration(tmp_path, monkeypatch):
    """迁移后的库可正常走业务读写（new_task → next_queued）。"""
    monkeypatch.setenv("SV_DB", str(tmp_path / "rt.db"))
    db.init_db()
    t = db.new_task("in.mp4", "out.mp4", "m", {})
    assert t["queue_order"] == 1
    got = db.next_queued()
    assert got is not None and got["id"] == t["id"]
    # next_queued 返回领取前快照（status 仍 queued），库里已是 running
    assert db.get_task(t["id"])["status"] == "running"


def test_legacy_db_missing_columns_rejected(tmp_path, monkeypatch):
    """损坏形态的老库（缺大量业务列）：迁移 UPDATE 引用缺失列抛
    OperationalError——损坏库快速失败暴露，而不是静默产出半迁移状态。"""
    p = tmp_path / "weird.db"
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    monkeypatch.setenv("SV_DB", str(p))
    with pytest.raises(sqlite3.OperationalError):
        db.init_db()
