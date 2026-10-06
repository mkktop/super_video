"""Guard release fixes: graph semantics, luma color fidelity, unsafe fallback."""
from types import SimpleNamespace
import sys

import numpy as np
import pytest
from PIL import Image

from sv.engines.onnx_engine import OnnxSrEngine


def test_opset_conversion_preserves_pixels_and_source(tmp_path, monkeypatch):
    import onnx
    import onnxruntime as ort
    from onnx import helper, TensorProto, numpy_helper
    from sv.models.dml_compat import directml_compatible
    from sv import paths
    monkeypatch.setattr(paths, 'TEMP_DIR', tmp_path / 'cache')
    weight = numpy_helper.from_array(np.ones((3, 3, 1, 1), dtype=np.float32), 'w')
    graph = helper.make_graph([helper.make_node('Conv', ['x', 'w'], ['y'])], 'conv',
        [helper.make_tensor_value_info('x', TensorProto.FLOAT, [1, 3, 5, 7])],
        [helper.make_tensor_value_info('y', TensorProto.FLOAT, [1, 3, 5, 7])], [weight])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid('', 23)])
    model.ir_version = 10
    source = tmp_path / 'source.onnx'
    onnx.save(model, source)
    before = source.read_bytes()
    converted = directml_compatible(source, 20)
    assert converted != source
    assert source.read_bytes() == before
    assert onnx.load(converted).opset_import[0].version == 20
    x = np.random.default_rng(7).random((1, 3, 5, 7), dtype=np.float32)
    a = ort.InferenceSession(str(source), providers=['CPUExecutionProvider']).run(None, {'x': x})[0]
    b = ort.InferenceSession(str(converted), providers=['CPUExecutionProvider']).run(None, {'x': x})[0]
    np.testing.assert_array_equal(a, b)
    assert directml_compatible(source, 20) == converted
    # A weight update must invalidate the derived cache.
    model.graph.initializer[0].CopyFrom(numpy_helper.from_array(np.full((3,3,1,1), 2, dtype=np.float32), 'w'))
    onnx.save(model, source)
    assert directml_compatible(source, 20) != converted


@pytest.mark.parametrize('fp16', [False, True])
@pytest.mark.parametrize('shape', [(13, 17), (32, 24)])
def test_luma_optimization_preserves_legacy_rgb(fp16, shape):
    rng = np.random.default_rng(21)
    frame = rng.integers(0, 256, (*shape, 3), dtype=np.uint8)[::2, ::2]
    h, w = frame.shape[:2]
    pad = 4
    ph, pw = (-h) % pad, (-w) % pad
    x = np.pad(frame, ((0, ph), (0, pw), (0, 0)), mode='edge')
    r, g, b = (x[..., i].astype(np.float32) / 255 for i in range(3))
    y = 0.299*r + 0.587*g + 0.114*b
    cb = 0.5 - 0.168736*r - 0.331264*g + 0.5*b
    cr = 0.5 + 0.5*r - 0.418688*g - 0.081312*b
    dtype = np.float16 if fp16 else np.float32
    raw = rng.normal(0.5, 0.7, (1,1,(h+ph)*2,(w+pw)*2)).astype(dtype)
    raw_before = raw.copy()
    y2 = raw[0,0].astype(np.float32)
    size = (y2.shape[1], y2.shape[0])
    cb2 = np.asarray(Image.fromarray(cb).resize(size, Image.BICUBIC), dtype=np.float32)
    cr2 = np.asarray(Image.fromarray(cr).resize(size, Image.BICUBIC), dtype=np.float32)
    rgb = np.stack([y2+1.402*(cr2-0.5),
        y2-0.344136*(cb2-0.5)-0.714136*(cr2-0.5), y2+1.772*(cb2-0.5)], axis=-1)*255
    expected = np.clip(rgb,0,255).astype(np.uint8)[:h*2,:w*2]
    eng = OnnxSrEngine('fake.onnx',2,io={'color':'y','pad':pad})
    eng._in_fp16=fp16; eng._in_name='x'; eng._out_names=['y']
    def run(names, inputs):
        np.testing.assert_array_equal(inputs['x'],y[None,None].astype(dtype))
        return [raw]
    eng.session = SimpleNamespace(run=run)
    np.testing.assert_array_equal(eng.process(frame),expected)
    np.testing.assert_array_equal(raw,raw_before)


@pytest.mark.parametrize('provider', ['DmlExecutionProvider','CUDAExecutionProvider'])
def test_cugan_trt_fallback_blocked_before_inference(tmp_path, monkeypatch, provider):
    from sv.server import worker_engine as worker
    from sv.models.registry import get_model
    monkeypatch.setattr(worker, '_ENGINE_CACHE', {})
    monkeypatch.setattr(worker.settings, 'load', lambda: {'engine':'trt'})
    class Fake:
        def __init__(self, *args, **kwargs):
            self.provider_used = [provider, 'CPUExecutionProvider']
        def load(self): pass
        def process(self, frame):
            pytest.fail('Unsafe CUGAN fallback must not execute')
    monkeypatch.setattr(worker, 'OnnxSrEngine', Fake)
    with pytest.raises(RuntimeError, match='TensorRT 未成功启用'):
        worker._load_onnx_engine(tmp_path/'w.onnx',get_model('real-cugan'),2,None,'fp32',0,(32,32))


def test_frozen_catalogue_and_task_guard(monkeypatch):
    from fastapi import HTTPException
    from sv.server.routes import models, tasks
    from sv.models.registry import get_model
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(models, 'cached_hardware', lambda: {'gpus':[]})
    ids = {m['id'] for m in models.get_models()}
    assert 'realesrgan-x4plus-torch' not in ids
    assert 'realesrgan-x4plus' in ids
    with pytest.raises(HTTPException, match='PyTorch'):
        tasks._require_model_runtime(get_model('realesrgan-x4plus-torch'))
