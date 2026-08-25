"""SQLite 任务表：M1 队列的持久化层（stdlib sqlite3，无 ORM）。"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

from ..paths import TEMP_DIR

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  input_path TEXT NOT NULL,
  output_path TEXT NOT NULL,
  model_id TEXT NOT NULL,
  params TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'queued',   -- queued|running|done|failed|canceled
  src_w INTEGER DEFAULT 0, src_h INTEGER DEFAULT 0,
  fps REAL DEFAULT 0, total_frames INTEGER DEFAULT 0,
  progress_frames INTEGER DEFAULT 0,
  fps_run REAL DEFAULT 0, eta_sec REAL DEFAULT 0,
  error TEXT, preview_path TEXT,
  out_bytes INTEGER DEFAULT 0, elapsed_s REAL DEFAULT 0,
  queue_order REAL DEFAULT 0             -- 排队顺序（拖拽排序）；running 取任务时保留原值
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, created_at);
"""


def db_path() -> Path:
    env = os.environ.get("SV_DB")
    return Path(env).resolve() if env else TEMP_DIR / "sidecar.db"


def connect() -> sqlite3.Connection:
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def db_conn():
    """事务 + 必关连接：`with db_conn() as c:` 提交后关闭（sqlite3 的 with 只提交不关闭）。"""
    conn = connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    with db_conn() as c:
        c.executescript(_SCHEMA)
        # 轻量迁移：老库补列（已存在则忽略）
        for ddl in (
            "ALTER TABLE tasks ADD COLUMN preview_src TEXT",
            "ALTER TABLE tasks ADD COLUMN elapsed_s REAL DEFAULT 0",
            "ALTER TABLE tasks ADD COLUMN queue_order REAL DEFAULT 0",
        ):
            try:
                c.execute(ddl)
            except sqlite3.OperationalError:
                pass
        # queue_order 回填为创建时间：老库保持原有 FIFO 相对顺序
        c.execute("UPDATE tasks SET queue_order = created_at WHERE queue_order = 0")


def new_task(input_path: str, output_path: str, model_id: str, params: dict,
             src: dict | None = None) -> dict:
    task_id = uuid.uuid4().hex[:12]
    now = time.time()
    src = src or {}
    with db_conn() as c:
        qo = c.execute("SELECT COALESCE(MAX(queue_order), 0) + 1 FROM tasks").fetchone()[0]
        c.execute(
            "INSERT INTO tasks (id, created_at, updated_at, input_path, output_path,"
            " model_id, params, status, src_w, src_h, fps, total_frames, queue_order)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, now, now, input_path, output_path, model_id,
             json.dumps(params, ensure_ascii=False), "queued",
             src.get("w", 0), src.get("h", 0), src.get("fps", 0.0),
             src.get("total_frames", 0), qo),
        )
    return get_task(task_id)


def _row2dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["params"] = json.loads(d["params"] or "{}")
    return d


def get_task(task_id: str) -> dict | None:
    with db_conn() as c:
        r = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return _row2dict(r) if r else None


def list_tasks(history_limit: int = 100) -> list[dict]:
    """任务列表：运行/排队任务全量在前（按 queue_order），历史（done/failed/canceled）
    只取最近 history_limit 条（按创建时间倒序）——列表渲染成本不随使用时间增长。
    首页统计要全量数字，走 stats() 聚合，别数这里。"""
    with db_conn() as c:
        active = c.execute(
            "SELECT * FROM tasks WHERE status IN ('running','queued')"
            " ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, queue_order ASC"
        ).fetchall()
        history = c.execute(
            "SELECT * FROM tasks WHERE status NOT IN ('running','queued')"
            " ORDER BY created_at DESC LIMIT ?",
            (history_limit,),
        ).fetchall()
    return [_row2dict(r) for r in [*active, *history]]


def stats() -> dict:
    """全量聚合统计（首页四宫格），不受 list_tasks 历史上限影响。"""
    with db_conn() as c:
        r = c.execute(
            "SELECT COUNT(*) AS total,"
            " COALESCE(SUM(CASE WHEN status='done' THEN 1 ELSE 0 END), 0) AS done,"
            " COALESCE(SUM(CASE WHEN status='done' THEN total_frames ELSE 0 END), 0) AS frames,"
            " COALESCE(SUM(CASE WHEN status='done' THEN out_bytes ELSE 0 END), 0) AS bytes"
            " FROM tasks"
        ).fetchone()
    return {"total": r["total"], "done": r["done"], "frames": r["frames"], "bytes": r["bytes"]}


def next_queued() -> dict | None:
    """取下一个排队任务（按 queue_order，即用户拖拽后的顺序），原子置为 running。"""
    with db_conn() as c:
        r = c.execute(
            "SELECT * FROM tasks WHERE status='queued'"
            " ORDER BY queue_order ASC LIMIT 1"
        ).fetchone()
        if r is None:
            return None
        c.execute(
            "UPDATE tasks SET status='running', updated_at=? WHERE id=? AND status='queued'",
            (time.time(), r["id"]),
        )
        if c.total_changes == 0:
            return None  # 并发竞争下被别人取走
        return _row2dict(r)


def reorder_queued(ids: list[str]) -> int:
    """按传入顺序重排排队任务（queue_order=1..n）；运行中/已结束/不存在的 id 跳过。"""
    n = 0
    with db_conn() as c:
        for i, tid in enumerate(ids, 1):
            cur = c.execute(
                "UPDATE tasks SET queue_order=?, updated_at=? WHERE id=? AND status='queued'",
                (i, time.time(), tid),
            )
            n += cur.rowcount
    return n


def update_task(task_id: str, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    with db_conn() as c:
        c.execute(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), task_id))


def recover_running() -> int:
    """启动恢复：上次残留的 running 任务回到队列。"""
    with db_conn() as c:
        cur = c.execute(
            "UPDATE tasks SET status='queued', updated_at=? WHERE status='running'",
            (time.time(),),
        )
        return cur.rowcount


def delete_task(task_id: str) -> bool:
    with db_conn() as c:
        cur = c.execute("DELETE FROM tasks WHERE id=? AND status!='running'", (task_id,))
        return cur.rowcount > 0


def all_task_ids() -> set[str]:
    """全表任务 id（孤儿目录清扫用；list_tasks 有历史上限会漏）。"""
    with db_conn() as c:
        return {r[0] for r in c.execute("SELECT id FROM tasks")}
