"""大图分块推理：重叠切分 + 无缝拼合。

拼合策略：每个 tile 只贴"核心区"——非画面边缘的一侧裁掉一半重叠量，
相邻 tile 的核心区恰好无缝衔接，对确定性变换（如最近邻上采样）严格无损。
"""
from __future__ import annotations

import numpy as np


def split_tiles(h: int, w: int, tile: int, overlap: int = 16) -> list[tuple[int, int, int, int]]:
    """返回覆盖 (h, w) 的 tile 列表，元素为 (y0, y1, x0, x1)，相邻 tile 重叠 overlap 像素。"""
    if overlap >= tile:
        raise ValueError(f"overlap({overlap}) 必须小于 tile({tile})")
    return [
        (y0, y1, x0, x1)
        for y0, y1 in _axis_spans(h, tile, overlap)
        for x0, x1 in _axis_spans(w, tile, overlap)
    ]


def _axis_spans(n: int, tile: int, overlap: int) -> list[tuple[int, int]]:
    if n <= tile:
        return [(0, n)]
    step = tile - overlap
    starts = list(range(0, n - tile + 1, step))
    last = n - tile
    if starts[-1] != last:
        starts.append(last)
    starts = sorted(set(starts))
    return [(s, s + tile) for s in starts]


def merge_tile(
    canvas: np.ndarray,
    tile_img: np.ndarray,
    span: tuple[int, int, int, int],
    frame_hw: tuple[int, int],
    overlap: int,
    scale: int,
) -> None:
    """把 tile_img（已上采样 scale 倍）贴到 canvas 的核心区。"""
    y0, y1, x0, x1 = span
    h, w = frame_hw
    top = overlap // 2 if y0 > 0 else 0
    bottom = overlap - overlap // 2 if y1 < h else 0
    left = overlap // 2 if x0 > 0 else 0
    right = overlap - overlap // 2 if x1 < w else 0
    th, tw = y1 - y0, x1 - x0
    src = tile_img[
        top * scale : (th - bottom) * scale,
        left * scale : (tw - right) * scale,
    ]
    canvas[
        (y0 + top) * scale : (y1 - bottom) * scale,
        (x0 + left) * scale : (x1 - right) * scale,
    ] = src
