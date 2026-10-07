"""Release regressions: official RRDB RGB/0-1 contract and legacy ONNX migration."""
from pathlib import Path
import numpy as np
import pytest


def test_missing_torch_explains_installation_compatibility(monkeypatch):
    import builtins
    from sv.engines.torch_engine import TorchSrEngine
    original_import=builtins.__import__
    def without_torch(name,*args,**kwargs):
        if name=='torch':raise ModuleNotFoundError("No module named 'torch'")
        return original_import(name,*args,**kwargs)
    monkeypatch.setattr(builtins,'__import__',without_torch)
    with pytest.raises(RuntimeError,match='TensorRT 组件不提供此依赖'):
        TorchSrEngine('unused.pth',4).load()


@pytest.mark.parametrize('named',[True,False])
def test_legacy_graph_removes_only_validated_wrapper(tmp_path, monkeypatch,named):
    import onnx
    from onnx import helper, numpy_helper, TensorProto
    from sv.models.legacy_x4plus import corrected_x4plus
    from sv import paths
    monkeypatch.setattr(paths, 'TEMP_DIR', tmp_path/'cache')
    mean=np.array([.485,.456,.406],dtype=np.float32).reshape(1,3,1,1)
    std=np.array([.229,.224,.225],dtype=np.float32).reshape(1,3,1,1)
    graph=helper.make_graph([
        helper.make_node('Sub',['input','mean'],['sub'],name='sub'),
        helper.make_node('Div',['sub','std'],['div'],name='div'),
        helper.make_node('Identity',['div'],['core'],name='core'),
        helper.make_node('Mul',['core','std'],['mul'],name='mul'),
        helper.make_node('Add',['mul','mean'],['output'],name='add'),
    ],'legacy',[helper.make_tensor_value_info('input',TensorProto.FLOAT,[1,3,4,4])],
    [helper.make_tensor_value_info('output',TensorProto.FLOAT,[1,3,4,4])],
    [numpy_helper.from_array(mean,'mean'),numpy_helper.from_array(std,'std')])
    model=helper.make_model(graph,opset_imports=[helper.make_opsetid('',17)])
    if not named:
        for node in model.graph.node:node.name=''
    model.ir_version=10
    source=tmp_path/'legacy.onnx';onnx.save(model,source)
    original=source.read_bytes();result=corrected_x4plus(source)
    fixed=onnx.load(result)
    assert source.read_bytes()==original
    assert [node.op_type for node in fixed.graph.node]==['Identity','Identity']
    assert not fixed.graph.initializer
    assert corrected_x4plus(source)==result
    assert corrected_x4plus(result)==result


@pytest.mark.parametrize('precision',['fp32','fp16'])
def test_real_x4plus_onnx_matches_official_torch_rgb(precision):
    pytest.importorskip('torch', reason='Optional PyTorch dependency required for real-model comparison')
    from sv.engines.torch_engine import TorchSrEngine
    from sv.engines.onnx_engine import OnnxSrEngine
    from sv.models.registry import get_model,model_file
    spec=get_model('realesrgan-x4plus')
    weight=model_file(spec,4,precision)
    pth=model_file(get_model('realesrgan-x4plus-torch'),4)
    if not weight.exists() or not pth.exists():pytest.skip('Missing real weights')
    frame=np.zeros((32,32,3),dtype=np.uint8)
    frame[:,:16]=[230,50,20];frame[:,16:]=[20,80,220]
    # Explicit CPU for this precision/contract regression; no contention with GPU audit.
    ref=TorchSrEngine(pth,4,tile=128);ref.load()
    ref.device='cpu';ref.model=ref.model.cpu()
    expected=ref.process(frame)
    eng=OnnxSrEngine(weight,4,io=spec.io,device='cpu',u8_wrap=False)
    eng.load();actual=eng.process(frame)
    difference=np.abs(actual.astype('int16')-expected)
    assert difference.mean()<1.2
    assert difference.max()<=3
    assert actual[:,:48,0].mean()>actual[:,:48,2].mean()+100
