"""Manifest-scoped ONNX conversion for DirectML operator support."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path


def directml_compatible(source: Path, target_opset: int) -> Path:
    """Cache a converted copy; leave downloaded weights and their hashes intact.

    Illustration SPAN exports Conv-22 (opset 23), which this DirectML runtime
    assigns to CPU. ONNX's version converter adapts the graph to opset 20.
    This is opt-in per manifest, never a blanket opset-number rewrite.
    """
    import onnx
    from ..paths import TEMP_DIR

    with source.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    key = hashlib.sha256(f'{digest}:{target_opset}:{onnx.__version__}:1'.encode()).hexdigest()
    cache = TEMP_DIR / 'dml_compat' / f'{source.stem}-{key[:24]}.onnx'
    if cache.exists():
        return cache
    model = onnx.load(str(source))
    current = next(o.version for o in model.opset_import if o.domain in ('', 'ai.onnx'))
    if current <= target_opset:
        return source
    model = onnx.version_converter.convert_version(model, target_opset)
    onnx.checker.check_model(model)
    cache.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache.with_name(f'{cache.name}.{os.getpid()}.tmp')
    try:
        onnx.save(model, str(temporary))
        temporary.replace(cache)
    finally:
        temporary.unlink(missing_ok=True)
    return cache
