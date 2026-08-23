"""ONNX 超分引擎（Real-ESRGAN / animevideov3 等模型族）。

输入约定由 manifest 的 io 字段描述：
  color: "bgr" | "rgb"（模型训练时的通道序）
  range: "0-255"（float32，不归一化——Real-ESRGAN 系约定）
pad: 输入边长需对齐的最小倍数（pixelshuffle 需要，通常 = scale）
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from .base import BaseEngine

# 按 preference 排序，取运行时可用的第一个组合（onnxruntime-directml 提供 DML+CPU）
_PROVIDER_ORDER = [
    "CUDAExecutionProvider",
    "DmlExecutionProvider",
    "CPUExecutionProvider",
]


class OnnxSrEngine(BaseEngine):
    def __init__(
        self,
        model_path: str | Path,
        scale: int,
        io: dict | None = None,
        device: str = "auto",
        tile: int = 0,
        tile_overlap: int = 16,
    ):
        self.model_path = Path(model_path)
        self.scale = scale
        io = io or {}
        self.color = io.get("color", "bgr")
        self.value_range = io.get("range", "0-255")
        self.pad = int(io.get("pad", scale))
        self.device = device
        self.tile = tile
        self.tile_overlap = tile_overlap
        self.session = None
        self.provider_used: list[str] = []
        self.fixed_hw: tuple[int, int] | None = None
        self._in_name = None
        self._out_names = None

    def load(self) -> None:
        import onnxruntime as ort

        from .nvidia_dlls import register_nvidia_dlls

        register_nvidia_dlls()  # CUDA 版 ORT 的 DLL 在 pip 包内，需先挂载
        available = set(ort.get_available_providers())
        if self.device == "cpu":
            chosen = ["CPUExecutionProvider"]
        else:
            chosen = [p for p in _PROVIDER_ORDER if p in available] or ["CPUExecutionProvider"]
        self.session = ort.InferenceSession(
            str(self.model_path), providers=chosen
        )
        self.provider_used = self.session.get_providers()
        inp = self.session.get_inputs()[0]
        self._in_name = inp.name
        self._out_names = [o.name for o in self.session.get_outputs()]
        self._in_dtype = inp.type  # 'tensor(float)' 等
        # 固定输入尺寸的导出版本（如 [1,3,64,64]）：推理前补边、推理后裁剪
        shape = inp.shape
        if len(shape) == 4 and isinstance(shape[2], int) and isinstance(shape[3], int):
            self.fixed_hw = (shape[2], shape[3])
        else:
            self.fixed_hw = None

    def _infer(self, frame: np.ndarray) -> np.ndarray:
        h, w = frame.shape[:2]
        if self.fixed_hw is not None:
            fh, fw = self.fixed_hw
            if h > fh or w > fw:
                raise ValueError(
                    f"输入 {w}x{h} 超过固定尺寸模型上限 {fw}x{fh}，应启用更小的 tile"
                )
            ph, pw = fh - h, fw - w
        else:
            ph = (self.pad - h % self.pad) % self.pad
            pw = (self.pad - w % self.pad) % self.pad
        x = frame
        if ph or pw:
            x = np.pad(x, ((0, ph), (0, pw), (0, 0)), mode="edge")
        if self.color == "bgr":
            x = x[..., ::-1]
        x = np.ascontiguousarray(x.transpose(2, 0, 1)[None].astype(np.float32))
        if self.value_range == "0-1":
            x = x / 255.0

        y = self.session.run(self._out_names, {self._in_name: x})[0]
        y = np.squeeze(y, axis=0).transpose(1, 2, 0)  # CHW -> HWC
        if self.value_range == "0-1":
            y = y * 255.0
        y = np.clip(y, 0, 255).astype(np.uint8)
        if self.color == "bgr":
            y = y[..., ::-1]
        if ph or pw:
            y = y[: h * self.scale, : w * self.scale]
        return np.ascontiguousarray(y)
