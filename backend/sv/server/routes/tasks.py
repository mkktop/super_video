"""任务域路由：创建（视频/图片）、列表、批量操作、续跑、预览/静帧/性能日志/分享卡。"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from ...models.registry import ModelNotFoundError, get_model, load_registry
from ...paths import SR_LOG_DIR, TEMP_DIR
from ...pipeline.probe import (
    DECODERS,
    HWACCEL_CODECS,
    UnsupportedMedia,
    probe,
    validate_m0,
)
from .. import db, task_stills
from ..settings import load as load_settings
from ..state import bus, cached_hardware, runner

router = APIRouter(tags=["tasks"])

log = logging.getLogger("sv.app")


class TaskCreate(BaseModel):
    input: str = ""  # 单输入；图片批量时可为空、改用 inputs 列表
    inputs: list[str] | None = None  # 图片批量：一次建一个任务循环处理
    output: str | None = None
    model_id: str
    params: dict = Field(default_factory=dict)  # scale/codec/crf/preset/tile/interp/denoise 每任务独立
    overwrite: bool = False  # 显式 output 撞已存在文件/活动任务时 409，确认覆盖后带 True 重交


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


def _active_output_keys() -> set[str]:
    """排队/运行中任务将写入的输出路径集合（normcase 归一）。

    自动命名的后缀避让必须把它算进去：A 刚建还没跑、磁盘上尚无产物时，
    B 再建同名任务 plain.exists() 探不到碰撞，会静默互相覆盖。显式路径
    则用于创建期 409 预检。"""
    return {
        os.path.normcase(t["output_path"])
        for t in db.list_tasks()
        if t["status"] in ("queued", "running") and t.get("output_path")
    }


def _reject_output_conflict(out: str, active_outs: set[str], overwrite: bool) -> None:
    """用户显式指定输出路径的覆盖预检：目标文件已存在或已有活动任务将写入
    同一路径时 409（前端弹确认，带 overwrite=True 重交）。"""
    if overwrite:
        return
    if os.path.normcase(out) in active_outs:
        raise HTTPException(
            409, f"已有排队/运行中的任务将写入同一路径：{out}")
    if Path(out).exists():
        raise HTTPException(409, f"输出文件已存在：{out}")


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
    from ..consts import _IMAGE_EXTS

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
    # 逐图输出命名：目录无同名则沿用原名，冲突退 _倍率 后缀（不覆盖现有文件；
    # 尚未开跑的活动任务占用的名字同样要避让）
    images_meta: list[dict] = []
    used: set[str] = _active_output_keys()
    ow, oh = (tw, th) if tw is not None else (src_w * target, src_h * target)
    for p in paths:
        if batch or not body.output:
            stem = _render_output_stem(tmpl, p.stem, body.model_id, res_label, ow, oh)
            out = _sr_output_name(out_root, stem, fmt, res_label, used)
        else:
            _reject_output_conflict(str(body.output), used, body.overwrite)
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


@router.post("/api/tasks", status_code=201)
def create_task(body: TaskCreate) -> dict:
    from ..consts import _AUDIO_MODES, _CODECS, _CONTAINERS, _IMAGE_EXTS, _PRESETS_XCODE

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
    active_outs = _active_output_keys()
    if out_kind == "video":
        if body.output:
            # 显式路径：静默覆盖不可接受，撞已存在文件/活动任务时 409 让前端确认
            _reject_output_conflict(body.output, active_outs, body.overwrite)
            out = body.output
        else:
            # 目录无同名则沿用（模板渲染后的）名字，同名冲突（含源文件本身与
            # 尚未开跑的活动任务）退 _倍率 后缀——同规则见 _sr_output_name
            out = _sr_output_name(out_root, stem, container, res_label, active_outs)
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


def _input_exists(t: dict) -> bool:
    """源素材是否仍在本机：对比页视频模式直接播源文件、静帧从源抽取，
    源被删/移动后入口就该置灰。图片批量任务看 params.images 全部清单。"""
    imgs = t.get("params", {}).get("images")
    if isinstance(imgs, list) and imgs:
        return all(
            isinstance(i, dict) and bool(i.get("in")) and Path(i["in"]).exists()
            for i in imgs
        )
    ip = t.get("input_path") or ""
    return bool(ip) and Path(ip).exists()


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


@router.get("/api/tasks")
def get_tasks(q: str = "") -> list[dict]:
    logs = sr_log_ids()
    out = _with_queue_pos(db.list_tasks(q=q.strip()))
    for t in out:
        t["has_sr_log"] = t["id"] in logs
        t["input_exists"] = _input_exists(t)
    return out


@router.get("/api/tasks/{task_id}/sr-log")
def task_sr_log(task_id: str):
    """超分性能日志文本（sr_profiling 开启时任务结束落盘；无则 404）。"""
    p = SR_LOG_DIR / f"{task_id}.log"
    if not p.is_file():
        raise HTTPException(404, "该任务没有性能日志")
    return PlainTextResponse(p.read_text(encoding="utf-8", errors="replace"))


@router.get("/api/stats")
def get_stats() -> dict:
    """首页四宫格 + 处理时机闸门状态（任务页"挂起中"提示用）。"""
    return db.stats() | {"queue_gate": runner.gate_state()}


@router.get("/api/tasks/{task_id}")
def get_task(task_id: str) -> dict:
    t = db.get_task(task_id)
    if t is None:
        raise HTTPException(404)
    t["has_sr_log"] = task_id in sr_log_ids()
    t["input_exists"] = _input_exists(t)
    return t


@router.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str) -> dict:
    ok = await runner.cancel(task_id)
    if not ok:
        raise HTTPException(409, "任务不存在或不可取消")
    return {"ok": True}


@router.delete("/api/tasks/{task_id}")
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


@router.post("/api/tasks/reorder")
def reorder_tasks(body: TaskReorder) -> dict:
    """按传入顺序重排排队任务（拖拽排序）；非排队 id 自动跳过。"""
    n = db.reorder_queued(body.ids)
    return {"ok": True, "reordered": n}


class TaskBatch(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=200)
    action: str  # cancel | delete | resume


@router.post("/api/tasks/batch")
async def batch_tasks(body: TaskBatch) -> dict:
    """批量取消/删除/续跑：逐个复用单任务端点的完整校验路径，失败不中断，
    返回逐条结果（部分成功语义），前端按 failed 内容提示。"""
    if body.action not in ("cancel", "delete", "resume"):
        raise HTTPException(400, "action 仅支持 cancel / delete / resume")
    done: list[str] = []
    failed: dict[str, str] = {}
    for tid in body.ids:
        try:
            if body.action == "cancel":
                await cancel_task(tid)
            elif body.action == "delete":
                await remove_task(tid)
            else:
                resume_task(tid)
            done.append(tid)
        except HTTPException as e:
            failed[tid] = str(e.detail)
    return {"ok": True, "done": done, "failed": failed}


@router.post("/api/tasks/{task_id}/resume")
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


@router.get("/api/tasks/{task_id}/preview")
def task_preview(task_id: str, src: int = 0):
    """处理预览帧；?src=1 返回源首帧（对比预览左半边）。"""
    t = db.get_task(task_id)
    if t is None:
        raise HTTPException(404)
    p = t.get("preview_src") if src else t.get("preview_path")
    if not p or not Path(p).exists():
        raise HTTPException(404, "暂无预览")
    return FileResponse(p, media_type="image/jpeg")


@router.get("/api/tasks/{task_id}/stills")
def task_stills_status(task_id: str) -> dict:
    """任务对比页多帧静帧状态：ready 给样本数与版本号，未建则后台起构建
    （前端轮询本接口到 ready）；unsupported=无多帧概念，回落单对预览图。"""
    t = db.get_task(task_id)
    if t is None:
        raise HTTPException(404)
    return task_stills.status(t)


@router.get("/api/tasks/{task_id}/stills/{i}")
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


@router.post("/api/tasks/{task_id}/share-card", status_code=201)
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

    from ...sharecard import make_long_image, make_slider_gif

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


@router.get("/api/tasks/{task_id}/share-card/file")
def share_card_file(task_id: str, kind: str = "image"):
    """分享卡片产物文件（POST 生成后此处取；无则 404）。"""
    if kind not in ("image", "gif"):
        raise HTTPException(400, "kind 仅支持 image / gif")
    suffix = "png" if kind == "image" else "gif"
    p = TEMP_DIR / "share" / f"{task_id}_{kind}.{suffix}"
    if not p.is_file():
        raise HTTPException(404, "请先生成分享卡片")
    return FileResponse(p, media_type=f"image/{suffix}")
