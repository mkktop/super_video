"""FP16 模型变体：转换 + 兄弟文件解析。

约定：fp16 权重与 fp32 原件同名，加 `_fp16` 后缀放在同一目录。
  RealESR-AnimeVideo-v3_x4.onnx -> RealESR-AnimeVideo-v3_x4_fp16.onnx

内置权重是 AnimeJaNai V3.1（原生 fp16，无需转换）；下载模型在服务端下载完成后
自动转换缓存。质量基准见 BENCH.md：fp16 相对 fp32 PSNR 74~75dB（数值级一致），
DML 提速 1.36~1.73x。
"""
from __future__ import annotations

from pathlib import Path

from .registry import ModelSpec, model_dir

# Clip 的 fp16 实现在 DML 上会崩（MLOperatorAuthorImpl 0x8007023E），
# 逐元素廉价算子保 fp32 不影响性能；卷积等重头算子仍走 fp16。
_BLOCK_LIST_EXTRA = ["Clip"]


def fp16_path(fp32_file: Path) -> Path:
    return fp32_file.with_name(fp32_file.stem + "_fp16.onnx")


def ensure_fp16_file(fp32_file: Path) -> Path:
    """单文件的 fp16 兄弟保障：缺失则就地转换一次，失败回退 fp32 原件。

    bundled 目录视为只读（fp16 变体随包分发）；转换依赖缺失
    （onnx/onnxconverter-common，如 CUDA 独立环境）同样回退。
    """
    from .registry import BUNDLED_DIR

    alt = fp16_path(fp32_file)
    if alt.exists() or BUNDLED_DIR in fp32_file.parents:
        return alt if alt.exists() else fp32_file
    try:
        convert_file(fp32_file, alt)
        return alt
    except Exception:  # noqa: BLE001 — 任何转换问题都优雅回退 fp32
        return fp32_file


def convert_file(src: Path, dst: Path) -> None:
    """fp32 -> fp16，IO 保持 fp32（引擎代码零改动）。

    tmp + 原子换名：中途崩溃/磁盘满只留孤 .tmp，不会留下半写的 fp16 文件被
    ensure_fp16_file 当有效变体永久使用（那会让之后所有 fp16 任务加载失败且
    不自愈）。tmp 名带 pid——双路并行两进程可能同时转换同一权重。
    """
    import os

    from onnx import load_model
    from onnxconverter_common.float16 import (
        DEFAULT_OP_BLOCK_LIST,
        convert_float_to_float16,
    )

    m = load_model(str(src))
    m16 = convert_float_to_float16(
        m, keep_io_types=True,
        op_block_list=list(DEFAULT_OP_BLOCK_LIST) + _BLOCK_LIST_EXTRA,
    )
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f"{dst.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(m16.SerializeToString())
        tmp.replace(dst)
    finally:
        tmp.unlink(missing_ok=True)  # 换名成功后 tmp 已不存在；失败清残片


def ensure_fp16(spec: ModelSpec) -> list[Path]:
    """为 models_store 中已就绪的 fp32 权重生成缺失的 fp16 兄弟文件。

    已有兄弟的跳过；bundled 目录只读不动。返回本次生成的文件列表。
    onnx/onnxconverter-common 缺失时抛 ImportError 由调用方降级。
    """
    made: list[Path] = []
    d = model_dir(spec.id)
    if not d.exists() or spec.engine != "onnx" or not spec.fp16:
        return made
    for f in spec.files:
        if "name" not in f:
            continue
        src = d / f["name"]
        dst = fp16_path(src)
        if src.exists() and not dst.exists():
            convert_file(src, dst)
            made.append(dst)
    return made
