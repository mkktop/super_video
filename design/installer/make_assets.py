# -*- coding: utf-8 -*-
"""生成 NSIS 安装器美术素材与中文许可协议。

产物（写进 app/build/，electron-builder 的 buildResources 目录）：
- installerSidebar.bmp  164×314 欢迎页左侧立绘（MUI_WELCOMEFINISHPAGE_BITMAP）
- installerHeader.bmp   150×57  内页右上页头图（MUI_HEADERIMAGE_BITMAP）
- license_zh.rtf        许可协议页（RTF，中文以 \\uN 转义成 7-bit，规避编码问题）

预览 PNG 输出到本目录 preview/ 供人工/审查查看，安装包只用上面的 bmp/rtf。
运行：python design/installer/make_assets.py
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "app" / "build"
PREVIEW = Path(__file__).resolve().parent / "preview"
ICON = BUILD / "icon.png"

# 取自应用图标实测主色（icon.png 高频色）
NAVY_TOP = (36, 51, 86)     # #243356
NAVY_BOTTOM = (19, 27, 48)  # #131B30
CYAN = (61, 183, 238)       # #3DB7EE
BLUE = (74, 105, 166)       # #4A69A6
INK = (35, 47, 75)          # #232F4B 图标底色/页头文字

FONT_DIR = "C:/Windows/Fonts"

LICENSE_CLAUSES = [
    "1. 授权范围：本软件仅供最终用户在遵守本协议的前提下免费安装与使用，未经授权不得用于商业转售或再分发。",
    "2. 现状提供：本软件按\u201c现状\u201d与\u201c现有\u201d基础提供，不附带任何明示或默示的担保，开发者不承诺软件完全无缺陷或不中断运行。",
    "3. 责任限制：在适用法律允许的最大范围内，开发者不对因使用或无法使用本软件而造成的任何直接或间接损失（包括但不限于数据丢失、业务中断）承担责任。",
    "4. 数据与隐私：本软件在您的计算机本地处理视频与设置数据，不会上传您所处理的视频内容。",
    "5. 第三方组件：本软件基于 Electron、FFmpeg 等第三方开源组件构建，相关组件的版权归各自作者所有，并遵循其各自的开源许可协议。",
    "6. 协议变更：开发者保留随时修订本协议的权利；修订后继续使用本软件，即视为接受修订后的协议。",
    "7. 完整条款：本协议构成双方就本软件使用达成的完整约定。",
]


def font(name: str, size: float) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(f"{FONT_DIR}/{name}", size)


def vertical_gradient(size: tuple[int, int], top, bottom) -> Image.Image:
    w, h = size
    img = Image.new("RGB", size)
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / (h - 1)
        d.line([(0, y), (w, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)))
    return img


def radial_glow(size: tuple[int, int], center: tuple[int, int], radius: int,
                peak: int, blur: int) -> Image.Image:
    """以 L 通道高斯光斑作为 alpha 的纯色 RGBA 层。"""
    mask = Image.new("L", size, 0)
    d = ImageDraw.Draw(mask)
    cx, cy = center
    d.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=peak)
    mask = mask.filter(ImageFilter.GaussianBlur(blur))
    layer = Image.new("RGBA", size, CYAN + (255,))
    layer.putalpha(mask)
    return layer


def make_sidebar() -> Image.Image:
    W, H = 164, 314
    base = vertical_gradient((W, H), NAVY_TOP, NAVY_BOTTOM).convert("RGBA")
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)

    # 图标背后的青色氛围光
    overlay = Image.alpha_composite(overlay, radial_glow((W, H), (82, 92), 105, 95, 42))
    d = ImageDraw.Draw(overlay)

    # 应用图标
    icon = Image.open(ICON).convert("RGBA").resize((88, 88), Image.LANCZOS)
    base.alpha_composite(overlay)
    base.alpha_composite(icon, (38, 48))

    # 「超分阶梯」：自左向右升高的圆角竖条，低清→高清
    motif = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dm = ImageDraw.Draw(motif)
    heights = (26, 44, 62, 80, 98)
    baseline = 248
    for i, hh in enumerate(heights):
        x0 = 24 + i * 24
        t = i / (len(heights) - 1)
        r = round(120 + (CYAN[0] - 120) * t)
        g = round(145 + (CYAN[1] - 145) * t)
        b = round(185 + (CYAN[2] - 185) * t)
        a = round(110 + 120 * t)
        dm.rounded_rectangle((x0, baseline - hh, x0 + 16, baseline), radius=8, fill=(r, g, b, a))
    dm.rounded_rectangle((21, baseline + 3, 143, baseline + 5), radius=1, fill=(96, 118, 158, 110))
    base.alpha_composite(motif)

    # 名称与标语
    dt = ImageDraw.Draw(base)
    f_title = font("msyhbd.ttc", 19)
    f_tag = font("msyh.ttc", 11)
    dt.text((82, 265), "super_video", font=f_title, fill=(255, 255, 255), anchor="ma")
    dt.text((82, 291), "AI 视频超分辨率", font=f_tag, fill=(172, 195, 231), anchor="ma")
    return base


def make_header() -> Image.Image:
    W, H = 150, 57
    base = Image.new("RGBA", (W, H), (255, 255, 255, 255))
    icon = Image.open(ICON).convert("RGBA").resize((38, 38), Image.LANCZOS)
    base.alpha_composite(icon, (9, 9))
    d = ImageDraw.Draw(base)
    d.text((54, 28), "super_video", font=font("msyhbd.ttc", 12), fill=INK, anchor="lm")
    bar = vertical_gradient((W, 5), CYAN, BLUE)
    base.paste(bar, (0, H - 5))
    return base


def make_license_rtf() -> str:
    title = "软件许可协议（最终用户）"
    clauses = LICENSE_CLAUSES
    closing = "点击\u201c我接受\u201d并继续安装，即表示您已阅读并同意本协议的全部内容。"

    def esc(text: str) -> str:
        out = []
        for ch in text:
            if ch in "\\{}":
                out.append("\\" + ch)
            elif ord(ch) < 128:
                out.append(ch)
            else:
                out.append(f"\\u{ord(ch)}?")
        return "".join(out)

    parts = [
        r"{\rtf1\ansi\deff0{\fonttbl{\f0\fswiss\fcharset134 Microsoft YaHei;}}",
        r"\viewkind4\uc1\pard\sa140\sl300\fs22\b " + esc(title) + r"\b0\par",
    ]
    for c in clauses:
        parts.append(r"\pard\sa140\fs20 " + esc(c) + r"\par")
    parts.append(r"\pard\sa140\fs20\b " + esc(closing) + r"\b0\par")
    parts.append("}")
    return "".join(parts)


def make_mock_welcome(sidebar: Image.Image, header: Image.Image) -> Image.Image:
    """拼一张近似 NSIS 欢迎页的预览，供评估整体观感（非安装产物）。"""
    W, H = 500, 360
    img = Image.new("RGBA", (W, H), (255, 255, 255, 255))
    img.paste(sidebar.convert("RGB"), (0, 0))
    img.paste(header.convert("RGB"), (W - 150, 0))
    d = ImageDraw.Draw(img)
    d.text((190, 60), "欢迎来到 super_video 安装向导", font=font("msyhbd.ttc", 16), fill=(31, 41, 61))
    d.text((190, 110), "本向导将指引你完成 super_video 的安装。", font=font("msyh.ttc", 12), fill=(90, 100, 120))
    d.text((190, 132), "建议在继续前关闭其他应用程序。", font=font("msyh.ttc", 12), fill=(90, 100, 120))
    _mock_buttons(d, ("下一步(N)", "取消(C)"))
    return img


def _mock_button(d: ImageDraw.ImageDraw, x: int, y: int, w: int, label: str) -> None:
    d.rounded_rectangle((x, y, x + w, y + 28), radius=3, fill=(238, 241, 246), outline=(160, 165, 175), width=1)
    d.text((x + w / 2, y + 14), label, font=font("msyh.ttc", 11), fill=(60, 66, 78), anchor="mm")


def _mock_buttons(d: ImageDraw.ImageDraw, labels: tuple[str, ...]) -> None:
    x = 500 - 14 - len(labels) * 92
    for label in labels:
        _mock_button(d, x, 312, 88, label)
        x += 92


def _mock_inner_page(header: Image.Image, title: str) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    """内页骨架：白底 + 右上页头图 + 左上大标题（按钮由各页自画）。"""
    img = Image.new("RGBA", (500, 360), (255, 255, 255, 255))
    img.paste(header.convert("RGB"), (500 - 150, 0))
    d = ImageDraw.Draw(img)
    d.text((25, 75), title, font=font("msyhbd.ttc", 15), fill=(31, 41, 61))
    return img, d


def make_mock_license(header: Image.Image) -> Image.Image:
    img, d = _mock_inner_page(header, "许可协议")
    d.text((25, 108), "在安装之前，请阅读以下许可协议条款。", font=font("msyh.ttc", 11), fill=(96, 104, 120))
    d.rounded_rectangle((25, 128, 475, 262), radius=2, fill=(255, 255, 255), outline=(196, 202, 212), width=1)
    y = 138
    for c in LICENSE_CLAUSES[:4]:
        d.text((35, y), c[:38], font=font("msyh.ttc", 10), fill=(70, 76, 90))
        d.text((35, y + 16), c[38:76], font=font("msyh.ttc", 10), fill=(70, 76, 90))
        y += 34
    for i, (checked, label) in enumerate(((True, "我接受这个协议(A)"), (False, "我不接受这个协议(D)"))):
        cy = 282 + i * 22
        d.ellipse((25, cy, 37, cy + 12), outline=(110, 118, 132), width=1)
        if checked:
            d.ellipse((28, cy + 3, 34, cy + 9), fill=(28, 118, 210))
        d.text((46, cy + 5), label, font=font("msyh.ttc", 11), fill=(50, 56, 68), anchor="lm")
    _mock_buttons(d, ("下一步(N)", "取消(C)"))
    return img


def make_mock_dir(header: Image.Image) -> Image.Image:
    img, d = _mock_inner_page(header, "选择安装位置")
    d.text((25, 110), "安装程序将把 super_video 安装到以下位置。", font=font("msyh.ttc", 11), fill=(96, 104, 120))
    d.rounded_rectangle((25, 150, 375, 178), radius=2, fill=(255, 255, 255), outline=(160, 165, 175), width=1)
    d.text((33, 163), "C:\\Users\\28640\\AppData\\Local\\Programs\\super_video",
           font=font("msyh.ttc", 11), fill=(40, 46, 60), anchor="lm")
    _mock_button(d, 385, 150, 90, "浏览(B)")
    d.text((25, 200), "安装 super_video 至少需要 620 MB 磁盘空间。", font=font("msyh.ttc", 10), fill=(110, 118, 132))
    d.text((25, 218), "可用空间：345.2 GB", font=font("msyh.ttc", 10), fill=(110, 118, 132))
    _mock_buttons(d, ("安装(I)", "取消(C)"))
    return img


def make_mock_install(header: Image.Image) -> Image.Image:
    img, d = _mock_inner_page(header, "正在安装 super_video")
    d.text((25, 120), "正在安装，请稍候…", font=font("msyh.ttc", 11), fill=(96, 104, 120))
    d.rounded_rectangle((25, 160, 475, 180), radius=4, fill=(240, 242, 246), outline=(180, 186, 196), width=1)
    d.rounded_rectangle((26, 161, 26 + int(448 * 0.42), 179), radius=3, fill=(56, 132, 214))
    d.text((250, 200), "42%", font=font("msyh.ttc", 10), fill=(110, 118, 132), anchor="ma")
    d.text((25, 250), "正在解压：resources\\sidecar\\sidecar.exe", font=font("msyh.ttc", 10), fill=(130, 138, 150))
    _mock_buttons(d, ("取消(C)",))
    return img


def make_mock_finish(sidebar: Image.Image) -> Image.Image:
    img = Image.new("RGBA", (500, 360), (255, 255, 255, 255))
    img.paste(sidebar.convert("RGB"), (0, 0))
    d = ImageDraw.Draw(img)
    d.text((190, 60), "super_video 安装向导完成", font=font("msyhbd.ttc", 16), fill=(31, 41, 61))
    d.text((190, 110), "安装向导已完成 super_video 的安装。", font=font("msyh.ttc", 12), fill=(90, 100, 120))
    d.text((190, 132), "点击\u201c完成\u201d退出向导。", font=font("msyh.ttc", 12), fill=(90, 100, 120))
    d.rounded_rectangle((190, 170, 203, 183), radius=2, fill=(255, 255, 255), outline=(110, 118, 132), width=1)
    d.text((210, 176), "运行 super_video(R)", font=font("msyh.ttc", 11), fill=(50, 56, 68), anchor="lm")
    _mock_buttons(d, ("完成(F)",))
    return img


def main() -> int:
    BUILD.mkdir(exist_ok=True)
    PREVIEW.mkdir(exist_ok=True)

    sidebar = make_sidebar()
    header = make_header()
    sidebar.convert("RGB").save(BUILD / "installerSidebar.bmp")
    header.convert("RGB").save(BUILD / "installerHeader.bmp")

    (BUILD / "license_zh.rtf").write_bytes(make_license_rtf().encode("ascii"))

    sidebar.save(PREVIEW / "sidebar.png")
    header.save(PREVIEW / "header.png")
    make_mock_welcome(sidebar, header).save(PREVIEW / "mock_welcome.png")
    make_mock_license(header).save(PREVIEW / "mock_license.png")
    make_mock_dir(header).save(PREVIEW / "mock_dir.png")
    make_mock_install(header).save(PREVIEW / "mock_install.png")
    make_mock_finish(sidebar).save(PREVIEW / "mock_finish.png")

    for f in ("installerSidebar.bmp", "installerHeader.bmp", "license_zh.rtf"):
        p = BUILD / f
        print(f"{f}: {p.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
