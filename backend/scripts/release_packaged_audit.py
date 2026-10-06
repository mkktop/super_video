"""Validate a freshly built frozen sidecar without touching the user's queue/settings.

Weights are shared through a verified junction; outputs, DB, settings and caches are isolated.
This is a sidecar integration audit, not Electron installer/UI acceptance.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'backend'))


def run(exe,out):
    data=out/'packaged-data'
    # Create this junction explicitly before running; never move or delete the target.
    shared=data/'models_store'
    if not shared.exists() or shared.resolve()!=(ROOT/'models_store').resolve():
        raise RuntimeError('Expected models_store junction to the existing workspace weights')
    os.environ.update(SV_DATA=str(data),SV_ROOT=str(data),SV_DB=str(data/'queue.db'),
                      SV_FFMPEG=str(ROOT/'bin/ffmpeg.exe'),SV_FFPROBE=str(ROOT/'bin/ffprobe.exe'),
                      PYTHONUTF8='1',PYTHONIOENCODING='utf-8')
    from PIL import Image
    import numpy as np
    from sv.server import db,settings
    from sv.models.registry import load_registry
    from sv.utils.process import WINDOWS_CREATE_FLAGS
    db.init_db()
    logs=out/'packaged-logs';logs.mkdir(exist_ok=True)
    check=subprocess.run([str(exe),'selftest'],capture_output=True,env=os.environ,creationflags=WINDOWS_CREATE_FLAGS)
    (out/'packaged-selftest.json').write_text(check.stdout.decode('utf-8','replace'),encoding='utf-8')
    if check.returncode:raise RuntimeError('Frozen selftest failed: '+check.stdout.decode('utf-8','replace'))
    results=out/'packaged-results.jsonl'
    existing={r['model']:r for r in (json.loads(l) for l in results.read_text(encoding='utf-8').splitlines())} if results.exists() else {}
    directory=out/'packaged-outputs';directory.mkdir(exist_ok=True)
    source=directory/'diagnostic.png'
    Image.open(out/'fixtures/diagnostic.png').resize((128,128)).save(source)
    for spec in load_registry().values():
        if spec.id in existing and existing[spec.id]['status'] in ('ok','blocked_dependency'):continue
        print(spec.id,flush=True)
        scale=min(spec.scale)
        settings.save({'engine':'cpu' if 'cugan' in spec.id else 'auto','precision':'fp16',
                       'parallel_streams':False,'delete_source_after_done':False})
        if spec.kind=='interp':
            clip=directory/'rife_input.mp4'
            subprocess.run([str(ROOT/'bin/ffmpeg.exe'),'-y','-loglevel','error','-i',
                str(next((ROOT/'samples').glob('*1.mp4'))),'-vf','scale=256:144','-frames:v','6',
                '-an','-c:v','libx264',str(clip)],check=True,creationflags=WINDOWS_CREATE_FLAGS)
            target=directory/'rife.mp4'
            task=db.new_task(str(clip),str(target),'animejanai-v31-hd-balanced-sharp',
                {'scale':2,'interp':'rife2x','codec':'h264','crf':18,'kind':'video','hw_decode':False})
        else:
            target=directory/f'{spec.id}.png'
            if target.exists():target.unlink()
            task=db.new_task(str(source),str(target),spec.id,
                {'kind':'image','scale':scale,'tile':spec.tile_hint or 256,'format':'png'})
        tick=time.perf_counter()
        with (logs/f'{spec.id}.txt').open('wb') as stream:
            try:
                proc=subprocess.run([str(exe),'worker',task['id']],env=os.environ,
                    stdout=stream,stderr=subprocess.STDOUT,timeout=240,creationflags=WINDOWS_CREATE_FLAGS)
                code=proc.returncode
            except subprocess.TimeoutExpired:code=-1
        events=[]
        for line in (logs/f'{spec.id}.txt').read_text(encoding='utf-8',errors='replace').splitlines():
            try:events.append(json.loads(line.replace('\x00','').strip()))
            except ValueError:pass
        done=any(e.get('type')=='done' for e in events)
        failures=[e.get('error') for e in events if e.get('type')=='failed']
        row={'model':spec.id,'engine':spec.engine,'exit_code':code,'done_event':done,
             'wall_s':time.perf_counter()-tick,'status':'ok' if code==0 and done and target.exists() else 'error',
             'errors':failures,'frozen':True,'backend':'cpu' if 'cugan' in spec.id else 'directml'}
        if row['status']=='ok':
            try:
                if spec.kind=='interp':
                    from sv.pipeline.probe import probe
                    info=probe(target)
                    assert info.total_frames==12 and abs(info.fps-60)<.01
                    assert (info.width,info.height)==(512,288)
                    subprocess.run([str(ROOT/'bin/ffmpeg.exe'),'-v','error','-xerror','-i',str(target),
                        '-f','null','-'],check=True,capture_output=True,creationflags=WINDOWS_CREATE_FLAGS)
                else:
                    with Image.open(target) as im:
                        assert im.size==(128*scale,128*scale)
                        rgb=np.asarray(im.convert('RGB')).astype(float)
                        for channel,x in ((0,5),(1,30),(2,55)):
                            patch=rgb[20*scale:30*scale,x*scale:(x+10)*scale].mean(axis=(0,1))
                            if spec.category=='manga_bw':
                                assert patch.max()-patch.min()<5
                            else:
                                assert patch[channel]>max(patch[(channel+1)%3],patch[(channel+2)%3])+5
                row['output_verified']=True
            except Exception as exc:
                row.update(status='error',verification_error=str(exc))
        if spec.engine=='torch' and code!=0 and any('未安装 PyTorch' in (e or '') for e in failures):
            row['status']='blocked_dependency'
        with results.open('a',encoding='utf-8') as stream:stream.write(json.dumps(row,ensure_ascii=False)+'\n')
        print('  '+row['status'],flush=True)


if __name__=='__main__':
    ap=argparse.ArgumentParser()
    ap.add_argument('--exe',default=str(ROOT/'backend/dist/sidecar/sidecar.exe'))
    ap.add_argument('--out',default=str(ROOT/'reports/model-release-2026-10-06'))
    args=ap.parse_args();run(Path(args.exe).resolve(),Path(args.out).resolve())
