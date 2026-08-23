"""从官方 RealESRGAN_x4plus.pth 导出动态尺寸 ONNX（GitHub Release 模型资产）。

用法（需 .venv-cuda 的 PyTorch）:
    ../.venv-cuda/Scripts/python.exe scripts/export_onnx_x4plus.py [pth路径] [onnx输出]

背景：HF 社区导出版固定 64x64 输入（x4plus 慢的根源，BENCH M0）；本导出为全动态
尺寸，ImageNet 归一化烘焙进图（模型 IO = 0-1 RGB，与其它模型一致），fp32 与
torch 引擎输出 PSNR 65.3dB，DML fp16 下 240x320→x4 66ms/帧（固定版约 1800ms）。
opset 17，导出器用 legacy 路径（dynamo=True 不带权重）。
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import torch

from sv.engines.torch_engine import _MEAN, _STD, _rrdbnet


class Normalized(torch.nn.Module):
    """输入 0-1 RGB -> ImageNet 归一化 -> RRDBNet -> 反归一化 -> 0-1 RGB。"""

    def __init__(self, m):
        super().__init__()
        self.m = m
        self.register_buffer("mean", torch.from_numpy(_MEAN).view(1, 3, 1, 1))
        self.register_buffer("std", torch.from_numpy(_STD).view(1, 3, 1, 1))

    def forward(self, x):
        y = self.m((x - self.mean) / self.std)
        return y * self.std + self.mean


def main() -> None:
    pth = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("RealESRGAN_x4plus.pth")
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("RealESRGAN_x4plus_dyn.onnx")

    net = _rrdbnet().eval()
    sd = torch.load(str(pth), map_location="cpu", weights_only=True)
    net.load_state_dict(sd.get("params_ema", sd), strict=True)

    torch.onnx.export(
        Normalized(net).eval(), torch.randn(1, 3, 64, 64), str(out),
        input_names=["input"], output_names=["output"],
        dynamic_axes={"input": {0: "batch", 2: "height", 3: "width"},
                      "output": {0: "batch", 2: "height4", 3: "width4"}},
        opset_version=17, do_constant_folding=True, dynamo=False,
    )
    print(f"{out}: {out.stat().st_size} bytes")
    print(f"sha256: {hashlib.sha256(out.read_bytes()).hexdigest()}")


if __name__ == "__main__":
    main()
