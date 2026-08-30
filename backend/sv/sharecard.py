"""对比分享卡片：任务产物 vs 源 → 可晒图的长图 / 滑块动图 GIF。

给"晒效果"场景用：长图 = 上下叠放处理前/后 + 标签与信息条（无损 PNG）；
动图 = 分割线来回扫的合成帧 GIF（256 色调色板是 GIF 格式限制，宽度收到
960 控体积）。抽帧用 ffmpeg 按时间点取（输出帧率翻倍的任务时间轴不变）。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .paths import ffmpeg_bin
from .utils.process import WINDOWS_CREATE_FLAGS

# 中文字体：Windows 自带微软雅黑（安装版运行环境必有；dev 的 Linux CI 无中文字体
# 时退回 PIL 位图字体，标签降级为可读的小字，不阻塞出图）
_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
]

CARD_W = 1280   # 长图宽
GIF_W = 960     # 动图宽（GIF 体积敏感收窄）
GIF_FRAMES = 34  # 一个来回；duration 60ms ≈ 2s/来回
BAR_H = 64      # 信息条高


def extract_frame(path: Path, t_s: float, target_w: int = CARD_W) -> Image.Image:
    """按时间点抽取一帧并缩到 target_w 宽（高度自适应、偶数对齐）。"""
    cmd = [
        ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin",
        "-ss", f"{max(0.0, t_s):.3f}", "-i", str(path),
        "-map", "0:v:0", "-an", "-sn", "-dn",
        "-frames:v", "1",
        "-vf", f"scale={target_w}:-2:flags=lanczos",
        "-f", "image2", "-c:v", "png", "-",
    ]
    r = subprocess.run(cmd, capture_output=True, timeout=60,
                       creationflags=WINDOWS_CREATE_FLAGS)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError(f"抽帧失败: {r.stderr.decode('utf-8', 'replace')[-200:]}")
    import io

    img = Image.open(io.BytesIO(r.stdout)).convert("RGB")
    return img


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for p in _FONT_CANDIDATES:
        try:
            if Path(p).exists():
                return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _scale_pair(src: Image.Image, out: Image.Image, target_w: int) -> tuple[Image.Image, Image.Image]:
    """源/输出统一到同宽（源与输出同宽高比；不同也不强行拉伸，各按宽缩放）。"""
    def fit(im: Image.Image) -> Image.Image:
        h = round(im.height * target_w / im.width)
        return im.resize((target_w, max(2, h - h % 2)), Image.LANCZOS)

    return fit(src), fit(out)


def make_long_image(src_path: Path, out_path: Path, t_src: float, t_out: float,
                    meta: dict, dest: Path) -> Path:
    """上下叠放长图：信息条 + 处理前 + 处理后 + 落款条（PNG 无损）。"""
    src = extract_frame(src_path, t_src, CARD_W)
    out = extract_frame(out_path, t_out, CARD_W)
    src, out = _scale_pair(src, out, CARD_W)
    f_title, f_small = _font(30), _font(22)
    gap = 10
    head_h = BAR_H
    label_h = 40
    foot_h = BAR_H
    total_h = head_h + label_h + src.height + gap + label_h + out.height + foot_h
    card = Image.new("RGB", (CARD_W, total_h), (13, 14, 16))
    d = ImageDraw.Draw(card)

    title = f"super_video · {meta.get('model', '')} · {meta.get('scale', '')}"
    d.text((20, 16), title, font=f_title, fill=(232, 234, 237))

    y = head_h
    d.text((20, y + 8), "处理前", font=f_small, fill=(154, 160, 166))
    y += label_h
    card.paste(src, (0, y))
    y += src.height + gap
    d.text((20, y + 8), "处理后（AI 超分）", font=f_small, fill=(79, 140, 255))
    y += label_h
    card.paste(out, (0, y))
    y += out.height

    foot = f"{meta.get('name', '')}  ·  {meta.get('date', '')}"
    d.text((20, y + 16), foot, font=f_small, fill=(120, 126, 132))
    # 分隔线把信息条/落款与画面隔开
    d.line([(0, head_h), (CARD_W, head_h)], fill=(42, 45, 49), width=2)
    d.line([(0, y), (CARD_W, y)], fill=(42, 45, 49), width=2)

    dest.parent.mkdir(parents=True, exist_ok=True)
    card.save(dest, "PNG")
    return dest


def make_slider_gif(src_path: Path, out_path: Path, t_src: float, t_out: float,
                    meta: dict, dest: Path) -> Path:
    """分割线来回扫的动图：分割线左=处理前 右=处理后，标签与信息条逐帧叠加。"""
    src = extract_frame(src_path, t_src, GIF_W)
    out = extract_frame(out_path, t_out, GIF_W)
    src, out = _scale_pair(src, out, GIF_W)
    w, h = src.size
    head_h, foot_h = 52, 44
    total_h = head_h + h + foot_h
    f_title, f_small = _font(24), _font(20)

    # 来回轨迹：0.08→0.92 线性去回（平滑往返，无停顿跳变）
    xs = [0.08 + 0.84 * i / (GIF_FRAMES / 2) for i in range(int(GIF_FRAMES / 2) + 1)]
    sweep = xs + xs[-2:0:-1]

    frames: list[Image.Image] = []
    for frac in sweep:
        fr = Image.new("RGB", (w, total_h), (13, 14, 16))
        d = ImageDraw.Draw(fr)
        canvas_y = head_h
        # 底=处理后，左侧贴处理前裁条（clip 宽 = 分割线 x）
        fr.paste(out, (0, canvas_y))
        cut = round(w * frac)
        if cut > 0:
            fr.paste(src.crop((0, 0, cut, h)), (0, canvas_y))
        # 分割线 + 提手
        d.line([(cut, canvas_y), (cut, canvas_y + h)], fill=(79, 140, 255), width=3)
        cy = canvas_y + h // 2
        d.ellipse([cut - 14, cy - 14, cut + 14, cy + 14], fill=(79, 140, 255))
        # 标签随分割线侧别移动，避免被线盖住
        d.text((max(8, cut - 110), canvas_y + 8), "处理前", font=f_small, fill=(255, 255, 255),
               stroke_width=2, stroke_fill=(0, 0, 0))
        d.text((min(w - 150, cut + 14), canvas_y + 8), "处理后", font=f_small, fill=(79, 140, 255),
               stroke_width=2, stroke_fill=(0, 0, 0))
        title = f"super_video · {meta.get('model', '')} · {meta.get('scale', '')}"
        d.text((14, 10), title, font=f_title, fill=(232, 234, 237))
        d.text((14, head_h + h + 10), str(meta.get("name", "")), font=f_small, fill=(120, 126, 132))
        frames.append(fr)

    dest.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(
        dest, "GIF", save_all=True, append_images=frames[1:],
        duration=60, loop=0, optimize=True,
    )
    return dest
