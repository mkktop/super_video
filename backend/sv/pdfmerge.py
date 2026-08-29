"""批量图片 → 单份 PDF（无损封装）：zlib + 手写 PDF 对象，零第三方依赖。

无损口径：
- PNG（及一切非 JPEG 产物）：解码回 RGB888 像素，PNG 行预测器（None/Sub/Up/
  Avg 逐行择优，向量实现）+ zlib —— 与 PNG 同源的 FlateDecode，逐像素一致；
  Paeth 略去：向量实现繁琐，对 SR 产物（大片平涂+硬边缘）增益可忽略。
- JPEG 产物：文件字节原样直嵌 DCTDecode——不二次压缩，画质与 .jpg 文件
  逐字节一致。
- 页面尺寸 = 图像像素数（1px = 1pt），超 PDF 单边上限 14400pt 等比缩小——
  只改页面几何（cm 矩阵缩放），图像流本身不受影响、依旧无损。
"""
from __future__ import annotations

import os
import zlib
from pathlib import Path

import numpy as np
from PIL import Image

MAX_PAGE_PT = 14400  # PDF 规格 MediaBox 单边上限（UserUnit=1 时）


def _flate_image(arr: np.ndarray) -> bytes:
    """RGB888 数组 → PNG 行预测器 + zlib 流（PDF /Predictor 15 兼容格式）。

    逐行在 None/Sub/Up/Avg 里按 |差值| 总和最小择优（PNG 规格建议的启发式），
    每行首字节是过滤器类型——与 PNG IDAT 行布局完全一致，解码侧可复用
    同一套 unfilter 逻辑。
    """
    h, w, c = arr.shape
    cur = np.ascontiguousarray(arr).reshape(h, w * c)
    prev = np.vstack([np.zeros((1, w * c), np.uint8), cur[:-1]])
    left = np.hstack([np.zeros((h, c), np.uint8), cur[:, :-c]])
    # Average 预测子 (left+up)/2：先升 int16 防 uint8 相加回绕
    pred = ((left.astype(np.int16) + prev) >> 1).astype(np.uint8)
    cand = {0: cur, 1: cur - left, 2: cur - prev, 3: cur - pred}
    scores = np.stack([np.abs(f.astype(np.int16)).sum(axis=1) for f in cand.values()])
    choice = scores.argmin(axis=0)  # (h,) 每行的过滤器类型
    rows = np.empty((h, 1 + w * c), np.uint8)
    rows[:, 0] = choice
    for ft, data in cand.items():
        sel = choice == ft
        rows[sel, 1:] = data[sel]
    return zlib.compress(rows.tobytes(), 9)


def _num(v: float) -> str:
    """PDF 数值：整数值不带小数点（MediaBox/cm 里更干净）"""
    return str(int(v)) if float(v).is_integer() else f"{v:.2f}"


def write_pdf(image_paths: list[Path], out_path: Path) -> dict:
    """把一组图片文件封装为单份 PDF（原子落盘 .part+replace）。

    页序 = 传入顺序。返回 {pages, bytes}；任一图片解码失败直接抛（调用方
    定夺失败语义——worker 侧图片已全部落盘，合并失败应显式报错而非静默）。
    """
    if not image_paths:
        raise ValueError("PDF 页列表为空")
    pages: list[dict] = []
    for p in image_paths:
        with Image.open(p) as im:  # 先只借头信息判型量尺寸
            size = im.size
            is_jpeg = im.format == "JPEG" and im.mode == "RGB"
        if is_jpeg:
            stream, filt, parms = p.read_bytes(), b"/DCTDecode", b""
        else:
            with Image.open(p) as im:
                arr = np.asarray(im.convert("RGB"), dtype=np.uint8)
            stream = _flate_image(arr)
            filt = b"/FlateDecode"
            parms = (b"/DecodeParms << /Predictor 15 /Colors 3 "
                     b"/BitsPerComponent 8 /Columns %d >> " % size[0])
        w, h = size
        scale = min(1.0, MAX_PAGE_PT / max(w, h))
        pages.append({"w": w, "h": h, "pw": w * scale, "ph": h * scale,
                      "stream": stream, "filter": filt, "parms": parms})

    bodies: list[bytes] = []  # 对象体（1 基编号；不含 "N 0 obj"/endobj 外壳）

    def add(body: bytes) -> int:
        bodies.append(body)
        return len(bodies)

    add(b"")  # 占位：1=Catalog 2=Pages 由下面两行写入
    add(b"")
    kids: list[str] = []
    for pg in pages:
        img_id = add(
            b"<< /Type /XObject /Subtype /Image /Width %d /Height %d "
            b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter %s %s/Length %d >>\n"
            b"stream\n" % (pg["w"], pg["h"], pg["filter"], pg["parms"], len(pg["stream"]))
            + pg["stream"] + b"\nendstream")
        content = (f"q {_num(pg['pw'])} 0 0 {_num(pg['ph'])} 0 0 cm /Im0 Do Q\n"
                   ).encode("ascii")
        cont_id = add(b"<< /Length %d >>\nstream\n" % len(content)
                      + content + b"\nendstream")
        page_id = add(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_num(pg['pw'])} {_num(pg['ph'])}] "
            f"/Resources << /XObject << /Im0 {img_id} 0 R >> >> /Contents {cont_id} 0 R >>"
            .encode("ascii"))
        kids.append(f"{page_id} 0 R")
    bodies[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    bodies[1] = (f"<< /Type /Pages /Count {len(pages)} /Kids [{' '.join(kids)}] >>"
                 .encode("ascii"))

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")  # 二进制标记行：按规格建议携带高位字节
    offsets: list[int] = []
    for i, body in enumerate(bodies, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(bodies) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode("ascii")
    out += (f"trailer\n<< /Size {len(bodies) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_at}\n%%EOF\n").encode("ascii")

    tmp = out_path.with_name(out_path.name + f".{os.getpid()}.part")
    try:
        tmp.write_bytes(bytes(out))
        os.replace(tmp, out_path)  # 原子：中断不留半个 PDF
    finally:
        tmp.unlink(missing_ok=True)
    return {"pages": len(pages), "bytes": len(out)}
