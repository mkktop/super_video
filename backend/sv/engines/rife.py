"""RIFE 光流补帧（vs-mlrt implementation=2 导出版）。

ONNX 输入 [N,7,H,W] float32 0-1 RGB：I0(3ch) + I1(3ch) + timestep(1ch)，
输出中间帧 [N,3,H,W]。2x 补帧固定 t=0.5。
校准记录（2026-08-23）：t=0 重构 I0 PSNR 45.7dB，t=1 重构 I1 38.9dB。

外部接口用 BGR uint8（与管线一致），内部转 RGB float。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np


class Rife2x:
    scale = 1  # 补帧不改分辨率

    def __init__(self, model_path: str | Path):
        self.model_path = Path(model_path)
        self.session = None
        self.provider_used: list[str] = []

    def load(self) -> None:
        import onnxruntime as ort

        from .nvidia_dlls import register_nvidia_dlls

        register_nvidia_dlls()
        available = set(ort.get_available_providers())
        chosen = [p for p in ("CUDAExecutionProvider", "DmlExecutionProvider",
                              "CPUExecutionProvider") if p in available]
        self.session = ort.InferenceSession(str(self.model_path), providers=chosen)
        self.provider_used = self.session.get_providers()

    def _run(self, i0: np.ndarray, i1: np.ndarray, t: float) -> np.ndarray:
        """i0/i1: [N,3,H,W] RGB 0-1。返回同布局中间帧。"""
        n, _, h, w = i0.shape
        tt = np.full((n, 1, h, w), t, dtype=np.float32)
        x = np.concatenate([i0, i1, tt], axis=1)
        return self.session.run(None, {"input": x})[0]

    def interpolate(self, frame0: np.ndarray, frame1: np.ndarray) -> np.ndarray:
        """两帧 BGR uint8 -> 中间帧 BGR uint8。"""
        def to_chw_rgb(f: np.ndarray) -> np.ndarray:
            return (np.ascontiguousarray(f[..., ::-1]).transpose(2, 0, 1)[None] / 255.0).astype(np.float32)

        mid = self._run(to_chw_rgb(frame0), to_chw_rgb(frame1), 0.5)
        out = (np.clip(mid[0].transpose(1, 2, 0), 0, 1) * 255).round().astype(np.uint8)
        return out[..., ::-1].copy()
