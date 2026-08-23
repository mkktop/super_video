import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_DML_CACHE: bool | None = None


def dml_available() -> bool:
    """当前机器能否创建 DML 会话（无显卡机器/CI runner 为 False）。"""
    global _DML_CACHE
    if _DML_CACHE is None:
        try:
            import numpy as np
            import onnxruntime as ort

            s = ort.InferenceSession(
                str(Path(__file__).parent.parent / "sv" / "models" / "bundled"
                    / "RealESR-AnimeVideo-v3_x4.onnx"),
                providers=["DmlExecutionProvider"],
            )
            s.run(None, {"input": np.zeros((1, 3, 64, 64), np.float32)})
            _DML_CACHE = "DmlExecutionProvider" in s.get_providers()
        except Exception:  # noqa: BLE001 — 任何失败都视为无 DML
            _DML_CACHE = False
    return _DML_CACHE
