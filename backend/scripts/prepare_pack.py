"""准备打包资源：把 x4plus 动态 ONNX 放进 pack/（安装包 extraResources 来源）。

用法:
    python scripts/prepare_pack.py          # 从 models_store 拷贝（开发机）
    CI 里由 export_onnx_x4plus.py 直接产出到 pack/，不经过本脚本。

sha256 与 manifest 严格校验，不一致即失败（防止把错文件打进安装包）。
"""
from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sv.models.registry import get_model
from sv.paths import MODELS_DIR

PACK_ROOT = Path(__file__).resolve().parents[2] / "pack"


def main() -> None:
    spec = get_model("realesrgan-x4plus")
    f = spec.files[0]
    src = MODELS_DIR / "realesrgan-x4plus" / f["name"]
    if not src.exists():
        sys.exit(f"缺少 {src}（先跑一次任务或手动下载）")

    h = hashlib.sha256(src.read_bytes()).hexdigest()
    if h != f["sha256"]:
        sys.exit(f"sha256 不符: {h}")

    dst = PACK_ROOT / "models_store" / "realesrgan-x4plus" / f["name"]
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    print(f"pack 就绪: {dst} ({dst.stat().st_size // 1024 // 1024}MB)")


if __name__ == "__main__":
    main()
