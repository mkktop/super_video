import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

_DML_CACHE: bool | None = None


@pytest.fixture(autouse=True, scope="module")
def _isolated_sv_db(tmp_path_factory):
    """每个测试模块独享 SV_DB。

    历史问题：多个模块在 import 期（collection 阶段）改写 os.environ["SV_DB"]，
    全量运行时所有模块实际共用字母序最后一个模块的库——"每模块独立库"只在
    单文件运行时成立，跨模块状态污染只是还没咬人。db.db_path() 每次调用
    实时读 env，这里在每模块的测试期覆盖成独一文件，import 期的赋值被
    运行期覆盖，隔离真实生效（模块自带的 client fixture 会 unlink 本值）。
    """
    old = os.environ.get("SV_DB")
    os.environ["SV_DB"] = str(
        tmp_path_factory.mktemp(f"svdb_{uuid.uuid4().hex[:6]}") / "tasks.db")
    yield
    if old is None:
        os.environ.pop("SV_DB", None)
    else:
        os.environ["SV_DB"] = old


@pytest.fixture(autouse=True, scope="session")
def _no_local_sidecar_token():
    """API 测试一律视为无本地令牌（会话级早于任何模块级 client 夹具生效）。

    开发机常有正在运行的 sidecar 在 DATA_ROOT/.tmp/sidecar.token 落了令牌，
    鉴权中间件按请求时读到它就会把无令牌的 TestClient 请求全判 401——
    测试是否通过取决于"机器上是否恰好在跑应用"，这种环境耦合必须掐断。
    """
    from sv.server import app as _app

    orig = _app._expected_tokens
    _app._expected_tokens = lambda: []
    yield
    _app._expected_tokens = orig


def dml_available() -> bool:
    """当前机器能否创建 DML 会话（无显卡机器/CI runner 为 False）。"""
    global _DML_CACHE
    if _DML_CACHE is None:
        try:
            import numpy as np
            import onnxruntime as ort

            s = ort.InferenceSession(
                str(Path(__file__).parent.parent / "sv" / "models" / "bundled"
                    / "2x_AnimeJaNai_HD_V3.1_Balanced_SPANF3_b8f64_unshuffle_fp16.onnx"),
                providers=["DmlExecutionProvider"],
            )
            # V3.1 原生 fp16 模型（tensor(float16) 进出），探测帧须用 float16
            s.run(None, {"input": np.zeros((1, 3, 64, 64), np.float16)})
            _DML_CACHE = "DmlExecutionProvider" in s.get_providers()
        except Exception:  # noqa: BLE001 — 任何失败都视为无 DML
            _DML_CACHE = False
    return _DML_CACHE
