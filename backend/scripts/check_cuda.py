"""在候选解释器内运行：验证 CUDA EP 真实可用（创建会话 + 跑一帧推理）。

用法: python check_cuda.py <bundled_model.onnx>
输出: 一行 JSON {"ok": bool, "provider": ..., "trt": bool, "error": ...}
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sv.engines.nvidia_dlls import register_nvidia_dlls


def _trt_available() -> bool:
    """TensorRT 库是否可加载（TRT EP 编译进 onnxruntime-gpu，但运行时另需 TRT 库）。"""
    try:
        import tensorrt  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def main(model_path: str) -> int:
    result = {"ok": False, "provider": None, "trt": False, "error": None}
    try:
        import numpy as np
        import onnxruntime as ort

        register_nvidia_dlls()
        if "CUDAExecutionProvider" not in ort.get_available_providers():
            result["error"] = "无 CUDAExecutionProvider（非 GPU 版 onnxruntime）"
            print(json.dumps(result))
            return 1
        result["trt"] = (
            "TensorrtExecutionProvider" in ort.get_available_providers()
            and _trt_available()
        )
        sess = ort.InferenceSession(model_path, providers=["CUDAExecutionProvider"])
        result["provider"] = ",".join(sess.get_providers())
        inp = sess.get_inputs()[0]
        x = np.zeros((1, 3, 64, 64), dtype=np.float32)
        sess.run([o.name for o in sess.get_outputs()], {inp.name: x})
        if "CUDAExecutionProvider" not in sess.get_providers():
            result["error"] = f"会话未使用 CUDA: {sess.get_providers()}"
            print(json.dumps(result))
            return 1
        result["ok"] = True
    except Exception as e:  # noqa: BLE001 — DLL 缺失/驱动问题等都要上报而非崩溃
        result["error"] = f"{type(e).__name__}: {e}"
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
