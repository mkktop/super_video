"""PyTorch 超分引擎（Real-ESRGAN RRDBNet 系）。

自带最小 RRDBNet 结构定义（无 BasicSR 依赖），加载官方 release 的 pth 权重
（state dict 键带 params_ema. 前缀，自动剥离）。CUDA 优先（torch cu128），
无 CUDA 时 CPU 兜底（慢）。x4plus 约定：RGB + 0-1 + ImageNet 均值方差归一化。

扩散模型（FlashVSR/SeedVR2）经调研在 Windows 不可行（无 ONNX、6.4~12.6GiB
权重、依赖 Linux-only apex / CUDA 源码编译组件），本引擎承接 M3 的 torch
基础设施；扩散模型本体留待其官方支持 Windows 后接入。见 BENCH.md。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

# ImageNet 归一化（Real-ESRGAN 官方 pre_process 约定）
_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


def _conv(ni: int, no: int, k: int = 3, s: int = 1, p: int | None = None):
    import torch.nn as nn

    return nn.Conv2d(ni, no, k, s, padding=(k - 1) // 2 if p is None else p)


def _rrdbnet(num_in_ch=3, num_out_ch=3, scale=4, num_feat=64, num_block=23,
             num_grow_ch=32):
    """最小 RRDBNet（对齐 Real-ESRGAN 官方 arch，无 basicsr）。"""
    import torch
    import torch.nn as nn
    import torch.nn.functional as F

    class ResidualDenseBlock(nn.Module):
        def __init__(self, num_feat, num_grow_ch):
            super().__init__()
            self.conv1 = _conv(num_feat, num_grow_ch)
            self.conv2 = _conv(num_feat + num_grow_ch, num_grow_ch)
            self.conv3 = _conv(num_feat + 2 * num_grow_ch, num_grow_ch)
            self.conv4 = _conv(num_feat + 3 * num_grow_ch, num_grow_ch)
            self.conv5 = _conv(num_feat + 4 * num_grow_ch, num_feat)
            self.lrelu = nn.LeakyReLU(0.2, inplace=True)

        def forward(self, x):
            x1 = self.lrelu(self.conv1(x))
            x2 = self.lrelu(self.conv2(torch.cat((x, x1), 1)))
            x3 = self.lrelu(self.conv3(torch.cat((x, x1, x2), 1)))
            x4 = self.lrelu(self.conv4(torch.cat((x, x1, x2, x3), 1)))
            x5 = self.conv5(torch.cat((x, x1, x2, x3, x4), 1))
            return x5 * 0.2 + x

    class RRDB(nn.Module):
        def __init__(self, num_feat, num_grow_ch):
            super().__init__()
            self.rdb1 = ResidualDenseBlock(num_feat, num_grow_ch)
            self.rdb2 = ResidualDenseBlock(num_feat, num_grow_ch)
            self.rdb3 = ResidualDenseBlock(num_feat, num_grow_ch)

        def forward(self, x):
            out = self.rdb1(x)
            out = self.rdb2(out)
            out = self.rdb3(out)
            return out * 0.2 + x

    class RRDBNet(nn.Module):
        def __init__(self):
            super().__init__()
            self.conv_first = _conv(num_in_ch, num_feat, 3, 1, 1)
            self.body = nn.Sequential(*[RRDB(num_feat, num_grow_ch)
                                        for _ in range(num_block)])
            self.conv_body = _conv(num_feat, num_feat, 3, 1, 1)
            self.conv_up1 = _conv(num_feat, num_feat, 3, 1, 1)
            self.conv_up2 = _conv(num_feat, num_feat, 3, 1, 1)
            self.conv_hr = _conv(num_feat, num_feat, 3, 1, 1)
            self.conv_last = _conv(num_feat, num_out_ch, 3, 1, 1)
            self.lrelu = nn.LeakyReLU(0.2, inplace=True)

        def forward(self, x):
            fea = self.conv_first(x)
            body_fea = self.conv_body(self.body(fea))
            fea = fea + body_fea
            fea = self.lrelu(self.conv_up1(F.interpolate(fea, scale_factor=2, mode="nearest")))
            fea = self.lrelu(self.conv_up2(F.interpolate(fea, scale_factor=2, mode="nearest")))
            out = self.conv_last(self.lrelu(self.conv_hr(fea)))
            return out

    return RRDBNet()


class TorchSrEngine:
    """process(frame BGR u8) -> BGR u8；CUDA autocast fp16（约半显存/双倍带宽）。"""

    def __init__(self, model_path: str | Path, scale: int, io: dict | None = None,
                 tile: int = 512, batch: int = 1):
        self.model_path = Path(model_path)
        self.scale = scale
        io = io or {}
        self.arch = io.get("arch", "rrdbnet")
        self.tile = int(tile)
        self.batch = max(1, batch)
        self.model = None
        self.device = "cpu"
        self.provider_used: list[str] = []

    def load(self) -> None:
        import torch

        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        if self.arch != "rrdbnet":
            raise ValueError(f"未知 torch 架构 {self.arch}")
        net = _rrdbnet()
        sd = torch.load(str(self.model_path), map_location="cpu", weights_only=True)
        sd = sd.get("params_ema", sd)
        net.load_state_dict(sd, strict=True)
        self.model = net.to(self.device).eval()
        self.provider_used = [f"torch-cuda:{torch.cuda.get_device_name(0)}"
                              if self.device == "cuda" else "torch-cpu"]

    @property
    def supports_batch(self) -> bool:
        return False  # 逐帧 tile 推理，批处理无益

    def process(self, frame: np.ndarray) -> np.ndarray:
        import torch

        h, w = frame.shape[:2]
        # BGR u8 -> RGB 0-1 ImageNet 归一化
        rgb = frame[..., ::-1].astype(np.float32) / 255.0
        x = ((rgb - _MEAN) / _STD).transpose(2, 0, 1)[None]

        import torch

        xt = torch.from_numpy(x).to(self.device)
        if self.device == "cuda":
            with torch.no_grad(), torch.autocast("cuda", dtype=torch.float16):
                yt = self._tile_run(xt, self.tile or max(h, w))
        else:
            with torch.no_grad():
                yt = self._tile_run(xt, self.tile or max(h, w))
        y = yt.float().cpu().numpy()[0].transpose(1, 2, 0)
        y = (y * _STD + _MEAN) * 255.0
        return np.clip(y, 0, 255).round().astype(np.uint8)[..., ::-1]

    def _tile_run(self, xt, tile: int):
        _, _, H, W = xt.shape
        s = self.scale
        if H <= tile and W <= tile:
            return self.model(xt)
        import torch

        out = torch.zeros(1, 3, H * s, W * s, device=xt.device)
        overlap = 16
        for y0 in range(0, H, tile - overlap):
            for x0 in range(0, W, tile - overlap):
                y1, x1 = min(y0 + tile, H), min(x0 + tile, W)
                y0c, x0c = max(0, y1 - tile), max(0, x1 - tile)
                part = self.model(xt[:, :, y0c:y1, x0c:x1])
                ph = (y1 - y0c) * s
                pw = (x1 - x0c) * s
                out[:, :, y0c * s:y0c * s + ph, x0c * s:x0c * s + pw] = part.float()
        return out

    def process_batch(self, frames: np.ndarray) -> np.ndarray:
        return np.stack([self.process(f) for f in frames])
