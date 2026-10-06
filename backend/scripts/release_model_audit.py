"""Registry-driven release audit. Every GPU job runs in an isolated subprocess.

Run with the normal venv. The coordinator chooses the CUDA venv for TRT/torch.
Results are append-only and resumable; existing successful jobs are not repeated.
Measurements describe this host, these sizes, and these fixtures, not a universal ranking.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import statistics
import shutil
import subprocess
import sys
import threading
import time
import traceback

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from sv.models.registry import load_registry, model_file, auto_variant
from sv.paths import ffmpeg_bin
from sv.utils.process import WINDOWS_CREATE_FLAGS


def dump(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')


def prepare(out):
    fixtures = out / 'fixtures'
    fixtures.mkdir(parents=True, exist_ok=True)
    anime = fixtures / 'anime.png'
    if not anime.exists():
        src = next((ROOT / 'samples').glob('*1.mp4'))
        subprocess.run([ffmpeg_bin(), '-y', '-loglevel', 'error', '-ss', '1', '-i', str(src),
                        '-frames:v', '1', str(anime)], check=True, creationflags=WINDOWS_CREATE_FLAGS)
    if not (fixtures/'motion01.png').exists():
        src = next((ROOT/'samples').glob('*1.mp4'))
        subprocess.run([ffmpeg_bin(), '-y', '-loglevel', 'error', '-ss', '0.5', '-i', str(src),
                        '-vf', 'fps=8,scale=256:144', '-frames:v', '7', str(fixtures/'motion%02d.png')],
                       check=True, creationflags=WINDOWS_CREATE_FLAGS)
    previous_sources=json.loads((out/'sources.json').read_text(encoding='utf-8')) if (out/'sources.json').exists() else {}
    sources = {**previous_sources, 'anime': str(src) if 'src' in locals() else str(next((ROOT/'samples').glob('*1.mp4'))),
               'photo': 'https://raw.githubusercontent.com/scikit-image/scikit-image/v0.20.0/skimage/data/astronaut.png'}
    from sv.server.routes.tasks import _page_is_color
    comic = Path(r'D:\work\copy_comic\out\租借女友')
    if comic.exists():
        for chapter in sorted(comic.iterdir()):
            if not chapter.is_dir():
                continue
            for p in sorted(chapter.glob('*.jpg'))[:5]:
                with Image.open(p) as im:
                    lane = 'illustration' if _page_is_color(im) else 'manga'
                    if not (fixtures / f'{lane}.png').exists():
                        im.convert('RGB').save(fixtures / f'{lane}.png')
                        sources[lane] = str(p)
            if all((fixtures / f'{lane}.png').exists() for lane in ('manga', 'illustration')):
                break
    # Explicitly labelled diagnostic fixtures are never presented as real-world evidence.
    chart = Image.new('RGB', (512, 512), 'white')
    d = ImageDraw.Draw(chart)
    font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 23)
    d.text((12, 12), 'RainFrame 文字细节 0123456789', fill='black', font=font)
    for i, color in enumerate(('red', 'green', 'blue', 'black', 'gray')):
        d.rectangle((i * 100, 60, i * 100 + 90, 140), fill=color)
    for y in range(170, 500, 5):
        d.line((10, y, 230, y), fill='black', width=1)
    for r in range(10, 160, 10):
        d.ellipse((360-r, 340-r, 360+r, 340+r), outline='black', width=1)
    chart.save(fixtures / 'diagnostic.png')
    for lane in ('anime', 'photo', 'manga', 'illustration', 'diagnostic'):
        p = fixtures / f'{lane}.png'
        if not p.exists():
            raise RuntimeError(f'Missing real fixture: {p}')
        with Image.open(p) as im:
            im = im.convert('RGB')
            # Crop actual pixels; never invent an HR reference by upscaling the source.
            side = min(512, *im.size)
            x, y = (im.width-side)//2, (im.height-side)//2
            im.crop((x, y, x+side, y+side)).save(fixtures / f'{lane}_reference.png')
    dump(out / 'sources.json', sources)


def jobs_for(out, mode):
    specs = load_registry()
    jobs = []
    for spec in specs.values():
        for f in spec.files:
            scale = f.get('scale', spec.scale[0])
            variant = f.get('variant')
            backend = 'torch' if spec.engine == 'torch' else ('cpu' if 'cugan' in spec.id else 'dml')
            jobs.append({'phase': 'smoke', 'model': spec.id, 'scale': scale,
                         'variant': variant, 'backend': backend})
        if mode == 'smoke':
            continue
        scale = min(spec.scale)
        variant = auto_variant(spec, scale, 1200)
        backend = 'torch' if spec.engine == 'torch' else ('cpu' if 'cugan' in spec.id else 'dml')
        jobs.append({'phase': 'benchmark', 'model': spec.id, 'scale': scale,
                     'variant': variant, 'backend': backend})
        if spec.engine != 'torch' and spec.kind != 'interp':
            jobs.append({'phase': 'benchmark', 'model': spec.id, 'scale': scale,
                         'variant': variant, 'backend': 'trt'})
        if spec.kind == 'interp':
            jobs.append({'phase':'benchmark', 'model':spec.id, 'scale':scale,
                         'variant':None, 'backend':'cuda'})
        # Every advertised SR scale is covered by smoke; non-default scales get
        # performance/quality coverage too, denoise/height variants remain smoke-only.
        for other in spec.scale:
            if other != scale and spec.kind != 'interp':
                jobs.append({'phase': 'benchmark', 'model': spec.id, 'scale': other,
                             'variant': auto_variant(spec, other, 1200), 'backend': backend})
        if spec.kind!='interp':
            jobs.append({'phase':'image_e2e','model':spec.id,'scale':scale,'variant':None,'backend':backend})
        if spec.id in ('animejanai-v31-hd-balanced-sharp','realesr-general-x4v3','realesrgan-x4plus-torch','rife-v4.26'):
            jobs.append({'phase':'video_e2e','model':spec.id,'scale':scale,'variant':None,'backend':backend})
    for j in jobs:
        j['id'] = '__'.join(str(j[k] or 'default') for k in ('phase', 'model', 'scale', 'variant', 'backend'))
        j['out'] = str(out)
    return jobs


class MemorySampler:
    """Whole-GPU memory includes the desktop/other apps; delta is observational."""
    def __init__(self):
        self.stop = threading.Event()
        self.gpu = []
        self.rss = []
        self.thread = threading.Thread(target=self.run, daemon=True)
    def run(self):
        import psutil
        proc = psutil.Process()
        while not self.stop.is_set():
            self.rss.append(proc.memory_info().rss / 1048576)
            try:
                result = subprocess.run(['nvidia-smi', '--query-gpu=memory.used', '--format=csv,noheader,nounits'],
                                        capture_output=True, timeout=3, creationflags=WINDOWS_CREATE_FLAGS)
                self.gpu.append(float(result.stdout.decode().splitlines()[0]))
            except Exception:
                pass
            self.stop.wait(.5)
    def start(self):
        self.thread.start()
    def finish(self):
        self.stop.set()
        self.thread.join(4)
        return {'rss_peak_mb': round(max(self.rss, default=0), 1),
                'gpu_total_baseline_mb': self.gpu[0] if self.gpu else None,
                'gpu_total_peak_mb': max(self.gpu) if self.gpu else None,
                'gpu_observed_delta_mb': max(self.gpu)-self.gpu[0] if self.gpu else None}


def e2e_worker(job,spec,directory):
    import asyncio
    from sv.server import settings
    snapshot=settings.load()
    settings.load=lambda:{**snapshot,'engine':{'dml':'auto','cpu':'cpu','trt':'trt','torch':'auto'}[job['backend']]}
    if job['phase']=='image_e2e':
        from sv.server import worker_image
        worker_image.TEMP_DIR=directory/'temporary'
        events=[]
        worker_image.emit=lambda event:events.append(event)
        images=[]
        for lane in ('manga','illustration','diagnostic'):
            source=directory/f'{lane}_input.png';target=directory/f'{lane}_output.png'
            Image.open(Path(job['out'])/'fixtures'/f'{lane}_reference.png').resize((128,128)).save(source)
            if target.exists():target.unlink()
            images.append({'in':str(source),'out':str(target),'expected_side':128*job['scale']})
        for lane in ('anime_alt','comic_color'):
            hr=Image.open(Path(job['out'])/'fixtures'/f'{lane}_reference.png').convert('RGB')
            side=hr.width//job['scale'];hr=hr.crop((0,0,side*job['scale'],side*job['scale']))
            lr=hr.resize((side,side),Image.Resampling.BICUBIC)
            for degradation in ('clean','jpeg45'):
                im=lr
                if degradation=='jpeg45':
                    buffer=io.BytesIO();lr.save(buffer,format='JPEG',quality=45,subsampling=2);buffer.seek(0)
                    im=Image.open(buffer).convert('RGB')
                source=directory/f'{lane}_{degradation}_input.png';target=directory/f'{lane}_{degradation}.png'
                im.save(source)
                if target.exists():target.unlink()
                images.append({'in':str(source),'out':str(target),'expected_side':side*job['scale']})
        task={'id':'release-'+spec.id,'model_id':spec.id,'input_path':images[0]['in'],
              'output_path':images[0]['out'],'created_at':time.time()}
        params={'kind':'image','scale':job['scale'],'tile':spec.tile_hint or 256,
                'format':'png','png_fast':True,'async_save':True,'images':images}
        start=time.perf_counter()
        code=worker_image._run_image_job(task,params,spec)
        dump(directory/'events.json',events)
        assert code==0 and any(e.get('type')=='done' for e in events),events[-3:]
        rgb_patches=[];color_warnings=[]
        for pair in images:
            with Image.open(pair['out']) as im:
                assert im.size==(pair['expected_side'],pair['expected_side']),im.size
                assert np.asarray(im).std()>4,'Flat saved output'
                if Path(pair['in']).name=='diagnostic_input.png':
                    rgb=np.asarray(im.convert('RGB')).astype(float);s=job['scale']
                    for channel,x in ((0,5),(1,30),(2,55)):
                        patch=rgb[20*s:30*s,x*s:(x+10)*s].mean(axis=(0,1))
                        rgb_patches.append(patch.tolist())
                        if spec.category=='manga_bw':
                            assert patch.max()-patch.min()<5,('Expected grayscale output',patch.tolist())
                        else:
                            assert patch[channel]>max(patch[(channel+1)%3],patch[(channel+2)%3])+5,('RGB channel mismatch',channel,patch.tolist())
                            if patch[channel]<(128 if channel==1 else 255)*.7:
                                color_warnings.append(f'Pure {("red","green","blue")[channel]} diagnostic patch attenuated: {patch[channel]:.1f}')
        assert not list(directory.glob('*.part*')),'Partial files remain'
        return {'status':'ok','saved_images':len(images),'e2e_wall_s':time.perf_counter()-start,
                'png_fast':True,'async_save':True,'rgb_diagnostic_ok':True,
                'color_mode':'grayscale' if spec.category=='manga_bw' else 'rgb',
                'rgb_patch_means':rgb_patches,'color_warnings':color_warnings}
    from sv.pipeline.probe import probe
    from sv.pipeline.stream import StreamPipeline,EncodeOpts
    clip=directory/'input.mp4'
    source=next((ROOT/'samples').glob('*1.mp4'))
    subprocess.run([ffmpeg_bin(),'-y','-loglevel','error','-i',str(source),'-vf','scale=256:144',
                    '-frames:v','6','-an','-c:v','libx264','-crf','18',str(clip)],check=True,
                   creationflags=WINDOWS_CREATE_FLAGS)
    info=probe(clip);interp=None
    if spec.kind=='interp':
        from sv.engines.rife import Rife2x
        interp=Rife2x(model_file(spec,job['scale'],'fp16'));interp.load()
        class Identity:
            scale=1
            supports_batch=False
            def process(self,frame):return frame
        eng=Identity()
    elif spec.engine=='torch':
        from sv.engines.torch_engine import TorchSrEngine
        eng=TorchSrEngine(model_file(spec,job['scale']),job['scale'],io=spec.io,tile=256);eng.load()
    else:
        from sv.server.worker_engine import _load_onnx_engine
        eng,_=_load_onnx_engine(model_file(spec,job['scale'],'fp16'),spec,job['scale'],None,'fp16',
                               spec.tile_hint,(144,256),log=lambda e:print(e.get('line',''),flush=True))
    output=directory/'output.mp4'
    if spec.engine=='torch':
        from sv.pipeline import chunked
        chunked.TEMP_DIR=directory/'temporary'
        pipe=chunked.ChunkedPipeline(info,output,eng,EncodeOpts(codec='h264'),task_id='release-'+spec.id)
    else:
        pipe=StreamPipeline(info,output,eng,EncodeOpts(codec='h264'),interp=interp)
    start=time.perf_counter();stats=asyncio.run(pipe.run());wall=time.perf_counter()-start
    result=probe(output)
    assert (result.width,result.height)==(info.width*eng.scale,info.height*eng.scale)
    expected=info.total_frames*(2 if interp else 1)
    assert result.total_frames==expected,(result.total_frames,expected)
    assert abs(result.fps-info.fps*(2 if interp else 1))<.01
    # Decode the complete result so a plausible metadata header is insufficient.
    subprocess.run([ffmpeg_bin(),'-v','error','-xerror','-i',str(output),'-f','null','-'],check=True,
                   stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,creationflags=WINDOWS_CREATE_FLAGS)
    for name,path in (('source',clip),('encoded',output)):
        subprocess.run([ffmpeg_bin(),'-y','-loglevel','error','-i',str(path),'-frames:v','1',
                        str(directory/f'{name}_first.png')],check=True,creationflags=WINDOWS_CREATE_FLAGS)
    source_pixels=np.asarray(Image.open(directory/'source_first.png').convert('RGB'))
    encoded_pixels=np.asarray(Image.open(directory/'encoded_first.png').convert('RGB'))
    expected_pixels=eng.process(source_pixels)
    color_mae=float(np.abs(encoded_pixels.astype(float)-expected_pixels.astype(float)).mean())
    assert color_mae<8,('Encoded color differs from engine RGB contract',color_mae)
    return {'status':'ok','frames':result.total_frames,'output_size':[result.width,result.height],
            'output_fps':result.fps,'e2e_wall_s':wall,'e2e_fps':stats.frames/wall,'pipeline':type(pipe).__name__,
            'encoded_first_frame_mae':color_mae}


def worker(job):
    spec = load_registry()[job['model']]
    scale, backend = job['scale'], job['backend']
    directory = Path(job['out']) / 'outputs' / job['id']
    directory.mkdir(parents=True, exist_ok=True)
    if job['phase'] in ('image_e2e','video_e2e'):
        return e2e_worker(job,spec,directory)
    weight = model_file(spec, scale, 'fp16' if backend != 'cpu' else 'fp32', job['variant'])
    if not weight.exists():
        raise FileNotFoundError(weight)
    if spec.fp16 and backend not in ('cpu', 'torch') and not weight.stem.endswith('_fp16'):
        from sv.models.fp16 import ensure_fp16_file
        weight = ensure_fp16_file(weight)
    frames = np.asarray(Image.open(Path(job['out'])/'fixtures/anime.png').convert('RGB').resize((256,144)))
    tile = spec.tile_hint
    if spec.category in ('manga_bw', 'illustration', 'photo_restore'):
        tile = tile or 256  # Product image/manga wizard default.
    # A real-sized pass, separately timed; no extrapolated FPS.
    if spec.kind == 'interp' or spec.category in ('anime_video','anime_restore','general_upscale'):
        full = np.asarray(Image.open(Path(job['out'])/'fixtures/anime.png').convert('RGB'))
        full = full[:720,:1280].copy()  # Actual 720p crop from the sample.
        real_name = '720p'
    else:
        lane = 'manga' if spec.category=='manga_bw' else ('illustration' if spec.category=='illustration' else 'photo')
        full_im = Image.open(Path(job['out'])/'fixtures'/f'{lane}.png').convert('RGB')
        full_im.thumbnail((1280,1280))
        full = np.asarray(full_im).copy()
        real_name = lane
    warm_hw = (47,63) if job['phase']=='smoke' else full.shape[:2]
    t = time.perf_counter()
    if spec.kind == 'interp':
        from sv.engines.rife import Rife2x
        eng = Rife2x(weight)
        eng.load()
        process = lambda a: eng.interpolate(a, a)
    elif spec.engine == 'torch':
        from sv.engines.torch_engine import TorchSrEngine
        eng = TorchSrEngine(weight, scale, io=spec.io, tile=tile or 256)
        eng.load()
        # Exercise product RGB contract directly, without hiding adapter bugs.
        process = eng.process
    else:
        from sv.server.worker_engine import _load_onnx_engine
        from sv.server import settings
        setting_snapshot=settings.load()
        settings.load=lambda: {**setting_snapshot, 'engine':{'dml':'auto','cpu':'cpu','trt':'trt'}[backend]}
        eng, used_precision = _load_onnx_engine(weight, spec, scale, job['variant'],
            'fp32' if backend=='cpu' else 'fp16', tile, warm_hw, batch=1,
            log=lambda event: print(event.get('line',''),flush=True))
        process = eng.process
    row = {'weight': weight.name, 'weight_sha256': hashlib.sha256(weight.read_bytes()).hexdigest(),
           'load_s': time.perf_counter()-t, 'provider_chain': eng.provider_used,
           'runtime_weight':str(getattr(eng,'model_path',weight)),
           'runtime_device':getattr(eng,'device',backend),
           'trt_fp16':getattr(eng,'trt_fp16',None),
           'tile':getattr(eng,'tile',tile), 'precision_file':locals().get('used_precision', 'fp16' if 'fp16' in weight.stem else 'fp32')}
    dump(directory/'partial.json',row)
    if job['phase']=='benchmark':
        start = time.perf_counter()
        large = process(full)
        row['real_size'] = list(full.shape[:2][::-1])
        row['real_case'] = real_name
        row['real_first_s'] = time.perf_counter()-start
        assert large.std() > 4, 'Real-size output is flat/black'
        Image.fromarray(large).save(directory/'real.png')
        real_times = []
        for _ in range(3):
            tick = time.perf_counter()
            process(full)
            real_times.append(time.perf_counter()-tick)
        row['real_repetitions'] = 3
        row['real_mean_ms'] = 1000*statistics.mean(real_times)
        row['real_fps'] = 3/sum(real_times)
        dump(directory/'partial.json',row)
    else:
        odd = np.asarray(Image.open(Path(job['out'])/'fixtures/diagnostic.png').resize((63,47)))
        result = process(odd)
        expected = (47,63,3) if spec.kind == 'interp' else (47*scale,63*scale,3)
        assert result.shape == expected, (result.shape, expected)
        assert result.dtype == np.uint8 and float(result.std()) > 5, 'Invalid/flat output'
        row['odd_shape_ok'] = True
        Image.fromarray(result).save(directory/'odd.png')
        if job['phase'] == 'smoke':
            row['status'] = 'ok'
            return row
    # First-call time and steady state are separated. Include transfers and tiling.
    start = time.perf_counter()
    process(frames)
    row['warmup_256x144_s'] = time.perf_counter()-start
    times = []
    for _ in range(5):
        start = time.perf_counter()
        process(frames)
        times.append(time.perf_counter()-start)
    row.update(input_size=[256,144], output_size=[256*scale,144*scale] if spec.kind!='interp' else [256,144],
               repetitions=5, mean_ms=1000*statistics.mean(times), median_ms=1000*statistics.median(times),
               p95_ms=1000*float(np.quantile(times,.95)), fps=5/sum(times))
    # Repeated inference exposes numerical drift and sustained allocation growth.
    before = time.perf_counter()
    reference = process(frames)
    max_difference = 0
    for _ in range(19):
        current = process(frames)
        max_difference = max(max_difference, int(np.abs(current.astype('int16')-reference).max()))
    row['repeat20_s'] = time.perf_counter()-before
    row['repeat20_max_difference'] = max_difference
    dump(directory/'partial.json',row)
    Image.fromarray(reference).save(directory/'speed_sample.png')
    if spec.kind == 'interp':
        a = np.asarray(Image.open(Path(job['out'])/'fixtures/motion01.png').convert('RGB'))
        b = np.asarray(Image.open(Path(job['out'])/'fixtures/motion03.png').convert('RGB'))
        Image.fromarray(eng.interpolate(a,b)).save(directory/'motion_mid.png')
        row['status'] = 'ok'
        return row
    for lane in ('anime','photo','manga','illustration','diagnostic'):
        hr = Image.open(Path(job['out'])/'fixtures'/f'{lane}_reference.png').convert('RGB')
        side = hr.width // scale
        hr = hr.crop((0,0,side*scale,side*scale))
        lr = hr.resize((side,side),Image.Resampling.BICUBIC)
        for degradation in ('clean','jpeg45'):
            if degradation == 'jpeg45':
                buffer = io.BytesIO()
                lr.save(buffer,format='JPEG',quality=45,subsampling=2)
                buffer.seek(0)
                inp = np.asarray(Image.open(buffer).convert('RGB'))
            else:
                inp = np.asarray(lr)
            processed = process(inp)
            assert processed.shape == (hr.height,hr.width,3), f'{lane} shape mismatch'
            assert processed.std() > 4, f'{lane} output is flat/black'
            Image.fromarray(processed).save(directory/f'{lane}_{degradation}.png')
    if spec.category in ('anime_video','anime_restore','general_upscale'):
        for number in range(1,8):
            motion=np.asarray(Image.open(Path(job['out'])/'fixtures'/f'motion{number:02}.png').convert('RGB'))
            Image.fromarray(process(motion)).save(directory/f'motion{number:02}.png')
    row['u8_wrapped'] = bool(getattr(eng,'u8_wrapped',False))
    row['status'] = 'ok'
    return row


def run(out, mode, only, retry_errors=False):
    out.mkdir(parents=True, exist_ok=True)
    prepare(out)
    jobs = jobs_for(out, mode)
    jobs.sort(key=lambda j:(0 if j['phase']=='smoke' else 2 if j['backend']=='trt' else 1, j['model'], j['scale']))
    dump(out/'matrix.json', jobs)
    if only:
        jobs = [j for j in jobs if only in j['id']]
    inventory = []
    for spec in load_registry().values():
        for f in spec.files:
            p = model_file(spec,f.get('scale',spec.scale[0]),variant=f.get('variant'))
            digest = hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
            inventory.append({'model':spec.id,'file':f['name'],'size':p.stat().st_size if p.exists() else 0,
                              'sha256':digest,'expected_sha256':f.get('sha256'),
                              'hash_ok':digest==f.get('sha256') if f.get('sha256') else None})
    dump(out/'inventory.json',inventory)
    gpu = subprocess.run(['nvidia-smi','--query-gpu=name,driver_version,memory.total','--format=csv'],capture_output=True)
    dump(out/'host.json',{'utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
                         'gpu':gpu.stdout.decode(),'python':sys.version,'commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT).decode().strip(),
                         'working_tree_dirty':True,'notes':'GPU total memory includes desktop/other apps. Single-host results.'})
    log = out/'results.jsonl'
    previous = {r['id']:r for r in (json.loads(l) for l in log.read_text(encoding='utf-8').splitlines())} if log.exists() else {}
    completed = {key for key,row in previous.items() if not retry_errors or row['status']=='ok'}
    for index,job in enumerate(jobs):
        if job['id'] in completed:
            continue
        print(f"[{index+1}/{len(jobs)}] {job['id']}",flush=True)
        cuda = job['backend'] in ('trt','torch') or (job['model']=='rife-v4.26' and job['backend']=='cuda')
        interpreter = ROOT/('.venv-cuda' if cuda else '.venv')/'Scripts/python.exe'
        job_file=out/'current-job.json';dump(job_file,job)
        child_output=out/'current-result.json'
        if child_output.exists():child_output.unlink()
        logs = out/'logs';logs.mkdir(exist_ok=True)
        log_path=logs/f"{job['id']}.txt"
        if log_path.exists():
            attempts=logs/'attempts';attempts.mkdir(exist_ok=True)
            shutil.copyfile(log_path,attempts/f"{job['id']}__{time.time_ns()}.txt")
        with log_path.open('w',encoding='utf-8') as stream:
            try:
                proc=subprocess.run([str(interpreter),str(Path(__file__).resolve()),'--worker',str(job_file)],
                                    stdout=stream,stderr=subprocess.STDOUT,cwd=ROOT,timeout=900 if cuda else 240,
                                    env={**os.environ,'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'},
                                    creationflags=WINDOWS_CREATE_FLAGS)
                result=json.loads(child_output.read_text(encoding='utf-8')) if child_output.exists() else {'status':'crash','exit_code':proc.returncode}
            except subprocess.TimeoutExpired:
                partial=out/'outputs'/job['id']/'partial.json'
                result={**(json.loads(partial.read_text(encoding='utf-8')) if partial.exists() else {}),
                        'status':'timeout','timeout_s':900 if cuda else 240}
        row={**job,**result}
        with log.open('a',encoding='utf-8') as stream:stream.write(json.dumps(row,ensure_ascii=False)+'\n')
        print(f"  {row['status']} {row.get('mean_ms','')} ms",flush=True)


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--out',default=str(ROOT/'reports/model-release-2026-10-06'))
    ap.add_argument('--mode',choices=['smoke','full'],default='full')
    ap.add_argument('--only',default='')
    ap.add_argument('--retry-errors',action='store_true',help='Retry failed jobs, retaining the original evidence')
    ap.add_argument('--worker',default='')
    args=ap.parse_args()
    if args.worker:
        job=json.loads(Path(args.worker).read_text(encoding='utf-8'))
        sampler=MemorySampler();sampler.start()
        try:
            result=worker(job)
        except Exception as exc:
            traceback.print_exc()
            partial=Path(job['out'])/'outputs'/job['id']/'partial.json'
            result={**(json.loads(partial.read_text(encoding='utf-8')) if partial.exists() else {}), 'status':'error','error':f'{type(exc).__name__}: {exc}',
                    'decoded_error_bytes':[a.decode('gbk','replace') for a in exc.args if isinstance(a,bytes)]}
        result.update(sampler.finish())
        dump(Path(job['out'])/'current-result.json',result)
    else:
        run(Path(args.out).resolve(),args.mode,args.only,args.retry_errors)


if __name__=='__main__':main()
