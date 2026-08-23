"""推理引擎基类：定义统一接口与分块调度，具体模型族实现 _infer。"""
from __future__ import annotations

import numpy as np

from ..pipeline.tile import merge_tile, split_tiles


class BaseEngine:
    """帧变换器接口。

    process(frame_rgb) 输入 uint8 HWC RGB，返回 uint8 HWC RGB（边长乘以 self.scale）。
    batch > 1 时管线会攒帧调 process_batch，引擎应覆写以获得真正的批量推理收益。
    """

    scale: int = 2
    tile: int = 0          # 0 = 不分块；>0 = 边长超过该值时按 tile 分块
    tile_overlap: int = 16
    batch: int = 1         # 每次 session.run 喂入的帧数（引擎能力内自动收敛）

    def load(self) -> None:
        raise NotImplementedError

    def process(self, frame: np.ndarray) -> np.ndarray:
        h, w = frame.shape[:2]
        if self.tile and (h > self.tile or w > self.tile):
            out = np.empty((h * self.scale, w * self.scale, 3), dtype=np.uint8)
            for span in split_tiles(h, w, self.tile, self.tile_overlap):
                y0, y1, x0, x1 = span
                up = self._infer(frame[y0:y1, x0:x1])
                merge_tile(out, up, span, (h, w), self.tile_overlap, self.scale)
            return out
        return self._infer(frame)

    def process_batch(self, frames: np.ndarray) -> np.ndarray:
        """输入 [N,H,W,3] uint8，输出 [N,H*s,W*s,3] uint8。默认逐帧回退。"""
        return np.stack([self.process(f) for f in frames])

    def _infer(self, frame: np.ndarray) -> np.ndarray:
        raise NotImplementedError
