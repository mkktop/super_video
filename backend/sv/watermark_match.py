"""Conservative, multiscale template matching for margin watermarks.

Uses the existing NumPy/Pillow dependencies. NCC is computed by FFT plus summed
area tables, with bounded-resolution search and full-resolution refinement.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageOps


class MatchSkipped(ValueError):
    """Expected uncertainty: leave the original alone and explain the skip."""


@dataclass
class WatermarkTemplate:
    pixels: Image.Image
    source_size: tuple[int, int]
    background: int = 255


@dataclass
class Match:
    box: tuple[int, int, int, int]
    score: float
    scale: float


def gray(im: Image.Image) -> Image.Image:
    # Composite transparent source pixels on white, matching the displayed page.
    if im.mode in {"RGBA", "LA"}:
        rgba = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, "white")
        bg.alpha_composite(rgba)
        return bg.convert("L")
    return im.convert("L")


def make_template(im: Image.Image, box: tuple[int, int, int, int], *, allow_dark: bool = False,
                  trim: bool = True) -> WatermarkTemplate:
    crop = gray(im).crop(box)
    a = np.asarray(crop)
    if a.size > 250000 or min(crop.size) < 5:
        raise ValueError("样本区域过大或过小，请只框选水印及少量白边")
    edge = np.concatenate((a[0], a[-1], a[:, 0], a[:, -1]))
    background = 0 if allow_dark and float((edge < 25).mean()) > .90 else 255
    if background == 0:
        crop = ImageOps.invert(crop)
        a = np.asarray(crop)
    ink = a < 245
    if np.count_nonzero(ink) < 20 or float(a.std()) < 2:
        raise ValueError("样本中没有足够的水印特征，请重新框选")
    if (background == 255 and float((a < 110).mean()) > .005) or float((a > 248).mean()) < .20:
        raise ValueError("样本可能包含漫画内容；请从纯色页边框选水印及少量空白")
    if not trim:
        return WatermarkTemplate(crop, im.size, background)
    ys, xs = np.where(ink)
    # Include JPEG fringe and white context, without retaining a large blank box.
    left, top = max(0, int(xs.min()) - 4), max(0, int(ys.min()) - 4)
    right, bottom = min(crop.width, int(xs.max()) + 5), min(crop.height, int(ys.max()) + 5)
    return WatermarkTemplate(crop.crop((left, top, right, bottom)), im.size, background)


def _sums(a: np.ndarray, h: int, w: int) -> np.ndarray:
    integral = np.pad(a.astype(np.float64), ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    return integral[h:, w:] - integral[:-h, w:] - integral[h:, :-w] + integral[:-h, :-w]


def ncc(image: np.ndarray, template: np.ndarray) -> np.ndarray:
    """Valid normalized cross correlation; constant windows have score -1."""
    image = image.astype(np.float64)
    template = template.astype(np.float64)
    h, w = template.shape
    ih, iw = image.shape
    if h > ih or w > iw:
        return np.empty((0, 0))
    centered = template - template.mean()
    energy = float(np.square(centered).sum())
    shape = (1 << (ih + h - 2).bit_length(), 1 << (iw + w - 2).bit_length())
    convolution = np.fft.irfft2(
        np.fft.rfft2(image, s=shape) * np.fft.rfft2(centered[::-1, ::-1], s=shape), s=shape,
    )[h - 1:ih, w - 1:iw]
    variance = np.maximum(0, _sums(image * image, h, w) - _sums(image, h, w) ** 2 / (h * w))
    denominator = np.sqrt(variance * energy)
    scores = np.full(variance.shape, -1., dtype=np.float64)
    np.divide(convolution, denominator, out=scores, where=denominator > 1e-6)
    return np.clip(scores, -1, 1)


def _overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    area = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    return area / max(1, min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1])))


def locate(im: Image.Image, template: WatermarkTemplate, threshold: float = .88,
           *, allow_dark: bool = False, allow_artwork: bool = False) -> Match:
    """Match normalized light/dark logos; artwork repair must be explicitly selected."""
    page = gray(im)
    candidates = []
    reasons = []
    polarities = [template.background]
    if allow_dark:
        polarities = [255, 0]
    for background in polarities:
        normalized = ImageOps.invert(page) if background == 0 else page
        try:
            candidates.append(_locate(normalized, template, threshold, allow_artwork=allow_artwork))
        except MatchSkipped as error:
            reasons.append(str(error))
    if not candidates:
        raise MatchSkipped(reasons[0])
    candidates.sort(key=lambda item: item.score, reverse=True)
    if len(candidates) > 1 and _overlap(candidates[0].box, candidates[1].box) < .35 and candidates[1].score >= candidates[0].score - .04:
        raise MatchSkipped("存在多个相近匹配，无法确定水印位置，请人工检查")
    return candidates[0]


def _locate(im: Image.Image, template: WatermarkTemplate, threshold: float,
            *, allow_artwork: bool = False) -> Match:
    page = gray(im)
    w, h = im.size
    ox, oy = int(w * .55), int(h * .70)
    roi = page.crop((ox, oy, w, h))
    reduction = min(1., 440 / max(roi.size))
    roi = roi.resize((max(1, round(roi.width * reduction)), max(1, round(roi.height * reduction))), Image.Resampling.LANCZOS)
    image = np.asarray(roi)
    tw, th = template.pixels.size
    ratios = [w / template.source_size[0], h / template.source_size[1]]
    scales = {1., *ratios, *np.geomspace(.5, 2, 25)}
    for ratio in ratios:
        scales.update(ratio * np.geomspace(.8, 1.25, 13))
    candidates: list[Match] = []
    sizes = set()
    for scale in sorted(scales):
        size = (round(tw * scale * reduction), round(th * scale * reduction))
        if size in sizes or min(size) < 7 or size[0] > roi.width or size[1] > roi.height:
            continue
        sizes.add(size)
        patch = np.asarray(template.pixels.resize(size, Image.Resampling.LANCZOS))
        scores = ncc(image, patch)
        for _ in range(2):
            index = np.unravel_index(np.argmax(scores), scores.shape)
            y, x = map(int, index)
            score = float(scores[y, x])
            if score < max(.60, threshold - .18):
                break
            bx, by = ox + round(x / reduction), oy + round(y / reduction)
            candidates.append(Match((bx, by, bx + round(tw * scale), by + round(th * scale)), score, float(scale)))
            # Suppress the same logo shifted by a few pixels, but keep distinct logos.
            scores[max(0, y - size[1] // 2):y + size[1] // 2 + 1,
                   max(0, x - size[0] // 2):x + size[0] // 2 + 1] = -1
    if not candidates:
        raise MatchSkipped("右下角未找到相似水印，保留原图")
    candidates.sort(key=lambda item: item.score, reverse=True)
    best = candidates[0]
    for other in candidates[1:]:
        if _overlap(best.box, other.box) < .35 and other.score >= best.score - .04:
            raise MatchSkipped("存在多个相近匹配，无法确定水印位置，请人工检查")
    # Refine the approximate location/scale at source resolution before erasing.
    pad = max(6, round(4 / reduction))
    x, y, right, bottom = best.box
    bounds = (max(0, x - pad), max(0, y - pad), min(w, right + pad), min(h, bottom + pad))
    local = np.asarray(page.crop(bounds))
    refined: list[Match] = []
    for scale in sorted({best.scale, *(best.scale * np.array([.97, .985, 1.015, 1.03]))}):
        size = (max(1, round(tw * scale)), max(1, round(th * scale)))
        patch = np.asarray(template.pixels.resize(size, Image.Resampling.LANCZOS))
        scores = ncc(local, patch)
        if not scores.size:
            continue
        iy, ix = map(int, np.unravel_index(np.argmax(scores), scores.shape))
        bx, by = bounds[0] + ix, bounds[1] + iy
        refined.append(Match((bx, by, bx + size[0], by + size[1]), float(scores[iy, ix]), float(scale)))
    if not refined:
        raise MatchSkipped("水印尺寸超出搜索范围，请人工检查")
    best = max(refined, key=lambda item: item.score)
    if best.score < threshold:
        raise MatchSkipped(f"匹配度不足（{best.score:.1%}，要求 {threshold:.0%}），请人工检查")
    if allow_artwork:
        return best
    crop = np.asarray(page.crop(best.box))
    expected = np.asarray(template.pixels.resize((crop.shape[1], crop.shape[0]), Image.Resampling.LANCZOS))
    white = expected > 250
    if not np.any(white) or float((crop[white] < 230).mean()) > .025:
        raise MatchSkipped("水印区域可能混有漫画线稿或文字，已跳过")
    # Dark context crossing the edge is a strong sign of touching artwork.
    bx, by, br, bb = best.box
    ring_box = (max(0, bx - 3), max(0, by - 3), min(w, br + 3), min(h, bb + 3))
    ring = np.asarray(page.crop(ring_box))
    context = np.ones(ring.shape, dtype=bool)
    context[by - ring_box[1]:bb - ring_box[1], bx - ring_box[0]:br - ring_box[0]] = False
    if np.any(context) and float((ring[context] < 160).mean()) > .025:
        raise MatchSkipped("水印靠近漫画内容，填白可能影响线条，已跳过")
    return best
