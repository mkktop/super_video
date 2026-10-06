"""Isolated, reproducible probes for release performance investigation."""
from pathlib import Path
import argparse
import json
import sys
import time
from collections import Counter

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
import numpy as np
import onnx
from PIL import Image
from sv.models.registry import load_registry, model_file
from sv.models.fp16 import ensure_fp16_file
from sv.engines.onnx_engine import OnnxSrEngine

p = argparse.ArgumentParser()
p.add_argument('model')
p.add_argument('--fp16', action='store_true')
p.add_argument('--wrap', action='store_true')
p.add_argument('--graph', default='all')
p.add_argument('--profile', action='store_true')
p.add_argument('--weight')
a = p.parse_args()
spec = load_registry()[a.model]
weight = model_file(spec, min(spec.scale))
if a.weight:
    weight = Path(a.weight)
if a.fp16:
    weight = ensure_fp16_file(weight) or weight
out = ROOT / 'reports/model-release-2026-10-06/performance-investigation'
out.mkdir(exist_ok=True)
io = {**spec.io, 'graph_opt': a.graph}
engine = OnnxSrEngine(weight, min(spec.scale), io=io, tile=spec.tile_hint,
                      u8_wrap=a.wrap, manifest_allow_wrap=a.wrap)
if a.profile:
    original = engine._session_options
    def options():
        so = original()
        so.enable_profiling = True
        so.profile_file_prefix = str(out / a.model)
        return so
    engine._session_options = options
engine.load()
fixture = 'illustration' if a.model.startswith('illustration') else 'anime'
frame = np.asarray(Image.open(out.parent / 'fixtures' / (fixture + '.png')).convert('RGB'))
frame = np.asarray(Image.fromarray(frame).resize((856,1280) if fixture=='illustration' else (1280,720)))
engine.process(frame)
times=[]
for _ in range(3):
    start=time.perf_counter(); result=engine.process(frame); times.append((time.perf_counter()-start)*1000)
tag=f'{a.model}-{Path(weight).stem}-{a.graph}-wrap{a.wrap}'
Image.fromarray(result).save(out/(tag+'.png'))
record={'model':a.model,'weight':str(weight),'providers':engine.provider_used,
        'shape':list(frame.shape),'ms':times,'mean_ms':float(np.mean(times)),
        'wrap':engine.u8_wrapped,'graph':a.graph,'output':tag+'.png'}
if a.profile:
    path=engine.session.end_profiling()
    events=json.loads(Path(path).read_text())
    totals=Counter(); counts=Counter()
    for event in events:
        if event.get('cat')=='Node' and event.get('args',{}).get('provider'):
            key=event['args']['provider']+':'+event['args'].get('op_name','')
            totals[key]+=event.get('dur',0);counts[key]+=1
    record['profile']=path;record['operators']=[{'op':k,'total_us':v,'calls':counts[k]} for k,v in totals.most_common(20)]
(out/(tag+'.json')).write_text(json.dumps(record,indent=2),encoding='utf-8')
print(json.dumps(record))
