"""把 fp32 ONNX 模型转成 fp16（内部权重/计算减半，IO 保持 fp32）。

用法:
    python scripts/convert_fp16.py <model.onnx> [model_fp16.onnx]

转换规则与 sv/models/fp16.py（下载后自动转换）保持一致。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sv.models.fp16 import convert_file


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_name(src.stem + "_fp16.onnx")
    convert_file(src, dst)
    print(f"{dst}: {src.stat().st_size/1e6:.2f}MB -> {dst.stat().st_size/1e6:.2f}MB")


if __name__ == "__main__":
    main()
