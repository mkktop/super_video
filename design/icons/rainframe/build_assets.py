#!/usr/bin/env python
"""雨帧品牌资产组装：从 design/icons/final/icon@512.png 产出
app/build/icon.ico + icon.png、标题栏 logo.png、安装器 header/sidebar 位图。
用法: .venv/Scripts/python.exe design/icons/rainframe/build_assets.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[3]
FINAL = ROOT / "design" / "icons" / "final"
BUILD = ROOT / "app" / "build"

# 与 rainframe.svg（M1 律动环）同源的品牌色：深水底 + 青→紫→粉霓虹
BG = (6, 26, 38)          # #061a26 深水底（water1 外缘）
PINK, PURPLE, CYAN = (255, 92, 138), (167, 139, 250), (34, 211, 238)
SUB_TEXT = (154, 163, 181)

_FONT_BOLD = r"C:\Windows\Fonts\msyhbd.ttc"


def font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(_FONT_BOLD, size)


def lerp(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def neon(t: float) -> tuple:
    """青→紫→粉 三色插值（t ∈ [0,1]，方向与 iridV 渐变一致）。"""
    return lerp(CYAN, PURPLE, t * 2) if t < 0.5 else lerp(PURPLE, PINK, t * 2 - 1)


def main() -> None:
    icon512 = Image.open(FINAL / "icon@512.png").convert("RGBA")

    # ICO 全尺寸 + 壳内 PNG/标题栏 logo
    icon512.save(BUILD / "icon.ico", sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    icon512.save(BUILD / "icon.png")
    icon512.save(ROOT / "app" / "src" / "renderer" / "src" / "assets" / "logo.png")

    # ---- 安装器顶部条 150x57：暗夜底 + 图标 + 雨帧（整组居中）+ 底部霓虹条 ----
    header = Image.new("RGB", (150, 57), BG)
    d = ImageDraw.Draw(header)
    f20 = font(20)
    tw = d.textlength("雨帧", font=f20)
    hx = round((150 - (38 + 10 + tw)) / 2)  # 图标+间距+字标整组居中
    hic = icon512.resize((38, 38), Image.LANCZOS)
    header.paste(hic, (hx, 8), hic)
    d.text((hx + 48, 14), "雨帧", font=f20, fill=(235, 238, 244))
    for x in range(150):  # 底部 3px 粉→青霓虹条
        d.line([(x, 54), (x, 56)], fill=neon(x / 149))
    header.save(BUILD / "installerHeader.bmp")

    # ---- 安装器侧栏 164x314：暗夜渐变底 + 图标 + 霓虹升柱 + 字标 + 标语 ----
    bar = Image.new("RGB", (164, 314))
    top, bottom = (14, 61, 80), (6, 26, 38)  # #0e3d50 → #061a26（water1 中心→外缘）
    bd = ImageDraw.Draw(bar)
    for y in range(314):
        bd.line([(0, y), (163, y)], fill=lerp(top, bottom, y / 313))
    ic = icon512.resize((88, 88), Image.LANCZOS)
    bar.paste(ic, ((164 - 88) // 2, 34), ic)

    # 升柱：性能/超分叙事，柱色横向走霓虹渐变
    heights = [20, 32, 44, 56, 68, 80]
    w, gap = 14, 9
    x0 = (164 - (len(heights) * w + (len(heights) - 1) * gap)) // 2
    base_y = 222
    for i, h in enumerate(heights):
        x = x0 + i * (w + gap)
        bd.rounded_rectangle([x, base_y - h, x + w, base_y], radius=5, fill=neon(i / (len(heights) - 1)))

    f26, f13 = font(26), font(13)
    for txt, f, yy, col in (("雨帧", f26, 244, (240, 243, 248)), ("AI 视频超分", f13, 280, SUB_TEXT)):
        tw = bd.textlength(txt, font=f)
        bd.text(((164 - tw) / 2, yy), txt, font=f, fill=col)
    bar.save(BUILD / "installerSidebar.bmp")

    print("assets written:", BUILD / "icon.ico", BUILD / "installerHeader.bmp", BUILD / "installerSidebar.bmp")


if __name__ == "__main__":
    main()
