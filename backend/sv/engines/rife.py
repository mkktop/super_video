"""RIFE 光流补帧（vs-mlrt implementation=2 导出版）。

ONNX 输入 [N,7,H,W] float32 0-1 RGB：I0(3ch) + I1(3ch) + timestep(1ch)，
输出中间帧 [N,3,H,W]。2x 补帧固定 t=0.5。
校准记录（2026-08-23）：t=0 重构 I0 PSNR 45.7dB，t=1 重构 I1 38.9dB。

外部接口用 RGB uint8（与管线一致：解码 rgb24、超分引擎输入输出均 RGB）。
历史版本这里按"BGR 外部约定"做了两次通道翻转，等于把 BGR 喂进 RGB 权重，
所有插帧红蓝错配——2026-08-26 审查修复，勿回退。

IFNet 内部 5 次下采样：非 32 对齐的输入做 edge 填充再裁回，
避免任意超分输出尺寸（如 4x 后 %32≠0 的宽高）在 session.run 才崩。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np


class Rife2x:
    scale = 1  # 补帧不改分辨率
    _ALIGN = 32

    def __init__(self, model_path: str | Path):
        self.model_path = Path(model_path)
        self.session = None
        self.provider_used: list[str] = []
        self._input_name = "input"

    def load(self) -> None:
        import onnxruntime as ort

        from .nvidia_dlls import register_nvidia_dlls

        register_nvidia_dlls()
        available = set(ort.get_available_providers())
        chosen = [p for p in ("CUDAExecutionProvider", "DmlExecutionProvider",
                              "CPUExecutionProvider") if p in available]
        self.session = ort.InferenceSession(str(self.model_path), providers=chosen)
        self.provider_used = self.session.get_providers()
        names = [i.name for i in self.session.get_inputs()]
        self._input_name = "input" if "input" in names else names[0]

    def _run(self, i0: np.ndarray, i1: np.ndarray, t: float) -> np.ndarray:
        """i0/i1: [N,3,H,W] RGB 0-1。返回同布局中间帧（尺寸对齐后裁回）。"""
        n, _, h, w = i0.shape
        tt = np.full((n, 1, h, w), t, dtype=np.float32)
        x = np.concatenate([i0, i1, tt], axis=1)
        ph, pw = (-h) % self._ALIGN, (-w) % self._ALIGN
        if ph or pw:
            x = np.pad(x, ((0, 0), (0, 0), (0, ph), (0, pw)), mode="edge")
        y = self.session.run(None, {self._input_name: x})[0]
        return y[:, :, :h, :w]

    def interpolate(self, frame0: np.ndarray, frame1: np.ndarray) -> np.ndarray:
        """两帧 RGB uint8 -> 中间帧 RGB uint8。"""
        def to_chw(f: np.ndarray) -> np.ndarray:
            return (np.ascontiguousarray(f).transpose(2, 0, 1)[None] / 255.0).astype(np.float32)

        mid = self._run(to_chw(frame0), to_chw(frame1), 0.5)
        return (np.clip(mid[0].transpose(1, 2, 0), 0, 1) * 255).round().astype(np.uint8).copy()
