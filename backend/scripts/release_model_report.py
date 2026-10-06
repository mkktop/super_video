"""Build local, auditable CSV/Markdown/HTML artifacts from the release audit."""
import argparse
import csv
import html
import io
import json
import hashlib
import re
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'backend'))
import cv2
import numpy as np
from PIL import Image
from sv.models.registry import load_registry


LABELS = {'anime_video':'动漫视频','anime_restore':'动漫重建/降噪','manga_bw':'黑白漫画',
          'illustration':'彩漫/插画','photo_restore':'照片/通用修复',
          'general_upscale':'轻量通用放大','interp':'补帧'}


def readable_logs(out):
    manifest_path=out/'log_encodings.json'
    previous=json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else {}
    records={}
    for directory in (out/'logs',out/'packaged-logs'):
        for path in directory.rglob('*.txt'):
            key=path.relative_to(out).as_posix();raw=path.read_bytes()
            digest=hashlib.sha256(raw).hexdigest()
            if previous.get(key,{}).get('readable_sha256')==digest:
                records[key]=previous[key];continue
            preserved=directory/'raw'/f'{path.stem}_{digest[:16]}.bin'
            preserved.parent.mkdir(exist_ok=True)
            if not preserved.exists():preserved.write_bytes(raw)
            lines=[]
            for line in raw.split(b'\n'):
                line=line.lstrip(b'\x00')
                if line and line.count(b'\x00')/len(line)>.2:
                    text=line.decode('utf-16-le','replace')
                else:
                    try:text=line.decode('utf-8')
                    except UnicodeDecodeError:text=line.decode('gbk','replace')
                lines.append(re.sub(r'\x1b\[[0-9;]*m','',text.replace('\x00','')).rstrip('\r'))
            readable='\n'.join(lines).encode('utf-8')
            path.write_bytes(readable)
            records[key]={'raw_sha256':digest,'raw':preserved.relative_to(out).as_posix(),
                          'readable_sha256':hashlib.sha256(readable).hexdigest()}
    manifest_path.write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')


def metrics(reference, output, border=4):
    a, b = reference.astype(np.float64), output.astype(np.float64)
    if border:
        a,b=a[border:-border,border:-border],b[border:-border,border:-border]
    mse=float(((a-b)**2).mean())
    psnr=10*np.log10(255**2/max(mse,1e-12))
    # Standard local SSIM: 11x11 Gaussian window, sigma 1.5, RGB channel mean.
    mu_a=cv2.GaussianBlur(a,(11,11),1.5)
    mu_b=cv2.GaussianBlur(b,(11,11),1.5)
    va=cv2.GaussianBlur(a*a,(11,11),1.5)-mu_a*mu_a
    vb=cv2.GaussianBlur(b*b,(11,11),1.5)-mu_b*mu_b
    cov=cv2.GaussianBlur(a*b,(11,11),1.5)-mu_a*mu_b
    ssim=((2*mu_a*mu_b+6.5025)*(2*cov+58.5225))/((mu_a*mu_a+mu_b*mu_b+6.5025)*(va+vb+58.5225))
    return {'psnr_db':round(float(psnr),3),'ssim_rgb':round(float(ssim[5:-5,5:-5].mean()),5),
            'mae_rgb':round(float(np.abs(a-b).mean()),3)}


def report(out):
    readable_logs(out)
    specs=load_registry()
    rows=[json.loads(l) for l in (out/'results.jsonl').read_text(encoding='utf-8').splitlines()]
    # A later retry supersedes the same job, with original attempts retained in JSONL.
    latest={r['id']:r for r in rows}
    rows=list(latest.values())
    table=[];quality=[]
    for row in rows:
        spec=specs[row['model']]
        table.append({**{k:row.get(k) for k in ('id','phase','model','scale','variant','backend','status',
            'load_s','real_first_s','real_size','real_mean_ms','real_fps','mean_ms','fps',
            'repeat20_max_difference','gpu_total_peak_mb','gpu_observed_delta_mb','rss_peak_mb','error',
            'saved_images','e2e_wall_s','e2e_fps','frames','output_size','output_fps','pipeline')},
            'name':spec.name,'category':spec.category,'kind':spec.kind,'provider_chain':row.get('provider_chain'),
            'tile':row.get('tile'),'precision_file':row.get('precision_file'),
            'runtime_device':row.get('runtime_device'),'trt_fp16':row.get('trt_fp16')})
        if row['phase'] not in ('benchmark','image_e2e'):continue
        directory=out/'outputs'/row['id']
        lanes=('anime_alt','comic_color') if row['phase']=='image_e2e' else ('anime','photo','manga','illustration','diagnostic')
        for lane in lanes:
            for deg in ('clean','jpeg45'):
                path=directory/f'{lane}_{deg}.png'
                if not path.exists():continue
                hr=Image.open(out/'fixtures'/f'{lane}_reference.png').convert('RGB')
                side=hr.width//row['scale'];hr=hr.crop((0,0,side*row['scale'],side*row['scale']))
                ref=np.asarray(hr)
                reference_path=out/'fixtures'/f'{lane}_{row["scale"]}_reference.png'
                if not reference_path.exists():hr.save(reference_path)
                result=np.asarray(Image.open(path).convert('RGB'))
                if result.shape!=ref.shape:continue
                lr=hr.resize((side,side),Image.Resampling.BICUBIC)
                if deg=='jpeg45':
                    buf=io.BytesIO();lr.save(buf,format='JPEG',quality=45,subsampling=2);buf.seek(0)
                    lr=Image.open(buf).convert('RGB')
                baseline=np.asarray(lr.resize(hr.size,Image.Resampling.LANCZOS))
                baseline_path=out/'fixtures'/f'{lane}_{row["scale"]}_{deg}_lanczos.png'
                if not baseline_path.exists():Image.fromarray(baseline).save(baseline_path)
                base=metrics(ref,baseline)
                score=metrics(ref,result)
                quality.append({'id':row['id'],'model':row['model'],'name':spec.name,'category':spec.category,
                    'backend':row['backend'],'scale':row['scale'],'fixture':lane,'degradation':deg,
                    **score,'lanczos_psnr_db':base['psnr_db'],'psnr_gain_db':round(score['psnr_db']-base['psnr_db'],3),
                    'lanczos_ssim_rgb':base['ssim_rgb'],'sample':path.relative_to(out).as_posix(),
                    'reference_sample':reference_path.relative_to(out).as_posix(),
                    'baseline_sample':baseline_path.relative_to(out).as_posix()})
        if spec.kind=='interp' and (directory/'motion_mid.png').exists():
            ref=np.asarray(Image.open(out/'fixtures/motion02.png').convert('RGB'))
            result=np.asarray(Image.open(directory/'motion_mid.png').convert('RGB'))
            a=np.asarray(Image.open(out/'fixtures/motion01.png').convert('RGB')).astype(float)
            b=np.asarray(Image.open(out/'fixtures/motion03.png').convert('RGB')).astype(float)
            quality.append({'id':row['id'],'model':row['model'],'name':spec.name,'category':spec.category,
                'backend':row['backend'],'scale':1,'fixture':'motion_mid','degradation':'heldout',
                **metrics(ref,result),'average_baseline':metrics(ref,((a+b)/2).round().astype('uint8')),
                'sample':(directory/'motion_mid.png').relative_to(out).as_posix()})
            baseline_path=out/'fixtures/motion_average.png'
            Image.fromarray(((a+b)/2).round().astype('uint8')).save(baseline_path)
            quality[-1]['baseline_sample']=baseline_path.relative_to(out).as_posix()
            quality[-1]['reference_sample']='fixtures/motion02.png'
    for name,data in [('speed.csv',table),('quality.csv',quality)]:
        fields=list(dict.fromkeys(k for r in data for k in r))
        with (out/name).open('w',encoding='utf-8-sig',newline='') as f:
            w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(data)
    inventory=json.loads((out/'inventory.json').read_text(encoding='utf-8'))
    covered=set(r['model'] for r in rows)
    success=set(r['model'] for r in rows if r['phase']=='benchmark' and r['status']=='ok')
    errors=[r for r in rows if r['status']!='ok']
    matrix=json.loads((out/'matrix.json').read_text(encoding='utf-8'))
    pending=[r['id'] for r in matrix if r['id'] not in latest]
    text=['# 上架前模型实测报告','',f'模型覆盖 {len(covered)}/{len(specs)}；至少一条完整基准通过 {len(success)}/{len(specs)}。',
          f'执行记录 {len(rows)} 条；当前非通过记录 {len(errors)} 条；权重文件 {len(inventory)} 个。','',
          f'计划 {len(matrix)} 项；尚未完成 {len(pending)} 项；文件校验通过 {sum(r["hash_ok"] is True for r in inventory)}/{len(inventory)}。','',
          '## 测量范围与限制','',
          '- RTX 5080 16GB 单机实测；不能代替 AMD/Intel 或低显存机器验收。',
          '- 每个权重/变体单独子进程验证，产品同款引擎装配及回退；CUGAN 默认链受产品门控，CPU/TRT 单独标记。',
          '- MangaJaNai 速度/参考重建使用显式 1200p 档，未将其它高度档的速度推算为相同；所有高度档另做基础验证，图片流程检验按实际源高度自动选档。',
          '- 实际尺寸先预热，再计时 3 次；另测 256×144 的 5 次推理。均包含 CPU/GPU 传输与分块，不包含视频解码编码。',
          '- 加载/首调用与稳态分开。provider_chain 是注册的执行链，不能据此证明全部节点均由 TensorRT 执行。',
          '- load_s 是引擎装配与真实尺寸预热耗时，不含审计器此前的转换准备，不等于完整任务启动耗时。本轮边测边修复，早期失败转换的加载耗时保留；新失败缓存会缩短后续任务等待。',
          '- precision_file 表示权重文件精度，不等于每个算子的计算精度；TensorRT 混合精度另见 trt_fp16 字段，PyTorch CUDA 使用 autocast fp16。',
          '- GPU 为 nvidia-smi 每 0.5 秒采样的整卡总占用，含桌面和其他应用；增量不是模型独占显存。RSS 同为采样值。',
          '- 效果素材：两段现有动漫样片、用户黑白漫画、人工确认的彩漫封面、漫画目录内写实插画、scikit-image astronaut 照片、人工诊断图。每类参考裁剪有限。补充动漫/彩漫通过图片流程测试，仅覆盖主后端。',
          '- 可控退化：原图裁剪后按原生倍率 bicubic 缩小，另加 JPEG quality=45；不是所有真实模糊/噪声/压缩的替代。',
          '- PSNR/SSIM 是参考重建指标；GAN 生成的好看纹理可能低分。原生 2x 与 4x 输入信息量不同，必须分倍率比较。',
          '- RIFE 的 2x 是补帧倍率，输出图像尺寸不变；用前后两帧预测中间保留帧，并给出平均融合基线。实际尺寸速度使用同帧输入，不代表所有运动难度。',
          '- 20 次同图检查反映确定性/短时稳定性，不代表跨帧无闪烁，也不代表数小时耐久性。',
          '- 表格来自当前已完成结果，可重跑本脚本刷新；缺失不计为通过。','',
          '## 每个模型的实际尺寸速度','',
          '按注册表用途排列。数值为此素材/分块/实际精度下的稳态推理；CPU 行是显式兼容模式。','',
          '| 用途 | 模型 | 倍率 / 请求后端 | 注册执行链 | 实际输入 | ms/次 | fps | 权重精度 | 结果 |',
          '|---|---|---|---|---|---:|---:|---|---|']
    for r in sorted((r for r in rows if r['phase']=='benchmark'),key=lambda r:(specs[r['model']].category,r['model'],r['scale'],r['backend'])):
        def number(key):
            value=r.get(key)
            return f'{value:.2f}' if isinstance(value,(int,float)) else '—'
        chain=' → '.join(p.replace('ExecutionProvider','') for p in r.get('provider_chain',[])) or '—'
        scale_label='补帧 2x；尺寸 1x' if specs[r['model']].kind=='interp' else f'{r["scale"]}x'
        name=specs[r['model']].name+(f' · {r["variant"]}' if r.get('variant') else '')
        text.append(f"| {LABELS.get(specs[r['model']].category)} | {name} | {scale_label} / {r['backend']} | {chain} | {'×'.join(map(str,r.get('real_size',[]))) or '—'} | {number('real_mean_ms')} | {number('real_fps')} | {r.get('precision_file','—')} | {r['status']} |")
    e2e=[r for r in rows if r['phase'] in ('image_e2e','video_e2e')]
    text+=['','## 产物流程验收','',
          f'已完成 {len(e2e)} 项，通过 {sum(r["status"]=="ok" for r in e2e)} 项。图片验证 7 张连续批处理、PNG 快速保存、后台保存、尺寸及残留文件；其中 4 张来自补充动漫/彩漫的可控退化。视频验证解码→推理→编码、输出尺寸/帧数/帧率及完整解码。视频样片为 256×144、6 帧的短片，不用来推算长视频速度；图片耗时含引擎加载，视频耗时仅为引擎就绪后的管线。','',
          '| 模型 | 流程 | 结果 | 已保存图片/帧数 | 流程耗时秒 |',
          '|---|---|---|---:|---:|']
    for r in e2e:
        text.append(f"| {specs[r['model']].name} | {r['phase']} / {r['backend']} | {r['status']} | {r.get('saved_images',r.get('frames','—'))} | {r.get('e2e_wall_s',0):.2f} |")
    text+=['','## 画质指标观察','',
          '下表只列当前有限参考样本中，同用途、同原生倍率的 SSIM 最高项；按主后端取值。没有覆盖所有内容，不等于主观画质冠军。彩漫使用人工确认的封面，动漫使用第一段样片。','',
          '| 用途 / 原生倍率 | 干净素材 SSIM 最高项 | JPEG45 SSIM 最高项 |',
          '|---|---|---|']
    lanes={'anime_video':'anime','anime_restore':'anime','manga_bw':'manga',
           'illustration':'comic_color','photo_restore':'photo','general_upscale':'anime'}
    for category,lane in lanes.items():
        candidates=[q for q in quality if q['category']==category and q['fixture']==lane and q['backend'] not in ('trt','cuda')]
        for scale in sorted({q['scale'] for q in candidates}):
            best=[]
            for degradation in ('clean','jpeg45'):
                pool=[q for q in candidates if q['scale']==scale and q['degradation']==degradation]
                q=max(pool,key=lambda q:q['ssim_rgb']) if pool else None
                best.append(f"{q['name']} · {q['ssim_rgb']:.5f}" if q else '—')
            text.append(f"| {LABELS[category]} / {scale}x | {' | '.join(best)} |")
    text+=['','## 能力与诊断图观察','',
          '- MangaJaNai 是黑白模型，彩色诊断块转灰度符合其用途；彩页应选 IllustrationJaNai。',
          '- IllustrationJaNai V1 4x ESRGAN 对纯蓝诊断块明显压暗：输入 B=255，输出中央区域 B≈13。CPU fp32、DirectML fp32/fp16 均复现；不是保存管线或 fp16 特有错误。此为人工色块观察，不能推断所有真实彩图都会出现同等问题。对照见 precision-illustration4x/。',
          '- 严重 JPEG 压缩的彩漫 4x 样图中，DAT2 与 ESRGAN 均重画了部分眼睛和衣褶，不能宣称忠实恢复原始细节；对照见 comic-color-review.jpg。',
          '- RGB 诊断检查通道顺序；颜色变暗另列为能力观察，不能直接等同于通道交换。',
          '- 用户设定的三个默认模型均通过本轮测试；动漫 Balanced Sharp 与 Performance Sharp 分别偏风格/速度选择，DAT2 对彩漫较慢，适合优先画质的任务。','',
          '## 本轮确认并修复的问题','',
          '- Real-ESRGAN x4plus：ONNX 导出与 PyTorch 路径额外使用 ImageNet 均值方差，偏离官方 RGB 0–1 输入。已修正导出、运行时旧图迁移及视频 RGB 契约，保留原下载文件与 SHA256。实际权重 CPU 对照修复后平均差约 0.50/255、最大差 1/255。',
          '  官方输入处理对照：[Real-ESRGAN utils.py](https://github.com/xinntao/Real-ESRGAN/blob/master/realesrgan/utils.py)。',
          '- DirectML：Ani4K Compact / AnimeSharp 的 fp16 算子出现 0x8007023E。已增加使用 fp32 原件的 GPU 重试；最终是否通过以本报告最新记录为准，原失败证据保留。','',
          '- fp16 转换：部分模型无法通过转换后 CPU 建图检查；新增按原权重 SHA256、转换依赖版本及转换规则的失败缓存，避免重复等待。模型/依赖/规则变化时自动重试，资源与文件系统错误不固化。','',
          '## 安装版与上架范围','',
          '- 已另行构建并测试冻结 sidecar；这不是 Electron/NSIS 安装器或 GUI 全流程验收。可选 TensorRT 组件的安装分发未在本轮验证。',
          '- sidecar.spec 排除 torch；冻结程序复用 sidecar.exe，TensorRT 组件探测检查的是 ONNX Runtime。PyTorch 条目依赖受阻，当前声明供源码 CUDA 环境使用；安装包已验证会给出改选 ONNX 的明确错误。',
          '- 当前 CUGAN 的默认 DirectML/CUDA 路径由产品主动拒绝；本轮用 CPU 兼容模式及可选 TensorRT 测试，不能声称默认模式可用。','',
          ]
    packaged_path=out/'packaged-results.jsonl'
    if packaged_path.exists():
        packaged={r['model']:r for r in (json.loads(l) for l in packaged_path.read_text(encoding='utf-8').splitlines())}
        text+=[f'冻结 sidecar 覆盖 {len(packaged)}/{len(specs)}；功能通过 {sum(r["status"]=="ok" for r in packaged.values())} 项。基础运行库 selftest 结果见 packaged-selftest.json。','',
               '| 模型 | 支持后端 | 冻结程序结果 |', '|---|---|---|']
        for r in packaged.values():text.append(f"| {specs[r['model']].name} | {r['backend']} | {r['status']} |")
        fields=list(dict.fromkeys(k for r in packaged.values() for k in r))
        with (out/'packaged.csv').open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerows(packaged.values())
    text+=['','## 当前异常','']
    if not errors:text.append('- 源码矩阵最新结果均通过；历史失败及复测保留在 results.jsonl 与日志归档中。')
    for r in errors:text.append(f"- `{r['id']}`：{r['status']}；{r.get('error',r.get('exit_code',''))}")
    regression=out/'regression-tests.log'
    if regression.exists():
        summary=next((l for l in reversed(regression.read_text(encoding='utf-8').splitlines()) if 'passed' in l),'尚未取得汇总')
        text+=['','## 回归验证','',summary,'',
               '首轮用例隔离与竞态修正记录见 regression-tests-first-summary.md。']
    text+=['','## 原始证据','','- `inventory.json`：文件大小及 SHA256 对账。',
           '- `results.jsonl`：全部执行记录，失败和重试均保留。',
           '- `logs/`：每条子进程日志。','- `speed.csv` / `quality.csv`：可筛选的指标。',
           '- `logs/raw/` / `log_encodings.json`：混合编码日志的原始字节及校验值；txt 是便于阅读的副本。',
           '- `packaged-results.jsonl` / `packaged.csv` / `packaged-logs/`：冻结 sidecar 的逐模型结果。',
           '- `regression-tests.log`：完整后端回归日志。',
           '- `code_sha256.json`：当前后端源码、注册表与审计脚本的校验值。',
           '- `index.html`：离线对比页面；`outputs/` 保留原尺寸输出。',
           '- `torch-baseline/`：归一化及颜色契约问题修复前后的对照。']
    (out/'REPORT.md').write_text('\n'.join(text)+'\n',encoding='utf-8')
    payload=json.dumps({'rows':table,'quality':quality,'labels':LABELS},ensure_ascii=False).replace('<','\\u003c')
    page='''<!doctype html><html lang="zh"><meta charset="utf-8"><title>雨帧模型验收</title>
<style>body{font:15px system-ui;background:#111827;color:#e5e7eb;margin:24px}h1{font-size:25px}select{padding:8px;margin:6px;background:#1f2937;color:white}table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:9px;border-bottom:1px solid #374151;text-align:left}th{position:sticky;top:0;background:#1f2937}a{color:#93c5fd}.warn{color:#fbbf24} .view{display:flex;gap:20px;flex-wrap:wrap}.view img{width:512px;max-width:90vw;image-rendering:auto} .scroll{max-height:500px;overflow:auto}input{width:500px}figure{margin:0}p{max-width:1000px;line-height:1.7}</style>
<h1>雨帧 · 上架前模型实测</h1><p class="warn">RTX 5080 单机结果。速度为纯推理，不能当成成片速度。请按倍率、后端和素材分类比较；高 PSNR 不代表主观最好。完整范围和异常见 <a href="REPORT.md">验收报告</a>。</p>
<select id="cat"><option value="">全部用途</option></select><select id="backend"><option value="">全部后端</option><option>dml</option><option>trt</option><option>cpu</option><option>torch</option><option>cuda</option></select><select id="scale"><option value="">全部倍率</option><option value="1">尺寸 1x（补帧）</option><option value="2">2x</option><option value="3">3x</option><option value="4">4x</option></select>
<div class="scroll"><table><thead><tr><th>模型</th><th>用途</th><th>请求后端 / 倍率</th><th>注册执行链</th><th>结果</th><th>实际输入</th><th>稳态 ms/次</th><th>加载秒</th><th>采样整卡峰值 MB</th></tr></thead><tbody id="body"></tbody></table></div>
<h2>原图与输出效果</h2><p>左右同尺度显示参考图与模型输出，可选模型与素材。clean 是可控缩小，jpeg45 额外带 JPEG 压缩。两倍和四倍源输入分辨率不同。真实整页输出在对应 logs/outputs 目录。</p>
<select id="sample"></select><p id="score"></p><div class="view"><figure><figcaption>输入 Lanczos 放大 / 补帧平均融合基线</figcaption><img id="baseline"></figure><figure><figcaption>参考原图</figcaption><img id="reference"></figure><figure><figcaption>模型重建</figcaption><img id="processed"></figure></div>
<script>const data=PAYLOAD;const $=id=>document.getElementById(id);Object.entries(data.labels).forEach(([k,v])=>$('cat').add(new Option(v,k)));function cell(row,text){let td=document.createElement('td');td.textContent=text;row.append(td)}function refresh(){let rows=data.rows.filter(r=>r.phase==='benchmark'&&(!$('cat').value||r.category===$('cat').value)&&(!$('backend').value||r.backend===$('backend').value)&&(!$('scale').value||r.scale===Number($('scale').value)));$('body').replaceChildren();rows.forEach(r=>{let tr=document.createElement('tr');[r.name+(r.variant?' · '+r.variant:''),data.labels[r.category],`${r.backend} / ${r.kind==='interp'?'补帧 2x / 尺寸 1x':r.scale+'x'}`,r.provider_chain?.map(p=>p.replace('ExecutionProvider','')).join(' → ')||'—',r.status,r.real_size?.join('×')||'—',r.real_mean_ms?.toFixed(2)||'—',r.load_s?.toFixed(2)||'—',r.gpu_total_peak_mb??'—'].forEach(v=>cell(tr,v));tr.title=r.error||r.provider_chain?.join(' → ')||'';$('body').append(tr)});let ids=new Set(rows.map(r=>r.id));let old=$('sample').value;$('sample').replaceChildren();data.quality.filter(q=>(!$('cat').value||q.category===$('cat').value)&&(!$('backend').value||q.backend===$('backend').value)&&(!$('scale').value||q.scale===Number($('scale').value))).forEach((q,i)=>$('sample').add(new Option(`${q.name} | ${q.backend} ${q.scale}x | ${q.fixture} ${q.degradation}`,String(data.quality.indexOf(q)))));if([...$('sample').options].some(o=>o.value===old))$('sample').value=old;display()}function display(){let q=data.quality[Number($('sample').value)];if(!q||!$('sample').options.length){$('score').textContent='尚无匹配输出';$('reference').removeAttribute('src');$('processed').removeAttribute('src');$('baseline').removeAttribute('src');return}$('reference').src=q.reference_sample;$('processed').src=q.sample;$('baseline').src=q.baseline_sample;$('score').textContent=`PSNR ${q.psnr_db} dB · SSIM ${q.ssim_rgb} · RGB MAE ${q.mae_rgb} · Lanczos 增益 ${q.psnr_gain_db??'不适用'} dB`;}['cat','backend','scale'].forEach(id=>$(id).onchange=refresh);$('sample').onchange=display;refresh()</script></html>'''.replace('PAYLOAD',payload)
    (out/'index.html').write_text(page,encoding='utf-8')
    print(f'{len(covered)}/{len(specs)} models, {len(rows)} jobs, {len(quality)} quality comparisons, {len(errors)} non-ok')


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--out',default=str(ROOT/'reports/model-release-2026-10-06'))
    report(Path(ap.parse_args().out))
