"""FastAPI sidecar：HTTP + WS 对外接口，串行队列调度。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import re
import shutil
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .. import __version__
from ..models import manager
from ..models.registry import (
    BUNDLED_DIR,
    USER_REGISTRY_DIR,
    ModelNotFoundError,
    get_model,
    load_registry,
    model_dir,
)
from ..paths import ROOT, SR_LOG_DIR, TEMP_DIR, migrate_legacy_data
from ..pipeline.probe import (
    DECODERS,
    HWACCEL_CODECS,
    UnsupportedMedia,
    probe,
    probe_hwaccel,
    validate_m0,
)
from ..pipeline.trim import run_trim
from . import compare
from . import db
from . import task_stills
from . import trt_component
from . import user_presets
from .engine_select import select_engine
from .events import EventBus
from .hardware import hardware_info
from .perf import INTERVAL_S, PerfSampler
from .runner import Runner
from .settings import DEFAULTS, load as load_settings, save as save_settings

log = logging.getLogger("sv.app")

bus = EventBus()
runner = Runner(bus)
perf = PerfSampler(bus, runner)
_download_lock = asyncio.Lock()


def _expected_tokens() -> set[str]:
    """本地令牌（未配置=开发/测试模式不鉴权）。

    监听 127.0.0.1 并非浏览器隔离：任意网页可直接 POST 本端口（CORS 挡不住
    no-cors/表单 POST，WS 更不受 CORS 约束）。带上随机 token 后恶意页面无从
    获得令牌，本机 API 不再是 drive-by 攻击面。

    两个来源取并集：
    - SV_TOKEN 环境变量：Electron 拉起 sidecar 时注入（本次会话令牌）；
    - TEMP_DIR/sidecar.token 文件：UI 重启生成新 token 复用旧 sidecar 时，
      旧进程 env 里还是老 token——按文件实时校验才能接受新 token。
    """
    toks: set[str] = set()
    env_tok = os.environ.get("SV_TOKEN")
    if env_tok:
        toks.add(env_tok)
    try:
        f = TEMP_DIR / "sidecar.token"
        if f.is_file():
            t = f.read_text(encoding="utf-8").strip()
            if t:
                toks.add(t)
    except OSError:
        pass
    return toks


async def _token_auth(request: Request, call_next):
    toks = _expected_tokens()
    if toks and request.method != "OPTIONS" and request.url.path != "/api/health":
        got = request.headers.get("x-sv-token") or request.query_params.get("token")
        if got not in toks:
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)

# 硬件探测含 nvidia-smi/NVENC 试编码，按 60s 缓存
_hw_cache: tuple[float, dict] = (0.0, {})


def cached_hardware() -> dict:
    global _hw_cache
    ts, data = _hw_cache
    if not data or time.time() - ts > 60:
        data = hardware_info()
        _hw_cache = (time.time(), data)
    return data


_PRESETS_PATH = Path(__file__).parent / "presets.json"


def _presets() -> list[dict]:
    return json.loads(_PRESETS_PATH.read_text(encoding="utf-8"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 一次性迁移 ≤v0.1.20 遗留在安装目录里的数据（模型/组件/设置/缓存）。
    # 必须在 init_db 之前——任务库也在 .tmp 里
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, migrate_legacy_data)
    db.init_db()
    await loop.run_in_executor(None, sweep_orphan_workdirs)
    await loop.run_in_executor(None, task_stills.sweep_orphans)
    await loop.run_in_executor(None, gc_sr_logs)
    _start_trim_worker()
    compare.start(bus)
    runner.start()
    perf.start()
    yield
    await runner.stop()
    perf.stop()


app = FastAPI(title="super_video sidecar", version=__version__, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)
app.middleware("http")(_token_auth)


# ---- 模型 ----


class TaskCreate(BaseModel):
    input: str = ""  # 单输入；图片批量时可为空、改用 inputs 列表
    inputs: list[str] | None = None  # 图片批量：一次建一个任务循环处理
    output: str | None = None
    model_id: str
    params: dict = Field(default_factory=dict)  # scale/codec/crf/preset/tile/interp/denoise 每任务独立


class ModelImport(BaseModel):
    path: str
    id: str = Field(min_length=2, max_length=48)
    name: str = Field(min_length=1, max_length=48)
    scale: int = Field(ge=2, le=8)
    color: str = "rgb"            # rgb | bgr
    value_range: str = "0-1"      # 0-1 | 0-255
    tile: int = 0
    description: str = ""


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "version": app.version}


@app.get("/api/hardware")
def get_hardware() -> dict:
    return cached_hardware()


@app.get("/api/perf/history")
def perf_history() -> dict:
    """性能采样快照:本次启动内的全部历史(环形缓冲最近 1 小时)。"""
    return {"interval_s": INTERVAL_S, "samples": perf.snapshot()}


@app.get("/api/engine")
async def get_engine() -> dict:
    """推理后端展示：backend/detail 恒为按当前设置推算的「下一任务将使用」后端
    （与 runner._run_one 同口径，保存后立即反映——任务运行中也不例外：用户切后端
    的高频时机正是任务在跑时，旧口径按任务实况返回会让设置页标签长时间不动，
    任务结束又无人回拉，只能重启才刷新）。当前任务实际所用后端另附 running 字段
    如实透出（设置热切换下一任务生效，两者可能不同，前端并排展示）。
    探测首次可达分钟级，放线程池避免阻塞事件循环。"""
    engine_setting = load_settings().get("engine", "auto")
    want = None if engine_setting == "auto" else engine_setting
    loop = asyncio.get_running_loop()
    e = await loop.run_in_executor(None, select_engine, want)
    out = {"backend": e.backend, "python": e.python_exe, "detail": e.detail}
    if runner.current_id is not None and runner.engine is not None:
        r = runner.engine
        out["running"] = {"backend": r.backend, "detail": r.detail}
    return out


@app.get("/api/trt-component")
def get_trt_component() -> dict:
    """TRT 可选组件状态（安装/版本/体积/显卡架构/资产清单/安装进度）。"""
    return trt_component.status()


@app.post("/api/trt-component/install")
def install_trt_component() -> dict:
    accepted, msg = trt_component.start_install(bus)
    if not accepted:
        raise HTTPException(409, msg)
    return {"ok": True}


@app.delete("/api/trt-component")
def uninstall_trt_component() -> dict:
    ok, msg = trt_component.uninstall()
    if not ok:
        raise HTTPException(409, msg)
    return {"ok": True}


@app.get("/api/presets")
def get_presets() -> list[dict]:
    """内置预设（只读）+ 用户自定义预设（user=True 标记，前端可删）。"""
    return _presets() + user_presets.load()


class PresetCreate(BaseModel):
    """保存用户预设：新建任务页把当前参数快照成一条可复用配置。"""
    name: str = Field(min_length=1, max_length=24)
    icon: str = Field(default="⭐", max_length=8)
    desc: str = Field(default="", max_length=80)
    model_id: str
    target_scale: int
    codec: str = "h264"
    crf: int = 18
    container: str = "mp4"
    audio_mode: str = "auto"
    subtitle_mode: str = "auto"
    interp: str = "off"
    denoise: int | None = None
    deinterlace: bool = False
    deband: bool = False


@app.post("/api/presets", status_code=201)
def create_preset(body: PresetCreate) -> dict:
    specs = load_registry()
    if body.model_id not in specs:
        raise HTTPException(404, f"未知模型 {body.model_id}")
    if body.target_scale not in specs[body.model_id].scale:
        raise HTTPException(400, (
            f"模型 {body.model_id} 不支持 x{body.target_scale}"
            f"（可选 {specs[body.model_id].scale}）"))
    if body.codec not in _CODECS:
        raise HTTPException(400, f"codec 仅支持 {' / '.join(_CODECS)}")
    if not (0 <= body.crf <= 51):
        raise HTTPException(400, "crf 范围 0-51")
    if body.container not in _CONTAINERS:
        raise HTTPException(400, "container 仅支持 mp4 / mkv / mov")
    if body.audio_mode not in _AUDIO_MODES:
        raise HTTPException(400, f"audio_mode 仅支持 {' / '.join(_AUDIO_MODES)}")
    if body.subtitle_mode not in ("none", "auto"):
        raise HTTPException(400, "subtitle_mode 仅支持 none / auto")
    if body.interp not in ("off", "rife2x"):
        raise HTTPException(400, "interp 仅支持 off / rife2x")
    if body.denoise is not None and body.denoise not in (0, 1, 2, 3):
        raise HTTPException(400, "denoise 仅支持 0 / 1 / 2 / 3")
    return user_presets.add(body.model_dump())


@app.delete("/api/presets/{preset_id}")
def delete_preset(preset_id: str) -> dict:
    if any(p["id"] == preset_id for p in _presets()):
        raise HTTPException(409, "内置预设不可删除")
    if not user_presets.remove(preset_id):
        raise HTTPException(404, f"预设不存在: {preset_id}")
    return {"ok": True}


class ProbeBody(BaseModel):
    path: str
    hwdecode: bool = False  # 附带硬件解码可用性（真解码 3 帧实测，+~1s；任务页门控解码器选项用）
    recommend: bool = False  # 附带智能推荐（采样 4 帧内容分析，+~1s；任务页推荐卡用）


@app.post("/api/probe")
def probe_media(body: ProbeBody) -> dict:
    """向导第一步：媒体信息 + M0 可行性（供 UI 展示与预估）。"""
    p = Path(body.path)
    if not p.exists():
        raise HTTPException(400, f"文件不存在: {body.path}")
    try:
        info = probe(p)
        m0_error = None
        try:
            validate_m0(info)
        except UnsupportedMedia as e:
            m0_error = str(e)
    except UnsupportedMedia as e:
        raise HTTPException(422, str(e))
    resp = {
        "ok": m0_error is None,
        "error": m0_error,
        "width": info.width, "height": info.height,
        "fps": round(info.fps, 3),
        "duration_s": round(info.duration_s, 2),
        "total_frames": info.total_frames,
        "codec": info.video_codec, "pix_fmt": info.pix_fmt,
        "has_audio": info.has_audio,
        "audio_tracks": [a.codec for a in info.audio],
        "subtitles": info.subtitles,
    }
    if body.hwdecode and m0_error is None:
        # 按本文件实测的硬解可用性（编码矩阵 + 真解码验证），UI 据此禁用不支持的选项
        resp["decoder"] = {
            name: probe_hwaccel(p, hw, info.video_codec)
            for name, hw in (("nvdec", "cuda"), ("d3d11va", "d3d11va"))
        }
    if body.recommend and m0_error is None:
        # 智能推荐：采样帧内容分析 + 规则引擎。分析失败只是少了推荐卡，
        # 绝不影响 probe 主流程
        try:
            from ..pipeline.analyze import sample_frame_stats
            from ..pipeline.recommend import build_recommendation

            stats = sample_frame_stats(info)
            if stats is not None:
                resp["recommend"] = build_recommendation(info, stats)
        except Exception as e:  # noqa: BLE001
            log.warning(f"源分析失败（跳过推荐）: {e}")
    return resp


@app.get("/api/settings")
def get_settings() -> dict:
    return load_settings()


@app.get("/api/log-tail")
def log_tail(n: int = 120) -> dict:
    p = TEMP_DIR / "sidecar.log"
    if not p.exists():
        return {"lines": []}
    # 从文件尾按块回读，不整读（日志大时全量 read_text 是内存尖峰）
    try:
        with p.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            data = b""
            while size > 0 and data.count(b"\n") <= n:
                step = min(4096, size)
                size -= step
                f.seek(size)
                data = f.read(step) + data
        return {"lines": data.decode("utf-8", "replace").splitlines()[-n:]}
    except OSError:
        return {"lines": []}


@app.put("/api/settings")
def put_settings(body: dict) -> dict:
    try:
        data = save_settings(body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # 完成动作设置变更：布防中的倒计时与新设置不符时作废（改为 none 也撤）
    if "queue_done_action" in body:
        runner.refresh_queue_action(str(data.get("queue_done_action", "none")))
    return data


@app.post("/api/queue-done/cancel")
def cancel_queue_done() -> dict:
    """手动取消"队列完成后关机/休眠"倒计时（无倒计时=幂等成功）。"""
    return {"ok": runner.cancel_queue_action()}


@app.get("/api/models")
def get_models() -> list[dict]:
    hw = cached_hardware()
    gpu_vram = next(
        (g["vram_gb"] for g in hw.get("gpus", []) if g.get("vram_gb")), None
    )
    out = []
    for spec in load_registry().values():
        bundled = bool(spec.files) and all(
            (BUNDLED_DIR / f["name"]).exists() for f in spec.files
        )
        size_mb = round(sum(f.get("size", 0) for f in spec.files) / 1e6, 1)
        vram_ok = gpu_vram is None or spec.vram_gb <= gpu_vram
        # denoise 档位：registry files 里 variant=denoiseN 的集合（real-cugan 专属概念）
        denoise_levels = sorted({
            int(f["variant"][7:]) for f in spec.files
            if str(f.get("variant", "")).startswith("denoise")
            and str(f["variant"])[7:].isdigit()
        })
        out.append({
            "id": spec.id, "name": spec.name, "scale": spec.scale,
            "kind": spec.kind,
            "content": spec.content, "speed": spec.speed,
            "scenes": spec.scenes,  # 适用场景标签：video/manga/image（前端卡片角标+筛选）
            "vram_gb": spec.vram_gb, "description": spec.description,
            "tile_hint": spec.tile_hint,
            "installed": manager.is_downloaded(spec),
            "bundled": bundled,
            "size_mb": size_mb,
            "vram_ok": vram_ok,
            "vram_note": None if vram_ok else (
                f"需要约 {spec.vram_gb}GB 显存，本机 {gpu_vram}GB"
            ),
            "denoise_levels": denoise_levels,
            "engine": spec.engine,  # onnx | torch（torch 需独立 CUDA 环境，暂不参与模型对比）
        })
    return out


@app.delete("/api/models/{model_id}")
def delete_model(model_id: str) -> dict:
    specs = load_registry()
    if model_id not in specs:
        raise HTTPException(404, f"未知模型 {model_id}")
    d = model_dir(model_id)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    # 自定义导入的模型：连 manifest 一起删（内置模型保留注册表条目）
    custom = USER_REGISTRY_DIR / f"{model_id}.json"
    if custom.exists():
        custom.unlink()
    return {"ok": True}


@app.post("/api/models/import", status_code=201)
def import_model(body: ModelImport) -> dict:
    """自定义 ONNX 导入：拷贝入库 -> 真跑一帧验证 -> 写 manifest。"""
    import re as _re
    import shutil as _shutil

    import numpy as np

    from ..engines.onnx_engine import OnnxSrEngine

    src = Path(body.path)
    if not src.exists() or src.suffix.lower() != ".onnx":
        raise HTTPException(400, f"ONNX 文件不存在: {body.path}")
    if not _re.fullmatch(r"[a-z0-9][a-z0-9-]+", body.id):
        raise HTTPException(400, "id 仅允许小写字母/数字/连字符")
    if body.color not in ("rgb", "bgr") or body.value_range not in ("0-1", "0-255"):
        raise HTTPException(400, "color 仅 rgb/bgr；value_range 仅 0-1/0-255")
    if body.id in load_registry():
        raise HTTPException(409, f"模型 id 已存在: {body.id}")

    d = model_dir(body.id)
    d.mkdir(parents=True, exist_ok=True)
    dest = d / src.name
    _shutil.copy2(src, dest)

    io = {"color": body.color, "range": body.value_range}
    try:
        eng = OnnxSrEngine(dest, body.scale, io=io, tile=body.tile)
        eng.load()
        out = eng.process(np.random.default_rng(0).integers(0, 256, (64, 64, 3), dtype=np.uint8))
        h, w = out.shape[:2]
        if (w, h) != (64 * body.scale, 64 * body.scale):
            raise ValueError(f"输出 {w}x{h} 与 x{body.scale} 预期不符，请核对倍率")
        if float(out.std()) < 1.0:
            raise ValueError("输出接近纯色，IO 约定（颜色序/值域）可能不对")
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 — 校验失败回滚拷贝
        _shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(422, f"模型验证失败: {e}") from e

    manifest = {
        "id": body.id, "name": body.name, "vendor": "自定义", "license": "用户自带",
        "engine": "onnx", "kind": "sr", "scale": [body.scale],
        "content": [], "temporal": False, "speed": "balanced", "vram_gb": 4,
        "tile_hint": int(body.tile), "description": body.description or "用户导入的 ONNX 模型",
        "io": io, "files": [{"name": src.name}],
    }
    USER_REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    (USER_REGISTRY_DIR / f"{body.id}.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"ok": True, "id": body.id}


@app.post("/api/models/{model_id}/download")
async def download_model(model_id: str) -> dict:
    specs = load_registry()
    if model_id not in specs:
        raise HTTPException(404, f"未知模型 {model_id}")
    spec = specs[model_id]
    if manager.is_downloaded(spec):
        return {"ok": True, "already": True}
    if _download_lock.locked():
        raise HTTPException(409, "已有下载在进行中")

    async def _do():
        async with _download_lock:
            # 排队窗口内前一个下载可能已完成：复查后直接补发完成事件，不重跑
            if manager.is_downloaded(spec):
                bus.publish({"type": "model_download", "model_id": model_id, "done": True})
                return
            loop = asyncio.get_running_loop()
            total = sum(f.get("size", 0) for f in spec.files) or 1

            def cb(done, tot, label):
                # executor 线程回调：asyncio.Queue 非线程安全，必须回事件循环线程投递
                bus.publish_threadsafe({
                    "type": "model_download", "model_id": model_id,
                    "progress": round(done / total, 4),
                })

            try:
                await loop.run_in_executor(None, manager.download, spec, cb)
                # 下载完成后生成 fp16 变体（bundled 模型随包已有，此处只补下载件）
                try:
                    from ..models.fp16 import ensure_fp16

                    await loop.run_in_executor(None, ensure_fp16, spec)
                except ImportError:
                    log.warning("fp16 转换依赖缺失（onnx/onnxconverter-common），保持 fp32")
                except Exception as e:  # noqa: BLE001 — 转换失败不影响使用 fp32
                    log.warning(f"fp16 转换失败，保持 fp32: {e}")
                bus.publish({"type": "model_download", "model_id": model_id, "done": True})
            except Exception as e:  # noqa: BLE001
                bus.publish({"type": "model_download", "model_id": model_id,
                             "failed": str(e)})

    asyncio.create_task(_do())
    return {"ok": True, "started": True}


# ---- 任务 ----


# codec → 硬件能力位（None = 软编总是可用）
_CODECS = {
    "h264": None, "h265": None,
    "h264_nvenc": "nvenc", "hevc_nvenc": "nvenc", "av1_nvenc": "av1_nvenc",
    "h264_amf": "amf", "hevc_amf": "amf",
    "av1_svt": "svt_av1",
}
_AUDIO_MODES = ("auto", "copy", "aac", "flac", "none")
_CONTAINERS = ("mp4", "mkv", "mov")


_PRESETS_XCODE = (
    "ultrafast", "superfast", "veryfast", "faster", "fast",
    "medium", "slow", "slower", "veryslow",
)

# 图片超分接受的输入扩展名（识别失败/损坏文件由 PIL 打开时报 400）
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


def _sr_output_name(out_root: Path, stem: str, fmt: str, res_label: str,
                    used: set[str]) -> str:
    """超分输出命名（视频/图片同规则）：

    目标目录没有同名文件时沿用原文件名（干净）；同名已存在——可能是源文件
    本身（同目录同扩展名）、用户已有文件或上次产物——退回「_倍率」后缀，
    不覆盖任何现有文件。后缀名也已被本批同伴占用时再加序号。
    """
    plain = out_root / f"{stem}.{fmt}"
    pk = os.path.normcase(str(plain))
    if not plain.exists() and pk not in used:
        used.add(pk)
        return str(plain)
    suffixed = out_root / f"{stem}_{res_label}.{fmt}"
    sk = os.path.normcase(str(suffixed))
    if sk not in used:  # 磁盘上已存在的后缀名=上次产物，可覆盖（重跑不增殖）
        used.add(sk)
        return str(suffixed)
    n = 2
    while True:  # 仅批量同伴撞名时升序号
        cand = out_root / f"{stem}_{res_label}_{n}.{fmt}"
        ck = os.path.normcase(str(cand))
        if ck not in used:
            used.add(ck)
            return str(cand)
        n += 1


_TEMPLATE_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _render_output_stem(template: str, stem: str, model_id: str, res_label: str,
                        out_w: int, out_h: int) -> str:
    """输出命名模板渲染：空模板返回原 stem（沿用旧行为）。

    变量：{name} 原文件主名 / {model} 模型 id / {scale} 倍率或自定义分辨率档
    （与冲突后缀同源，如 2x、1920x1080）/ {res} 输出分辨率 / {date} 当天日期。
    未知变量原样保留（用户能一眼看出拼写错误）；模板注入的非法文件名字符
    兜底替换为下划线（设置端已挡，此处防手改配置文件）。
    """
    if not template:
        return stem
    s = (template
         .replace("{name}", stem)
         .replace("{model}", model_id)
         .replace("{scale}", res_label)
         .replace("{res}", f"{out_w}x{out_h}")
         .replace("{date}", time.strftime("%Y%m%d")))
    s = _TEMPLATE_ILLEGAL.sub("_", s).strip(" .")[:120]
    return s or stem


def _create_image_task(body: TaskCreate, spec) -> dict:
    """图片超分任务的创建校验与入库（单张或批量合一，批量=一个任务循环跑）。

    与视频任务共用模型/倍率纪律；批量时只允许倍数放大（自定义分辨率需逐图
    尺寸校验，暂不开放）。params.images 携带 [{in,out},...] 清单，worker 端
    一次模型加载循环处理。输出默认 PNG（无损），可选 JPG + 质量。批量可勾选
    merge_pdf：逐图产物照常落盘，另把全部成功页无损封装成一份 PDF（任务
    output 指向 PDF），pdf_out 与逐图产物同命名系、不覆盖现有文件。
    """
    params_in = dict(body.params)
    inputs_raw = list(body.inputs) if body.inputs else [body.input]
    if not inputs_raw or any(not i for i in inputs_raw):
        raise HTTPException(400, "未提供输入图片")
    paths: list[Path] = []
    for i in inputs_raw:
        p = Path(i)
        if not p.exists():
            raise HTTPException(400, f"输入文件不存在: {i}")
        if p.suffix.lower() not in _IMAGE_EXTS:
            raise HTTPException(400, f"{p.name} 不是受支持的图片格式")
        paths.append(p)
    # 去重（同一路径重复选择）
    seen: set[str] = set()
    paths = [p for p in paths
             if not (key := os.path.normcase(str(p))) in seen and not seen.add(key)]
    batch = len(paths) > 1

    scale = int(params_in.get("scale") or max(spec.scale))
    if scale not in spec.scale:
        raise HTTPException(400, f"模型 {spec.id} 不支持 x{scale}，可选 {spec.scale}")
    target = int(params_in.get("target_scale") or scale)
    if not (1 <= target <= scale):
        raise HTTPException(400, f"目标倍率需在 x1 ~ x{scale} 之间")
    # 自定义目标分辨率：仅单张开放（批量需逐图校验边界，暂不支持）
    tw, th = params_in.get("target_w"), params_in.get("target_h")
    if tw is not None or th is not None:
        if batch:
            raise HTTPException(400, "批量图片暂不支持自定义分辨率，请按倍数放大")
        if not (isinstance(tw, int) and isinstance(th, int) and tw > 0 and th > 0):
            raise HTTPException(400, "自定义分辨率需同时提供正整数 target_w/target_h")
        tw, th = tw - tw % 2, th - th % 2
    tile = int(params_in.get("tile", 0))
    if tile != 0 and not (64 <= tile <= 4096 and tile % 2 == 0):
        raise HTTPException(400, "tile 需为 0（自动）或 64~4096 的偶数像素")
    fmt = str(params_in.get("format") or "png").lower()
    if fmt not in ("png", "jpg"):
        raise HTTPException(400, "图片输出格式仅支持 png / jpg")
    denoise = params_in.get("denoise")
    if denoise is not None and int(denoise) not in (0, 1, 2, 3):
        raise HTTPException(400, "denoise 仅支持 0 / 1 / 2 / 3")
    merge_pdf = bool(params_in.get("merge_pdf"))  # 批量合并输出 PDF（无损封装）

    # 逐图可用性校验 + 尺寸读取（EXIF 方向转正后的真实宽高）
    from PIL import Image, ImageOps

    sizes: list[tuple[int, int]] = []
    for p in paths:
        try:
            with Image.open(str(p)) as im:
                im = ImageOps.exif_transpose(im)
                sizes.append(im.size)
        except OSError as e:
            raise HTTPException(400, f"无法读取图片 {p.name}: {e}") from e
    src_w, src_h = sizes[0]
    if tw is not None or th is not None:
        eff_w, eff_h = tw, th
        if eff_w < src_w or eff_h < src_h:
            raise HTTPException(
                400, f"目标分辨率不能低于源 {src_w}x{src_h}（当前 {eff_w}x{eff_h}）")
        if eff_w > src_w * scale or eff_h > src_h * scale:
            raise HTTPException(400, (
                f"目标 {eff_w}x{eff_h} 超出模型 x{scale} 原生上限 "
                f"{src_w * scale}x{src_h * scale}，请提高放大倍数或换更高倍率模型"))

    res_label = f"{tw}x{th}" if (tw is not None and th is not None) else f"{target}x"
    st = load_settings()
    odir = str(st.get("output_dir") or "").strip()
    tmpl = str(st.get("output_name_template") or "")
    out_root = Path(odir) if odir else paths[0].parent
    if batch and body.output:
        raise HTTPException(400, "批量图片由系统逐图命名输出，不能指定单一输出文件")
    # 逐图输出命名：目录无同名则沿用原名，冲突退 _倍率 后缀（不覆盖现有文件）
    images_meta: list[dict] = []
    used: set[str] = set()
    ow, oh = (tw, th) if tw is not None else (src_w * target, src_h * target)
    for p in paths:
        if batch or not body.output:
            stem = _render_output_stem(tmpl, p.stem, body.model_id, res_label, ow, oh)
            out = _sr_output_name(out_root, stem, fmt, res_label, used)
        else:
            out = str(body.output)
            used.add(os.path.normcase(out))
        images_meta.append({"in": str(p), "out": out})
    pdf_out = None
    if merge_pdf:
        # PDF 沿用首图名系（首图撞名拿了 _倍率 后缀时 PDF 跟随），与逐图
        # 产物互不覆盖；名字在创建期定死，worker 只消费
        pdf_out = _sr_output_name(
            out_root, Path(images_meta[0]["out"]).stem, "pdf", res_label, used)
    try:  # 与视频同规则：目录不存在自动建，指向不可写处给可读错误
        out_root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise HTTPException(400, f"无法创建输出目录 {out_root}: {e}") from e

    params = {
        "kind": "image", "format": fmt,
        "scale": scale, "target_scale": target,
        "tile": tile, "images": images_meta,
    }
    if fmt == "jpg":
        q = int(params_in.get("jpg_quality", 92))
        params["jpg_quality"] = min(100, max(60, q))
    if tw is not None and th is not None:
        params["target_w"], params["target_h"] = tw, th
        params["target_scale"] = scale  # 整数倍率字段退位为模型原生档
    if denoise is not None:
        params["denoise"] = int(denoise)
    if pdf_out is not None:
        params["merge_pdf"] = True
        params["pdf_out"] = pdf_out
    task = db.new_task(
        str(paths[0]), pdf_out or images_meta[0]["out"], body.model_id, params,
        src={"w": src_w, "h": src_h, "fps": 0.0, "total_frames": len(paths)},
    )
    bus.publish({"type": "task_status", "task_id": task["id"], "status": "queued"})
    return task


@app.post("/api/tasks", status_code=201)
def create_task(body: TaskCreate) -> dict:
    # 图片批量：inputs 列表 → 单任务循环处理（一次模型加载跑完全部）
    if body.inputs:
        try:
            specs = load_registry()
            if body.model_id not in specs:
                raise HTTPException(404, f"未知模型 {body.model_id}")
            return _create_image_task(body, specs[body.model_id])
        except UnsupportedMedia as e:
            raise HTTPException(422, str(e))
    input_path = Path(body.input)
    if not input_path.exists():
        raise HTTPException(400, f"输入文件不存在: {body.input}")
    try:
        specs = load_registry()
        if body.model_id not in specs:
            raise HTTPException(404, f"未知模型 {body.model_id}")
        spec = specs[body.model_id]
        if input_path.suffix.lower() in _IMAGE_EXTS:
            return _create_image_task(body, spec)
        info = probe(input_path)
        validate_m0(info)
    except UnsupportedMedia as e:
        raise HTTPException(422, str(e))

    params = dict(body.params)
    scale = int(params.get("scale") or max(spec.scale))
    if scale not in spec.scale:
        raise HTTPException(400, f"模型 {spec.id} 不支持 x{scale}，可选 {spec.scale}")
    params["scale"] = scale
    target = int(params.get("target_scale") or scale)
    if not (1 <= target <= scale):
        raise HTTPException(400, f"目标倍率需在 x1 ~ x{scale} 之间")
    params["target_scale"] = target
    # 自定义分辨率：精确目标宽高，优先于整数倍率；纪律=原生超分后只缩不放
    tw, th = params.get("target_w"), params.get("target_h")
    if tw is not None or th is not None:
        if not (isinstance(tw, int) and isinstance(th, int) and tw > 0 and th > 0):
            raise HTTPException(400, "自定义分辨率需同时提供正整数 target_w/target_h")
        tw, th = tw - tw % 2, th - th % 2  # yuv420 编码要求偶数，向下取整
        if tw < info.width or th < info.height:
            raise HTTPException(
                400, f"目标分辨率不能低于源 {info.width}x{info.height}（当前 {tw}x{th}）")
        if tw > info.width * scale or th > info.height * scale:
            raise HTTPException(400, (
                f"目标 {tw}x{th} 超出模型 x{scale} 原生上限 "
                f"{info.width * scale}x{info.height * scale}，请提高放大倍数或换更高倍率模型"
            ))
        params["target_w"], params["target_h"] = tw, th
        params["target_scale"] = scale  # 整数倍率字段退位为模型原生档
    tile = int(params.get("tile", 0))
    if tile != 0 and not (64 <= tile <= 4096 and tile % 2 == 0):
        raise HTTPException(400, "tile 需为 0（自动）或 64~4096 的偶数像素")
    params["tile"] = tile
    out_kind = params.get("out_kind", "video")
    if out_kind not in ("video", "png", "jpg"):
        raise HTTPException(400, "out_kind 仅支持 video / png / jpg")
    params["out_kind"] = out_kind
    codec = params.get("codec", "h264")
    if codec not in _CODECS:
        raise HTTPException(400, f"codec 仅支持 {' / '.join(_CODECS)}")
    hw_flag = _CODECS[codec]
    if hw_flag and not cached_hardware().get(hw_flag):
        raise HTTPException(400, f"本机 {hw_flag} 不可用，请选择软件编码")
    params["codec"] = codec
    # 解码器：软解默认；硬解按源编码矩阵校验（驱动/逐文件实测在 worker 端
    # probe_hwaccel 预验证，失败回退软解，不影响任务成败）
    decoder = params.get("decoder", "sw")
    if decoder not in DECODERS:
        raise HTTPException(400, f"decoder 仅支持 {' / '.join(DECODERS)}")
    hw_dec = DECODERS[decoder]
    if hw_dec and info.video_codec not in HWACCEL_CODECS[hw_dec]:
        raise HTTPException(400, (
            f"视频编码 {info.video_codec} 不支持 {decoder} 硬件解码，请选择软件解码"
        ))
    params["decoder"] = decoder
    container = params.get("container", "mp4")
    if container not in _CONTAINERS:
        raise HTTPException(400, "container 仅支持 mp4 / mkv / mov")
    params["container"] = container
    audio_mode = params.get("audio_mode", "auto")
    if audio_mode not in _AUDIO_MODES:
        raise HTTPException(400, f"audio_mode 仅支持 {' / '.join(_AUDIO_MODES)}")
    if audio_mode == "flac" and container != "mkv":
        raise HTTPException(400, "FLAC 音轨仅支持 mkv 容器（mp4 请选 aac）")
    params["audio_mode"] = audio_mode
    # 默认 auto（保留）：与音轨 auto 对齐——mkv 原样 copy 无损、mp4 文本转
    # mov_text；图形字幕进不了 mp4 由 worker 落日志说明，不再静默丢轨
    subtitle_mode = params.get("subtitle_mode", "auto")
    if subtitle_mode not in ("none", "auto"):
        raise HTTPException(400, "subtitle_mode 仅支持 none / auto")
    params["subtitle_mode"] = subtitle_mode
    interp = params.get("interp", "off")
    if interp not in ("off", "rife2x"):
        raise HTTPException(400, "interp 仅支持 off / rife2x")
    params["interp"] = interp
    # 前置滤镜（反交错/去色带）：布尔开关，滤镜串由 worker 按 prefilter_chain 拼装
    for k in ("deinterlace", "deband"):
        v = params.get(k, False)
        if not isinstance(v, bool):
            raise HTTPException(400, f"{k} 需为布尔值")
        params[k] = v
    if params.get("denoise") is not None:
        if int(params["denoise"]) not in (0, 1, 2, 3):
            raise HTTPException(400, "denoise 仅支持 0 / 1 / 2 / 3")
        params["denoise"] = int(params["denoise"])
    crf = params.get("crf", 18)
    if not (0 <= int(crf) <= 51):
        raise HTTPException(400, "crf 范围 0-51")
    params["crf"] = int(crf)
    preset = params.get("preset", "medium")
    if preset not in _PRESETS_XCODE:
        raise HTTPException(400, f"preset 仅支持 {' / '.join(_PRESETS_XCODE)}")
    params["preset"] = preset
    for k, hi in (("batch", 16), ("chunk", 1024)):
        v = params.get(k)
        if v is not None:
            if not isinstance(v, int) or isinstance(v, bool) or not (1 <= v <= hi):
                raise HTTPException(400, f"{k} 需为 1~{hi} 的整数")
            params[k] = v

    res_label = f"{tw}x{th}" if (tw is not None and th is not None) else f"{target}x"
    # 未显式指定输出时：全局设置里的输出目录优先（目录懒创建），否则沿用源视频同目录
    _st = load_settings()
    odir = str(_st.get("output_dir") or "").strip()
    _tmpl = str(_st.get("output_name_template") or "")
    out_root = Path(odir) if odir else input_path.parent
    ow, oh = (tw, th) if tw is not None else (info.width * target, info.height * target)
    stem = _render_output_stem(_tmpl, input_path.stem, body.model_id, res_label, ow, oh)
    if out_kind == "video":
        # 目录无同名则沿用（模板渲染后的）名字，同名冲突（含源文件本身）
        # 退 _倍率 后缀——同规则见 _sr_output_name
        out = body.output or _sr_output_name(
            out_root, stem, container, res_label, set())
    else:
        # 图片序列：输出是文件夹，帧图按 000001.png 起逐帧编号
        out = body.output or str(out_root / f"{stem}_{res_label}_frames")
        if Path(out).exists() and not Path(out).is_dir():
            raise HTTPException(400, "图片序列的输出路径需为文件夹")
    try:  # 目录不存在时自动创建（设置里指向新盘/新目录的场景）；失败给出可读错误
        if out_kind == "video":
            Path(out).parent.mkdir(parents=True, exist_ok=True)
        else:
            Path(out).mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise HTTPException(400, f"无法创建输出目录 {Path(out).parent}: {e}") from e
    task = db.new_task(
        str(input_path), out, body.model_id, params,
        src={"w": info.width, "h": info.height, "fps": info.fps,
             "total_frames": info.total_frames},
    )
    runner.notify_queue_activity_threadsafe()  # 队列又有活干：撤掉挂着的完成动作倒计时
    bus.publish({"type": "task_status", "task_id": task["id"], "status": "queued"})
    return task


def _with_queue_pos(tasks: list[dict]) -> list[dict]:
    qpos = 0
    for t in tasks:
        if t["status"] == "queued":
            qpos += 1
            t["queue_position"] = qpos
        else:
            t["queue_position"] = None
    return tasks


def sr_log_ids() -> set[str]:
    """已有性能日志的任务 id 集合（一次 listdir，标注列表用）。"""
    try:
        return {p[:-4] for p in os.listdir(SR_LOG_DIR) if p.endswith(".log")}
    except OSError:
        return set()


@app.get("/api/tasks")
def get_tasks() -> list[dict]:
    logs = sr_log_ids()
    out = _with_queue_pos(db.list_tasks())
    for t in out:
        t["has_sr_log"] = t["id"] in logs
    return out


@app.get("/api/tasks/{task_id}/sr-log")
def task_sr_log(task_id: str):
    """超分性能日志文本（sr_profiling 开启时任务结束落盘；无则 404）。"""
    p = SR_LOG_DIR / f"{task_id}.log"
    if not p.is_file():
        raise HTTPException(404, "该任务没有性能日志")
    return PlainTextResponse(p.read_text(encoding="utf-8", errors="replace"))


@app.get("/api/stats")
def get_stats() -> dict:
    """首页四宫格 + 处理时机闸门状态（任务页"挂起中"提示用）。"""
    return db.stats() | {"queue_gate": runner.gate_state()}


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str) -> dict:
    t = db.get_task(task_id)
    if t is None:
        raise HTTPException(404)
    t["has_sr_log"] = task_id in sr_log_ids()
    return t


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str) -> dict:
    ok = await runner.cancel(task_id)
    if not ok:
        raise HTTPException(409, "任务不存在或不可取消")
    return {"ok": True}


@app.delete("/api/tasks/{task_id}")
async def remove_task(task_id: str) -> dict:
    t = db.get_task(task_id)
    if t is None:
        raise HTTPException(404)
    if t["status"] == "running":
        raise HTTPException(409, "任务运行中，请先取消")
    if not db.delete_task(task_id):
        raise HTTPException(409, "删除失败")
    # 工作目录（续跑用）与预览图随任务删除一并清理；大目录可能数百 MB，
    # 放后台线程执行不阻塞响应
    asyncio.get_running_loop().run_in_executor(None, purge_task_files, task_id)
    # 广播删除事件：WS 健康时轮询间隔 8s，靠事件让列表即时刷新
    bus.publish({"type": "task_deleted", "task_id": task_id})
    return {"ok": True}


def purge_task_files(task_id: str) -> None:
    """删除任务专属临时产物：分段/分块工作目录 + 预览图 + 对比静帧缓存 + 性能日志。"""
    for d in (TEMP_DIR / "segmented" / task_id, TEMP_DIR / "chunked" / task_id):
        shutil.rmtree(d, ignore_errors=True)
    task_stills.clear(task_id)
    pv = TEMP_DIR / "previews"
    if pv.is_dir():
        for p in pv.glob(f"{task_id}*.jpg"):
            try:
                p.unlink()
            except OSError:
                pass
    try:
        (SR_LOG_DIR / f"{task_id}.log").unlink(missing_ok=True)
    except OSError:
        pass


def sweep_orphan_workdirs(max_age_s: float = 3600.0) -> None:
    """启动清扫：崩溃/强杀遗留的孤儿工作目录（数据库无对应任务）。

    只清修改时间超过 max_age_s 的目录——避免误删其他 sidecar 实例
    （测试/开发）刚建的任务目录。取消任务的目录因任务行还在而保留（续跑依据）。
    """
    ids = db.all_task_ids()
    deadline = time.time() - max_age_s
    for sub in ("segmented", "chunked"):
        base = TEMP_DIR / sub
        if not base.is_dir():
            continue
        for d in base.iterdir():
            try:
                if d.is_dir() and d.name not in ids and d.stat().st_mtime < deadline:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                continue


def gc_sr_logs(keep: int = 200) -> int:
    """性能日志留最近 keep 份（按修改时间），防止长年累月无界增长。"""
    try:
        files = sorted(
            (p for p in SR_LOG_DIR.iterdir() if p.suffix == ".log"),
            key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return 0
    removed = 0
    for p in files[keep:]:
        try:
            p.unlink()
            removed += 1
        except OSError:
            continue
    return removed


class TaskReorder(BaseModel):
    ids: list[str]


@app.post("/api/tasks/reorder")
def reorder_tasks(body: TaskReorder) -> dict:
    """按传入顺序重排排队任务（拖拽排序）；非排队 id 自动跳过。"""
    n = db.reorder_queued(body.ids)
    return {"ok": True, "reordered": n}


# ---- 视频剪切（轻量后台任务，与超分队列独立；产物可直接作为超分输入）----


class TrimCreate(BaseModel):
    input: str
    start_s: float = Field(ge=0)
    end_s: float
    mode: str = "smart"  # smart | fast | exact
    output: str | None = None


_trim_jobs: dict[str, dict] = {}
_trim_queue: "queue.Queue[str]" = queue.Queue()
_trim_thread: threading.Thread | None = None


def _start_trim_worker() -> None:
    """lifespan 里启动（import 即起线程对测试不友好；幂等防多次启停重启）"""
    global _trim_thread
    if _trim_thread is not None and _trim_thread.is_alive():
        return
    _trim_thread = threading.Thread(target=_trim_worker, daemon=True, name="trim-worker")
    _trim_thread.start()


def _trim_worker() -> None:
    from ..pipeline.trim import TrimCanceled
    from ..utils.process import kill_tree

    while True:
        jid = _trim_queue.get()
        job = _trim_jobs.get(jid)
        if job is None:
            continue
        if job.get("cancel_requested"):  # 排队期被取消：不再执行
            job.update(state="canceled", error=None)
            continue
        job["proc"] = None

        def _spawn_hook(proc, _job=job):
            _job["proc"] = proc  # 取消端点据此 kill

        try:
            res = run_trim(
                job["input"], job["start_s"], job["end_s"], job["mode"], job["output"],
                nvenc=bool(cached_hardware().get("nvenc")),
                progress_cb=lambda p: job.update(state="running", progress=round(p, 4)),
                on_spawn=_spawn_hook,
                cancel_check=lambda: bool(job.get("cancel_requested")),
            )
            job.update(
                state="done", progress=1.0, output=str(res.output),
                actual_start_s=round(res.actual_start, 3),
                duration_s=round(res.duration_s, 3),
                mode=res.mode, notices=res.notices,
            )
        except (TrimCanceled, Exception) as e:  # noqa: BLE001 — 后台线程兜底
            if job.get("cancel_requested") or isinstance(e, TrimCanceled):
                job.update(state="canceled", error=None)
                bus.publish_threadsafe({"type": "trim", "job_id": jid, "state": "canceled"})
            else:
                job.update(state="failed", error=str(e))


def _ts_label(t: float) -> str:
    m, s = divmod(int(t), 60)
    return f"{m:02d}-{s:02d}"


@app.post("/api/trim", status_code=201)
def create_trim(body: TrimCreate) -> dict:
    input_path = Path(body.input)
    if not input_path.exists():
        raise HTTPException(400, f"输入文件不存在: {body.input}")
    if body.mode not in ("smart", "fast", "exact"):
        raise HTTPException(400, "mode 仅支持 smart / fast / exact")
    try:
        info = probe(input_path)
    except UnsupportedMedia as e:
        raise HTTPException(422, str(e))
    if body.end_s <= body.start_s:
        raise HTTPException(400, "出点必须晚于入点")
    if body.start_s >= info.duration_s:
        raise HTTPException(400, "入点超出视频时长")
    output = body.output
    if not output:
        # 与超分任务同一规则：全局输出目录优先，否则源视频同目录
        _odir = str(load_settings().get("output_dir") or "").strip()
        _root = Path(_odir) if _odir else input_path.parent
        output = str(_root / f"{input_path.stem}_cut_{_ts_label(body.start_s)}-{_ts_label(body.end_s)}.mp4")
        try:
            _root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise HTTPException(400, f"无法创建输出目录 {_root}: {e}") from e
    # 简单 GC：完成态任务超过 50 个时丢最旧的
    done_ids = [k for k, v in _trim_jobs.items() if v["state"] in ("done", "failed")]
    for k in done_ids[: max(0, len(done_ids) - 50)]:
        _trim_jobs.pop(k, None)
    jid = uuid4().hex[:12]
    _trim_jobs[jid] = {
        "state": "queued", "progress": 0.0, "input": str(input_path),
        "start_s": body.start_s, "end_s": body.end_s, "mode": body.mode,
        "output": output, "error": None, "cancel_requested": False,
    }
    _start_trim_worker()  # 懒启动（幂等）：TestClient 不触发 lifespan 也能跑
    _trim_queue.put(jid)
    return {"job_id": jid, "output": output}


@app.get("/api/trim/{job_id}")
def trim_status(job_id: str) -> dict:
    job = _trim_jobs.get(job_id)
    if job is None:
        raise HTTPException(404)
    # proc（Popen 句柄）等内部字段不能进 JSON
    return {k: v for k, v in job.items() if k != "proc"}


@app.post("/api/trim/{job_id}/cancel")
def cancel_trim(job_id: str) -> dict:
    """取消排队/进行中的剪切：杀当前 ffmpeg 段，后续段不再执行。"""
    from ..utils.process import kill_tree

    job = _trim_jobs.get(job_id)
    if job is None:
        raise HTTPException(404)
    if job["state"] not in ("queued", "running"):
        raise HTTPException(409, "任务已结束")
    job["cancel_requested"] = True
    proc = job.get("proc")
    if proc is not None and proc.poll() is None:
        kill_tree(proc.pid)
    if job["state"] == "queued":  # 还没被 worker 领走：直接落终态
        job.update(state="canceled", error=None)
    return {"ok": True}


# ---- 模型对比 ----


class CompareCreate(BaseModel):
    kind: str  # image | video
    input: str
    start_s: float = 0.0
    end_s: float = 0.0
    models: list[str]
    scale: int


@app.post("/api/compare", status_code=201)
def create_compare(body: CompareCreate) -> dict:
    """创建模型对比作业：同一素材 × 多模型并排处理（详见 sv/server/compare.py）。"""
    if body.kind not in ("image", "video"):
        raise HTTPException(400, "kind 仅支持 image / video")
    input_path = Path(body.input)
    if not input_path.exists():
        raise HTTPException(400, f"输入文件不存在: {body.input}")
    if not (2 <= len(body.models) <= compare.MAX_MODELS):
        raise HTTPException(400, f"请选择 2~{compare.MAX_MODELS} 个模型对比")
    if len(set(body.models)) != len(body.models):
        raise HTTPException(400, "模型列表存在重复")
    if body.scale <= 0:
        raise HTTPException(400, "scale 需为正整数")
    try:
        specs = load_registry()
        for mid in body.models:
            if mid not in specs:
                raise HTTPException(404, f"未知模型 {mid}")
            if specs[mid].engine == "torch":
                raise HTTPException(400, f"{specs[mid].name} 是 torch 引擎，暂不参与对比")
            if body.scale not in specs[mid].scale:
                raise HTTPException(400, (
                    f"{specs[mid].name} 不支持 x{body.scale}（可选 {specs[mid].scale}），"
                    "请选择所有模型共同支持的倍率"))
    except KeyError as e:
        raise HTTPException(404, str(e)) from e

    if body.kind == "video":
        from ..pipeline.probe import UnsupportedMedia, probe

        try:
            info = probe(input_path)
        except (UnsupportedMedia, FileNotFoundError) as e:
            raise HTTPException(422, str(e))
        start = max(0.0, float(body.start_s))
        end = min(float(body.end_s), info.duration_s)
        if end <= start:
            raise HTTPException(400, "出点必须晚于入点")
        if end - start > compare.MAX_SEG_S:
            end = start + compare.MAX_SEG_S  # 超长自动截断，不拒绝
    else:
        if input_path.suffix.lower() not in _IMAGE_EXTS:
            raise HTTPException(400, "图片对比仅支持 png/jpg/webp/bmp/tiff")
        from PIL import Image, ImageOps

        try:
            with Image.open(str(input_path)) as im:
                ImageOps.exif_transpose(im).size
        except OSError as e:
            raise HTTPException(400, f"无法读取图片: {e}") from e
        start = end = 0.0
    job = compare.create(body.kind, input_path, start, end, body.models, body.scale)
    return _compare_view(job)


def _compare_view(job: dict) -> dict:
    """对外视图：路径收敛成 asset key，不暴露磁盘绝对路径。"""
    return job | {
        "entries": [
            {k: v for k, v in e.items()
             if k not in ("output", "still")} | {"has_output": bool(e.get("output"))}
            for e in job["entries"]
        ],
    }


@app.get("/api/compare/cache")
def compare_cache_stats() -> dict:
    """对比产物占用统计（设置页展示）：模型对比作业 + 任务静帧缓存合并口径。
    注意必须注册在 /{job_id} 之前，否则 "cache" 会被当作 job_id 吞掉。"""
    a, b = compare.cache_stats(), task_stills.cache_stats()
    return {"jobs": a["jobs"] + b["jobs"], "bytes": a["bytes"] + b["bytes"]}


@app.delete("/api/compare/cache")
def clear_compare_cache() -> dict:
    """清理全部对比产物（模型对比作业目录 + 任务静帧缓存）；有正在写的拒绝。"""
    try:
        r1 = compare.clear_cache()
        r2 = task_stills.clear_all()
        return {"removed_jobs": r1["removed_jobs"] + r2["removed_jobs"],
                "freed_bytes": r1["freed_bytes"] + r2["freed_bytes"]}
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


@app.get("/api/compare/{job_id}")
def compare_status(job_id: str) -> dict:
    job = compare.get(job_id)
    if job is None:
        raise HTTPException(404)
    return _compare_view(job)


@app.post("/api/compare/{job_id}/cancel")
def cancel_compare(job_id: str) -> dict:
    if not compare.request_cancel(job_id):
        raise HTTPException(409, "作业已结束")
    return {"ok": True}


@app.get("/api/compare/{job_id}/asset/{key:path}")
def compare_asset(job_id: str, key: str):
    """对比产物文件：key 为白名单标识（seg/src_still/<i>/out/<mid>/still/<mid>[/<i>]），
    不开放任意路径。"""
    p = compare.asset_path(job_id, key)
    if p is None:
        raise HTTPException(404)
    media = "video/mp4" if p.suffix == ".mp4" else "image/png"
    return FileResponse(p, media_type=media)


@app.post("/api/tasks/{task_id}/resume")
def resume_task(task_id: str) -> dict:
    """续跑失败/取消的任务：回到队列，worker 按 checkpoint 跳过已完成部分。"""
    t = db.get_task(task_id)
    if t is None:
        raise HTTPException(404)
    if t["status"] not in ("failed", "canceled"):
        raise HTTPException(409, "仅失败或取消的任务可续跑")
    if not Path(t["input_path"]).exists():
        raise HTTPException(409, "输入文件已不存在，无法续跑")
    try:
        spec = get_model(t["model_id"])
    except ModelNotFoundError:
        raise HTTPException(409, "模型已不存在，无法续跑")
    # 流式(ONNX)任务续跑依赖分段工作目录；无目录时仅当任务从未开始（无进度）才允许从头重跑。
    # torch 任务无目录则从头跑（解码是幂等的）
    if spec.engine == "onnx" and not (TEMP_DIR / "segmented" / task_id).exists() \
            and (t["progress_frames"] or 0) > 0:
        raise HTTPException(409, "续跑数据已不存在（临时目录被清理），请新建任务")
    # 半成品输出已在失败/取消时删除；万一残留，续跑前清掉避免混淆
    try:
        Path(t["output_path"]).unlink(missing_ok=True)
    except OSError:
        pass
    db.update_task(task_id, status="queued", error=None, progress_frames=0)
    runner.notify_queue_activity_threadsafe()  # 续跑也是队列活动：撤掉完成动作倒计时
    bus.publish({"type": "task_status", "task_id": task_id, "status": "queued"})
    return {"ok": True}


@app.get("/api/tasks/{task_id}/preview")
def task_preview(task_id: str, src: int = 0):
    """处理预览帧；?src=1 返回源首帧（对比预览左半边）。"""
    t = db.get_task(task_id)
    if t is None:
        raise HTTPException(404)
    p = t.get("preview_src") if src else t.get("preview_path")
    if not p or not Path(p).exists():
        raise HTTPException(404, "暂无预览")
    return FileResponse(p, media_type="image/jpeg")


@app.get("/api/tasks/{task_id}/stills")
def task_stills_status(task_id: str) -> dict:
    """任务对比页多帧静帧状态：ready 给样本数与版本号，未建则后台起构建
    （前端轮询本接口到 ready）；unsupported=无多帧概念，回落单对预览图。"""
    t = db.get_task(task_id)
    if t is None:
        raise HTTPException(404)
    return task_stills.status(t)


@app.get("/api/tasks/{task_id}/stills/{i}")
def task_still_file(task_id: str, i: int, src: int = 0):
    """任务静帧样本文件（PNG 无损）；未就绪/越界 404。"""
    t = db.get_task(task_id)
    if t is None:
        raise HTTPException(404)
    p = task_stills.asset_path(task_id, i, bool(src))
    if p is None:
        raise HTTPException(404)
    return FileResponse(p, media_type="image/png")


# ---- 对比分享卡片（晒图长图 / 滑块动图）----


class ShareCardBody(BaseModel):
    kind: str = "image"  # image 长图（PNG） | gif 滑块动图
    t_s: float = -1  # 抽帧时间点；<0 = 自动取 40% 时长（避开片头黑场）


def _share_card_meta(task: dict) -> dict:
    p = task.get("params") or {}
    tw, th = p.get("target_w"), p.get("target_h")
    if tw and th:
        scale = f"{task.get('src_w', 0)}→{tw}×{th}"
    else:
        k = p.get("target_scale") or p.get("scale") or 1
        scale = f"x{k}"
    try:
        model = get_model(task["model_id"]).name
    except Exception:  # noqa: BLE001 — 模型被删也能出图，回退 id
        model = task["model_id"]
    return {
        "model": model, "scale": scale,
        "name": Path(task["input_path"]).stem,
        "date": time.strftime("%Y-%m-%d"),
    }


@app.post("/api/tasks/{task_id}/share-card", status_code=201)
async def create_share_card(task_id: str, body: ShareCardBody) -> dict:
    """生成对比分享卡片：源与任务输出在同一时间点抽帧合成。"""
    if body.kind not in ("image", "gif"):
        raise HTTPException(400, "kind 仅支持 image / gif")
    t = db.get_task(task_id)
    if t is None:
        raise HTTPException(404)
    if t["status"] != "done" or not Path(t["output_path"]).exists():
        raise HTTPException(409, "任务未完成或输出已不存在，暂无法生成")
    src, out = Path(t["input_path"]), Path(t["output_path"])
    if out.is_dir():
        raise HTTPException(409, "图片序列任务的输出是文件夹，暂不支持分享卡片")
    if not src.exists():
        raise HTTPException(409, "源文件已不存在")
    try:
        info = probe(src)
    except (UnsupportedMedia, FileNotFoundError) as e:
        raise HTTPException(409, f"源探测失败: {e}") from e
    t_s = body.t_s if body.t_s >= 0 else info.duration_s * 0.4
    t_s = max(0.0, min(t_s, info.duration_s - 0.05))
    meta = _share_card_meta(t)

    from ..sharecard import make_long_image, make_slider_gif

    suffix = "png" if body.kind == "image" else "gif"
    dest = TEMP_DIR / "share" / f"{task_id}_{body.kind}.{suffix}"
    loop = asyncio.get_running_loop()
    try:
        if body.kind == "image":
            await loop.run_in_executor(
                None, make_long_image, src, out, t_s, t_s, meta, dest)
        else:
            await loop.run_in_executor(
                None, make_slider_gif, src, out, t_s, t_s, meta, dest)
    except Exception as e:  # noqa: BLE001 — 抽帧/合成失败给可读错误
        raise HTTPException(422, f"分享卡片生成失败: {e}") from e
    return {"path": str(dest), "kind": body.kind,
            "url": f"/api/tasks/{task_id}/share-card/file?kind={body.kind}&t={time.time()}"}


@app.get("/api/tasks/{task_id}/share-card/file")
def share_card_file(task_id: str, kind: str = "image"):
    """分享卡片产物文件（POST 生成后此处取；无则 404）。"""
    if kind not in ("image", "gif"):
        raise HTTPException(400, "kind 仅支持 image / gif")
    suffix = "png" if kind == "image" else "gif"
    p = TEMP_DIR / "share" / f"{task_id}_{kind}.{suffix}"
    if not p.is_file():
        raise HTTPException(404, "请先生成分享卡片")
    return FileResponse(p, media_type=f"image/{suffix}")


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    toks = _expected_tokens()
    if toks and ws.query_params.get("token") not in toks:  # WS 不受 CORS 约束，必须自查令牌
        await ws.close(code=4401)
        return
    await ws.accept()
    q = bus.subscribe()
    # 客户端不发消息；并发挂一个 receive 作断连哨兵，否则 q.get() 永远发现不了断开
    sentinel = asyncio.create_task(ws.receive())
    try:
        while True:
            send_task = asyncio.create_task(q.get())
            done, _ = await asyncio.wait(
                {sentinel, send_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if sentinel in done:
                send_task.cancel()
                break
            if send_task not in done:
                continue
            event = send_task.result()
            await ws.send_text(json.dumps(event, ensure_ascii=False))
    except Exception:  # 断开时 send 抛异常，走统一清理
        pass
    finally:
        sentinel.cancel()
        bus.unsubscribe(q)
