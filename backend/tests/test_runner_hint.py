"""runner 错误提示链路：worker 信息行不进 error 字段 / 成功任务清除残留。"""
import os
import time
from pathlib import Path

import pytest

from sv.server.runner import error_hint


def test_engine_info_lines_are_not_errors():
    """[engine] 前缀是引擎状态日志（u8 包装生效/后端回退），不能充当任务错误。"""
    assert error_hint("[engine] u8 包装生效（前后处理 GPU 化）: 2x_AnimeJaNai_HD_V3_UltraCompact_fp16_u8.onnx") is None
    assert error_hint("[engine] TensorRT 初始化失败(...)，回退 CUDA/CPU") is None
    assert error_hint("") is None


def test_real_errors_still_captured():
    assert error_hint("Traceback (most recent call last):") == "Traceback (most recent call last):"
    long = "E" * 500
    assert error_hint(long) == "E" * 300  # 超长截断到 300


@pytest.fixture(autouse=True)
def _own_db(tmp_path, monkeypatch):
    old = os.environ.get("SV_DB")
    monkeypatch.setenv("SV_DB", str(tmp_path / "runner_hint.db"))
    from sv.server import db

    db.init_db()
    yield db


def test_done_update_clears_stale_error(_own_db):
    """done 落库带 error=None：运行期残留的日志尾部被清掉（任务卡不再误显错误）。"""
    db = _own_db
    with db.db_conn() as c:
        c.execute(
            "INSERT INTO tasks (id, created_at, updated_at, input_path, output_path,"
            " model_id, params, status, error) VALUES ('t1',?,?, 'in','out','m','{}',"
            "'running', 'stale hint')",
            (time.time(), time.time()),
        )
    db.update_task("t1", status="done", error=None)
    assert db.get_task("t1")["error"] is None
