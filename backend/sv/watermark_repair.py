"""Local watermark masks, solid-margin fill and CPU inpainting.

Only masked pixels are pasted back; PNG output preserves every other pixel.
"""
from __future__ import annotations

from typing import Literal
import numpy as np
from PIL import Image

from .watermark_match import MatchSkipped, WatermarkTemplate, gray

Removal = Literal["white", "auto", "repair"]


def erase(im: Image.Image, box: tuple[int, int, int, int], removal: Removal,
          template: WatermarkTemplate | None = None) -> str:
    if removal == "white":
        fill = {"RGB": (255, 255, 255), "RGBA": (255, 255, 255, 255),
                "L": 255, "LA": (255, 255)}[im.mode]
        im.paste(fill, box)
        return "white"
    x, y, right, bottom = box
    cut = im.crop(box)
    a = np.asarray(gray(cut))
    if template is not None:
        # The template is normalized to a white background, even for white-on-black logos.
        import cv2
        sample = np.asarray(template.pixels.resize(cut.size, Image.Resampling.LANCZOS))
        mask = np.uint8(sample < 250) * 255
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(mask, contours, -1, 255, -1)
        mask = cv2.dilate(mask, np.ones((5, 5), np.uint8))
        context = a[mask == 0]
    else:
        mask = np.full(a.shape, 255, np.uint8)
        # A fixed box has no glyph sample. Require both the box and its ring to
        # resemble a blank margin before deciding to fill rather than repair.
        context = a.ravel()
    ring_box = (max(0, x - 4), max(0, y - 4), min(im.width, right + 4), min(im.height, bottom + 4))
    ring = np.asarray(gray(im.crop(ring_box)))
    outside = np.ones(ring.shape, bool)
    outside[y-ring_box[1]:bottom-ring_box[1], x-ring_box[0]:right-ring_box[0]] = False
    ring_pixels = ring[outside]
    white = context.size > 0 and float((context > 230).mean()) >= .975
    black = context.size > 0 and float((context < 25).mean()) >= .975
    if template is None:
        white = float((a < 110).mean()) <= .005 and float((a > 248).mean()) >= .20
        # White glyphs on black can be fully opaque. Their brightness alone
        # must not disqualify an otherwise-black manually selected margin.
        black = float((a < 25).mean()) >= .65
    if ring_pixels.size:
        white = white and float((ring_pixels > 230).mean()) >= .975
        black = black and float((ring_pixels < 25).mean()) >= .975
    # Without any surrounding context, fixed automatic fill is too uncertain.
    elif template is None:
        white = black = False
    if white or black:
        background = 255 if white else 0
        value = {"RGB": (background,)*3, "RGBA": (background,)*3+(255,),
                 "L": background, "LA": (background,255)}[im.mode]
        im.paste(value, box)
        return "white" if white else "black"
    if removal == "auto":
        raise MatchSkipped("区域包含画面或背景不均匀，已跳过；可选择局部修补并检查预览")
    import cv2
    # Include real context on all sides, even if the logo is flush with the edge.
    pad = 12
    bounds = (max(0,x-pad), max(0,y-pad), min(im.width,right+pad), min(im.height,bottom+pad))
    context_im = im.crop(bounds)
    source = np.array(context_im)
    local_mask = np.zeros((context_im.height, context_im.width), np.uint8)
    local_mask[y-bounds[1]:bottom-bounds[1], x-bounds[0]:right-bounds[0]] = mask
    if not np.any(local_mask == 0):
        raise MatchSkipped("修补区域没有可参考的周围像素，请缩小框选范围")
    if im.mode in {"L", "LA"}:
        color = source if im.mode == "L" else source[:, :, 0]
    else:
        color = source[:, :, :3].copy()
    result = cv2.inpaint(color, local_mask, 3, cv2.INPAINT_TELEA)
    # Preserve alpha and exactly retain all unmasked pixels.
    if im.mode == "L":
        source[local_mask > 0] = result[local_mask > 0]
    elif im.mode == "LA":
        source[:, :, 0][local_mask > 0] = result[local_mask > 0]
    else:
        source[:, :, :3][local_mask > 0] = result[local_mask > 0]
    im.paste(Image.fromarray(source), bounds)
    return "inpaint"
