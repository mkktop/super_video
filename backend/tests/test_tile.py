"""tile 切分/拼合：确定性上采样下必须严格无损。"""
import numpy as np
import pytest

from sv.pipeline.tile import merge_tile, split_tiles


def upsample_nearest(img: np.ndarray, scale: int) -> np.ndarray:
    return np.repeat(np.repeat(img, scale, axis=0), scale, axis=1)


@pytest.mark.parametrize("h,w", [(377, 509), (128, 128), (300, 200), (65, 67)])
@pytest.mark.parametrize("scale", [2, 4])
def test_tile_roundtrip_lossless(h, w, scale):
    rng = np.random.default_rng(42)
    frame = rng.integers(0, 256, (h, w, 3), dtype=np.uint8)
    tile, overlap = 128, 16

    direct = upsample_nearest(frame, scale)
    out = np.empty_like(direct)
    for span in split_tiles(h, w, tile, overlap):
        y0, y1, x0, x1 = span
        assert y1 - y0 <= tile and x1 - x0 <= tile
        up = upsample_nearest(frame[y0:y1, x0:x1], scale)
        merge_tile(out, up, span, (h, w), overlap, scale)

    assert np.array_equal(out, direct), "tile 拼合必须与整帧结果逐像素一致"


def test_split_tiles_full_coverage():
    for h, w in [(100, 100), (500, 333), (64, 64)]:
        tiles = split_tiles(h, w, 64, 8)
        assert tiles[0] == (0, min(h, 64), 0, min(w, 64))
        # 每个像素至少被一个 tile 覆盖
        cover_h = np.zeros(h, dtype=bool)
        cover_w = np.zeros(w, dtype=bool)
        for y0, y1, x0, x1 in tiles:
            cover_h[y0:y1] = True
            cover_w[x0:x1] = True
        assert cover_h.all() and cover_w.all()
