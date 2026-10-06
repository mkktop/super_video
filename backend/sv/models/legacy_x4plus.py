"""Correct the historical x4plus export without changing downloaded model assets.

The old export incorrectly wrapped the official RRDBNet in ImageNet normalization.
Only the exact, validated wrapper is removed. Corrected exports pass through.
"""
from pathlib import Path
import hashlib
import os


def corrected_x4plus(source: Path) -> Path:
    import numpy as np
    import onnx
    from onnx import helper, numpy_helper
    from ..paths import TEMP_DIR

    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    cache = TEMP_DIR / 'corrected_models' / f'{source.stem}_rgb01_{digest}.onnx'
    if cache.exists():
        return cache
    model = onnx.load(str(source))
    nodes = list(model.graph.node)
    tensors = {t.name: numpy_helper.to_array(t) for t in model.graph.initializer}
    subs = [n for n in nodes if n.op_type == 'Sub' and list(n.input)[-1:] == ['mean']]
    if not subs:
        return source  # New export already uses the official 0-1 RGB contract.
    if len(subs) != 1 or not all(k in tensors for k in ('mean', 'std')):
        raise ValueError('Unrecognized legacy x4plus normalization wrapper')
    if not (np.allclose(tensors['mean'].reshape(-1), [.485, .456, .406], atol=.001)
            and np.allclose(tensors['std'].reshape(-1), [.229, .224, .225], atol=.001)):
        raise ValueError('Unexpected legacy x4plus normalization constants')
    sub = subs[0]
    divs = [n for n in nodes if n.op_type == 'Div' and list(n.input) == [sub.output[0], 'std']]
    adds = [n for n in nodes if n.op_type == 'Add' and list(n.input)[-1:] == ['mean']]
    if len(divs) != 1 or len(adds) != 1:
        raise ValueError('Incomplete legacy x4plus normalization wrapper')
    div, add = divs[0], adds[0]
    muls = [n for n in nodes if n.op_type == 'Mul' and list(n.output) == [add.input[0]]
            and list(n.input)[-1:] == ['std']]
    if len(muls) != 1:
        raise ValueError('Missing legacy x4plus output normalization')
    mul = muls[0]
    keep = []
    for node in nodes:
        if node is sub or node is div or node is mul:
            continue
        if node is add:
            node = helper.make_node('Identity', [mul.input[0]], list(add.output), name='OfficialRGB01Output')
        for i, name in enumerate(node.input):
            if name == div.output[0]:
                node.input[i] = sub.input[0]
        keep.append(node)
    del model.graph.node[:]
    model.graph.node.extend(keep)
    used = {name for node in keep for name in node.input}
    initializers = [t for t in model.graph.initializer if t.name in used]
    del model.graph.initializer[:]
    model.graph.initializer.extend(initializers)
    deleted = {sub.output[0], div.output[0], mul.output[0]}
    value_info = [v for v in model.graph.value_info if v.name not in deleted]
    del model.graph.value_info[:]
    model.graph.value_info.extend(value_info)
    onnx.checker.check_model(model)
    cache.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache.with_name(f'{cache.stem}.{os.getpid()}.tmp.onnx')
    onnx.save(model, str(temporary))
    temporary.replace(cache)
    return cache
