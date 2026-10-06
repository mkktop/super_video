"""runner 错误提示链路：worker 信息行不进 error 字段 / 成功任务清除残留。"""
import os
import time
from pathlib import Path

import pytest

from sv.server.runner import error_hint


def test_native_utf16_padding_does_not_drop_worker_events(_own_db):
    import asyncio
    import json
    from types import SimpleNamespace
    from sv.server.runner import Runner
    task=_own_db.new_task('in','out','model',{})
    messages=[{'type':'started','total_frames':7},
              {'type':'log','line':'escaped\x00payload'},
              {'type':'progress','frames':3,'total':7},
              {'type':'done','frames':7}]
    async def stream():
        yield 'native ORT warning\r\n'.encode('utf-16-le')
        for message in messages:
            yield b'\x00'+json.dumps(message).encode('utf-8')+b'\n'
    events=[]
    runner=Runner(SimpleNamespace(publish=events.append))
    runner.proc=SimpleNamespace(stdout=stream())
    final=asyncio.run(runner._pump_until_final(task))
    assert final==messages[-1]
    assert _own_db.get_task(task['id'])['total_frames']==7
    assert _own_db.get_task(task['id'])['progress_frames']==3
    assert next(e for e in events if e['type']=='log')['line']=='escaped\x00payload'


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
