"""模型域路由：模型清单/删除/导入/下载、预设、媒体探测、TRT 可选组件。"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ...models import manager
from ...models.registry import (
    BUNDLED_DIR,
    USER_REGISTRY_DIR,
    load_registry,
    model_dir,
)
from ...pipeline.probe import (
    UnsupportedMedia,
    probe,
    probe_hwaccel,
    validate_m0,
)
from .. import trt_component, user_presets
from ..state import bus, cached_hardware, _download_lock

log = logging.getLogger("sv.app")

router = APIRouter(tags=["models"])


class ModelImport(BaseModel):
    path: str
    id: str = Field(min_length=2, max_length=48)
    name: str = Field(min_length=1, max_length=48)
    scale: int = Field(ge=2, le=8)
    color: str = "rgb"            # rgb | bgr
    value_range: str = "0-1"      # 0-1 | 0-255
    tile: int = 0
    description: str = ""


_PRESETS_PATH = Path(__file__).parent.parent / "presets.json"


def _presets() -> list[dict]:
    return json.loads(_PRESETS_PATH.read_text(encoding="utf-8"))


@router.get("/api/trt-component")
def get_trt_component() -> dict:
    """TRT 可选组件状态（安装/版本/体积/显卡架构/资产清单/安装进度）。"""
    return trt_component.status()


@router.post("/api/trt-component/install")
def install_trt_component() -> dict:
    accepted, msg = trt_component.start_install(bus)
    if not accepted:
        raise HTTPException(409, msg)
    return {"ok": True}


@router.delete("/api/trt-component")
def uninstall_trt_component() -> dict:
    ok, msg = trt_component.uninstall()
    if not ok:
        raise HTTPException(409, msg)
    return {"ok": True}


@router.get("/api/presets")
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


@router.post("/api/presets", status_code=201)
def create_preset(body: PresetCreate) -> dict:
    from ..consts import _AUDIO_MODES, _CODECS, _CONTAINERS

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


@router.delete("/api/presets/{preset_id}")
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


@router.post("/api/probe")
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
        # 媒体属性透出（UI 信息卡展示 10bit/VFR/隔行，用户据此决定预处理开关）
        "bit_depth": getattr(info, "bit_depth", 8),
        "vfr": bool(getattr(info, "vfr", False)),
        "field_order": getattr(info, "field_order", "progressive") or "progressive",
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
            from ...pipeline.analyze import sample_frame_stats
            from ...pipeline.recommend import build_recommendation

            stats = sample_frame_stats(info)
            if stats is not None:
                resp["recommend"] = build_recommendation(info, stats)
        except Exception as e:  # noqa: BLE001
            log.warning(f"源分析失败（跳过推荐）: {e}")
    return resp


@router.get("/api/models")
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


@router.delete("/api/models/{model_id}")
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


@router.post("/api/models/import", status_code=201)
def import_model(body: ModelImport) -> dict:
    """自定义 ONNX 导入：拷贝入库 -> 真跑一帧验证 -> 写 manifest。"""
    import re as _re
    import shutil as _shutil

    import numpy as np

    from ...engines.onnx_engine import OnnxSrEngine

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


@router.post("/api/models/{model_id}/download")
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

            def on_source(url, label):
                # 渠道透出：按实际命中的下载 URL 判定（主源 ModelScope，回落 GitHub 镜像）
                src = ("modelscope" if "modelscope" in url
                       else "github" if "github" in url else "")
                if src:
                    bus.publish_threadsafe({
                        "type": "model_download", "model_id": model_id, "source": src,
                    })

            try:
                await loop.run_in_executor(None, manager.download, spec, cb, None, on_source)
                # 下载完成后生成 fp16 变体（bundled 模型随包已有，此处只补下载件）
                try:
                    from ...models.fp16 import ensure_fp16

                    await loop.run_in_executor(None, ensure_fp16, spec)
                except ImportError:
                    log.warning("fp16 转换依赖缺失（onnx/onnxconverter-common），保持 fp32")
                except Exception as e:  # noqa: BLE001 — 转换失败不影响使用 fp32
                    log.warning(f"fp16 转换失败，保持 fp32: {e}")
                bus.publish({"type": "model_download", "model_id": model_id, "done": True})
            except Exception as e:  # noqa: BLE001
                bus.publish({"type": "model_download", "model_id": model_id,
                             "failed": str(e)})

    # 即时反馈：真实进度事件要等远端连上并收到首块数据才来（直连 GitHub 常以十秒计），
    # 先广播 0% 让前端立刻出进度条
    bus.publish({"type": "model_download", "model_id": model_id, "progress": 0.0})
    asyncio.create_task(_do())
    return {"ok": True, "started": True}
