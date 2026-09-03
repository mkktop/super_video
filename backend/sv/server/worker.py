"""任务 Worker：独立进程执行单个超分任务，stdout 输出 JSON 事件行。

由 runner 通过 `python -m sv.server.worker <task_id>` 拉起。
事件协议（每行一个 JSON）：
  {"type":"started","total_frames":n,"output":...}
  {"type":"progress","frames":n,"total":n,"fps":f,"eta_sec":e}
  {"type":"log","line":"..."}
  {"type":"done","frames":n,"elapsed":s,"out_bytes":b}
  {"type":"failed","error":"..."}
退出码：0 成功 / 1 失败 / 2 任务不存在 / 3 已取消
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sv.engines.onnx_engine import OnnxSrEngine
from sv.engines.rife import Rife2x
from sv.models import manager
from sv.models.fp16 import ensure_fp16_file
from sv.models.registry import get_model, model_file
from sv.paths import SR_LOG_DIR, TEMP_DIR
from sv.pipeline.probe import (
    DECODERS,
    UnsupportedMedia,
    probe,
    probe_hwaccel,
    validate_m0,
)
from sv.pipeline.segmented import SegmentedPipeline, read_eof_marker
from sv.pipeline.stream import (
    TEXT_SUBS,
    EncodeOpts,
    PipelineError,
    RunStats,
    TaskCanceled,
    prefilter_chain,
)
from sv.server import db, settings


def _encode_opts(params: dict, out_kind: str) -> EncodeOpts:
    """任务参数 → 编码选项（app.py 已做过白名单校验，此处 defensive 再夹一层默认值）。"""
    return EncodeOpts(
        codec=params.get("codec", "h264"),
        crf=int(params.get("crf", 18)),
        preset=params.get("preset", "medium"),
        audio_mode=params.get("audio_mode", "auto"),
        subtitle_mode=params.get("subtitle_mode", "auto"),
        container=params.get("container", "mp4"),
        out_kind=out_kind,
    )


def emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def emit_failed(prefix: str, e: Exception) -> None:
    """失败事件 + 完整堆栈落 sidecar.log（任务 error 保持单行可读）。

    runner 把 log 事件打到 sidecar.log（日志页可见）——异常被本层捕获后
    只有一行 error 上达任务卡，没有堆栈时「引擎加载失败 UnicodeDecodeError」
    这类问题只能靠猜（1060 实机教训）；堆栈行号能直接定位裸奔的读取点。
    注意 emit 的是 JSON 行：堆栈里不能有裸 \r，json.dumps 会转义，安全。
    """
    import traceback

    tb = traceback.format_exc()
    emit({"type": "log", "line": f"{prefix} {type(e).__name__} 堆栈:\n{tb.strip()}"})
    emit({"type": "failed", "error": f"{prefix} {type(e).__name__}: {e}"})


# ---- 超分性能日志（设置 sr_profiling 开启时生效）----
# 任务结束把"引擎配置 + 分段耗时明细 + 汇总"落到 SR_LOG_DIR/<task_id>.log，
# 供分析速度瓶颈（推理慢/解码跟不上/编码拖后腿/引擎加载占比）。旁路功能：
# 任何写入失败静默忽略，绝不影响任务本身。


def _prof_enabled() -> bool:
    return bool(settings.load().get("sr_profiling"))


def _prof_write(task_id: str, text: str) -> None:
    try:
        SR_LOG_DIR.mkdir(parents=True, exist_ok=True)
        with (SR_LOG_DIR / f"{task_id}.log").open("a", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass


def _prof_write_video_header(task_id: str, ctx: dict) -> None:
    seg_txt = (f"分段 ≈{ctx['seg']}帧 x {ctx['n_segs']}段" if ctx["n_segs"]
               else f"分块 {ctx['seg']}帧")
    lines = [
        f"==== {time.strftime('%Y-%m-%d %H:%M:%S')} 运行开始 ====",
        f"模型 {ctx['model']} · 推理后端 {ctx['provider']} · 精度 {ctx['precision']}"
        f" · GPU前后处理包装 {'开' if ctx['u8'] else '关'}",
        f"设置 engine={ctx['engine_setting']} · tile={ctx['tile']} · batch={ctx['batch']}"
        f" · 补帧={ctx['interp']} · 解码={ctx.get('decoder', 'sw')}"
        f" · 预处理={ctx.get('prefilter') or '无'}",
        f"源 {ctx['src_w']}x{ctx['src_h']} @{ctx['fps']:.3f}fps {ctx['frames']}帧"
        f" → 目标 {ctx['target']}",
        f"引擎加载+预热 {ctx['load_s']:.1f}s · {seg_txt}"
        f" · 双路并行 {'是(2进程)' if ctx['parallel'] else '否'}"
        f" · 断点续跑 {'是(跳过已完成段)' if ctx['resumed'] else '否'}",
    ]
    _prof_write(task_id, "\n".join(lines) + "\n")


def _prof_collect(task_id: str, work: Path) -> None:
    """任务成功收尾：把工作目录里的分段耗时明细（SegmentedPipeline 逐段落盘）
    并入持久日志。工作目录本身的清理仍由调用方原逻辑负责。"""
    perf_file = work / "perf_stages.jsonl"
    try:
        if perf_file.exists():
            body = perf_file.read_text(encoding="utf-8")
            _prof_write(task_id,
                        "---- 分段耗时明细（jsonl，每行一段；summary 行 ms_per_frame"
                        " 为各阶段毫秒/帧拆解，infer=推理 read=解码 write=编码）----\n"
                        + body)
    except OSError:
        pass


def _spawn_shard_child(task_id: str):
    """双路并行：拉起子进程跑第 2 路分段（与 runner 拉本进程同款入口形态）。"""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "worker", task_id, "--shard", "1", "2"]
    else:
        cmd = [sys.executable, "-m", "sv.server.worker", task_id, "--shard", "1", "2"]
    from sv.utils.process import WINDOWS_CREATE_FLAGS

    return subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=WINDOWS_CREATE_FLAGS, env={**os.environ, "PYTHONUNBUFFERED": "1"})


def _read_child_events(proc, state: dict, started_cb=None) -> None:
    """子分片进程 stdout 事件泵：聚合帧数/转发日志/捕获成败（守护线程）。

    frames 是该路的全局帧号（含段起点偏移），用增量累计成本路完成数，
    与协调者同口径相加才是真实总进度（直接相加两个全局号会翻倍）。
    """
    assert proc.stdout is not None
    last = 0
    for raw in proc.stdout:
        line = raw.decode("utf-8", "replace").strip()
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue  # 库 print 等噪音：分片失败以 failed 事件为准
        et = ev.get("type")
        if et == "progress":
            f = ev.get("frames", last)
            state["frames"] += max(0, f - last)
            last = f
        elif et == "log":
            emit({"type": "log", "line": ev.get("line", "")})
        elif et == "done":
            state["done"] = True
        elif et == "failed":
            state["error"] = ev.get("error", "并行分片失败")
        # started 等其余事件忽略（本进程已上报过）


def _load_onnx_engine(
    weight: Path,
    spec,
    scale: int,
    variant: str | None,
    precision: str,
    tile: int,
    warmup_hw: tuple[int, int],
    *,
    batch: int = 1,
    log=None,
) -> tuple["OnnxSrEngine", str]:
    """构建并预热 onnx 推理引擎（视频/图片任务共用）。

    含 fp16 惰性补转、TRT 组件激活与编译提示、显存不足自动减半 tile 重试。
    warmup 必须用源帧真实尺寸：DML 会话跑过小形状后拖慢真实尺寸且不可逆。
    返回 (engine, 实际使用精度)。失败向上抛，由调用方转 failed 事件。
    """
    # 惰性补转 fp16（一次），失败回退 fp32；spec.fp16=False 的模型（如 real-cugan，
    # 转换后 ShapeInference 崩）直接用 fp32 原件
    if precision == "fp16" and spec.fp16 and not weight.stem.endswith("_fp16"):
        if log:
            log({"type": "log", "line": f"生成 fp16 变体: {weight.name}"})
        weight = ensure_fp16_file(weight)
    used_precision = "fp16" if weight.stem.endswith("_fp16") else "fp32"

    def _oom(e: Exception) -> bool:
        t = str(e).lower()
        return "memory" in t or "alloc" in t or "oom" in t

    def _cuda_kernel_fault(e: Exception) -> bool:
        """CUDA 执行层内核崩溃特征：报错串带 GPU 版 ORT 的 providers/cuda 源路径
        （如 fast_divmod 断言的 Resize 缺陷）。只认精确特征，避免把普通
        CUDA 初始化/驱动问题误判成「换链能救」。"""
        t = str(e)
        return "RUNTIME_EXCEPTION" in t and "providers/cuda" in t

    # 推理后端：设置 engine=trt 走 TensorRT 链（TRT 不可用引擎层自动回退）；
    # engine=cpu 显式锁 CPUExecutionProvider（CUGAN×DML 泄漏模型的兜底通道）
    _eng = settings.load().get("engine")
    ort_device = _eng if _eng in ("trt", "cpu") else "auto"
    # CUGAN×DML 逐帧显存泄漏（实测 1080p BASIC≈8 帧、ALL≈30 帧即 0x887A0006
    # 设备摘除，全 GPU 会话连坐；540p 阈值更高但终会爆；与线程/包装/内容无关），
    # CUDA EP 实测同样挂起。仅 TRT 与显式 CPU 可用（TRT 端到端 14.9fps 验证）。
    # 证据链 BENCH.md §13。此处直接拒绝，避免用户任务跑到一半设备崩溃。
    eng_setting = settings.load().get("engine")
    if "cugan" in spec.id.lower() and eng_setting not in ("trt", "cpu"):
        raise RuntimeError(
            f"模型 {spec.id} 在 DirectML/CUDA 后端存在 GPU 显存泄漏崩溃（实测高分辨率"
            f"约 8~30 帧即设备摘除），当前设置 engine={eng_setting or 'auto'} 不可用。"
            f"请安装 TensorRT 组件并将引擎设为 TensorRT，或显式切换到 CPU 后端（较慢）。")
    if ort_device == "trt":
        if getattr(sys, "frozen", False):
            # 安装版：激活 TRT 组件（GPU 版 onnxruntime 重定向）。必须在
            # 进程内首次 import onnxruntime 之前（OnnxSrEngine.load 惰性导入）；
            # 组件缺失/不兼容返回 False → 引擎层自然回退 DML
            from sv.engines.trt_runtime import activate_component

            if activate_component():
                if log:
                    log({"type": "log", "line": "TensorRT 加速组件已加载"})
        if log:
            log({"type": "log", "line":
                 "TensorRT 引擎加载中：新模型或新分辨率首次使用需编译引擎（约 1~2 分钟），完成后可直接加载"})
    engine = None
    # 显存不足自动降档 tile 减半（最多 3 次）；CUDA 执行层内核崩溃换链重试（一次）
    for attempt in range(4):
        try:
            engine = OnnxSrEngine(
                weight, scale, io=spec.io, tile=tile, batch=batch,
                device=ort_device, validate_hw=warmup_hw)
            engine.load()
            import numpy as np
            # 预热兼显存探测。必须用源帧真实尺寸：DML 会话一旦跑过 64x64 这类
            # 小形状，后续真实尺寸的执行路径被拖慢且不可逆（实测 960x720：
            # 25.7ms -> 38.5ms/帧，+50%；先小后大也无法自愈）
            engine.process(np.zeros((warmup_hw[0], warmup_hw[1], 3), dtype=np.uint8))
            break
        except Exception as e:  # noqa: BLE001
            # CUDA/TRT 执行层的内核崩溃（非显存问题）：MxNet 导出的 Resize 节点
            # （MangaJaNai 系）在 CUDA EP 有 fast_divmod 断言缺陷，DML/CPU 正常。
            # TRT 场景换「TRT+CPU 兜底」链重试（主图仍 TRT 编译，坏节点落 CPU，
            # 速度几乎无损）；纯 CUDA 场景无 GPU 替代，退 CPU 保出片并说明。
            # 用 eng_setting 判定（cuda 设置映射的 ort_device 是 auto）；
            # 降链后 ort_device 离开初值，天然只降一次
            if (not _oom(e) and _cuda_kernel_fault(e)
                    and (ort_device == "trt" or eng_setting == "cuda")):
                if eng_setting == "trt":
                    ort_device = "trt_cpu"
                    if log:
                        log({"type": "log", "line":
                             "CUDA 执行层在个别算子上崩溃，已切换为 TensorRT+CPU 混合执行重试"})
                else:
                    ort_device = "cpu"
                    if log:
                        log({"type": "log", "line":
                             "CUDA 执行层在个别算子上崩溃，已切换为 CPU 推理重试（速度较慢）"})
                continue
            # 最后一次尝试仍失败必须 raise：带着没加载成功的 engine 继续走，
            # 后面会以更难懂的方式崩（如 provider_used AttributeError）
            if not _oom(e) or tile in (1,) or attempt == 3:
                raise
            new_tile = 256 if tile == 0 else max(64, tile // 2)
            if log:
                log({"type": "log", "line":
                     f"显存不足，分块大小调整为 {new_tile} 后重试"})
            tile = new_tile
    return engine, used_precision


def _batch_heights(images: list[dict]) -> list[int]:
    """批量图片的展示高度（EXIF 方向转正后）：只读头部，不解码像素。

    读不出的图返回时略过——坏图由主循环按张跳过并记日志，这里不重复报错。
    """
    from PIL import Image

    out: list[int] = []
    for meta in images:
        try:
            with Image.open(str(meta["in"])) as im:
                w, h = im.size
                if im.getexif().get(274, 1) in (5, 6, 7, 8):  # 横竖互换方向
                    w, h = h, w
            out.append(h)
        except Exception:  # noqa: BLE001
            pass
    return out


def _run_image_job(task: dict, params: dict, spec) -> int:
    """图片超分作业（单张或批量一个任务）：一次模型加载，逐图 解码（含 EXIF
    方向）→ 推理 → 原子落盘（.part + replace，取消/中断不留半个文件）。

    单图失败跳过并记日志（个别坏图不拖垮整批），全部失败才算任务失败。
    merge_pdf：全部图片落盘后把成功页无损封装成一份 PDF（PNG→Flate 逐像素
    一致，JPG→原样直嵌）；合并失败任务判失败——图片产物保留（清理链对图片
    任务豁免），错误信息说明这一层。
    不走分段/checkpoint（单帧无意义）；取消靠 runner 杀进程兜底，已完成
    的输出文件保留（runner._cleanup_partial 对图片任务豁免）。
    """
    import numpy as np
    from PIL import Image, ImageOps

    fmt = str(params.get("format") or "png").lower()
    if fmt not in ("png", "jpg"):
        emit({"type": "failed", "error": f"图片格式仅支持 png / jpg，当前 {fmt}"})
        return 1
    jpg_quality = int(params.get("jpg_quality", 92))
    jpg_quality = min(100, max(60, jpg_quality))

    images = params.get("images") or [
        {"in": task["input_path"], "out": task["output_path"]}]
    n = len(images)
    if n == 0:
        emit({"type": "failed", "error": "任务不含任何图片"})
        return 1

    # 首图尺寸：started 事件、自定义分辨率边界、引擎预热共用
    try:
        with Image.open(images[0]["in"]) as im:
            width, height = ImageOps.exif_transpose(im).size
    except Exception as e:  # noqa: BLE001
        emit({"type": "failed", "error": f"无法读取图片 {Path(images[0]['in']).name}: {e}"})
        return 1

    scale = int(params.get("scale") or max(spec.scale))
    if scale not in spec.scale:
        emit({"type": "failed", "error": f"模型不支持 x{scale}"})
        return 1
    target = int(params.get("target_scale") or scale)
    if not (1 <= target <= scale):
        emit({"type": "failed", "error": f"目标倍率 x{target} 无效（1 ~ x{scale}）"})
        return 1
    # 自定义目标分辨率：仍只允许"原生超分后缩小"；批量已由创建端拒绝
    tw = params.get("target_w")
    th = params.get("target_h")
    target_size = None
    if tw is not None or th is not None:
        if not (isinstance(tw, int) and isinstance(th, int)):
            emit({"type": "failed", "error": "target_w/target_h 需同时提供整数宽高"})
            return 1
        if n > 1:
            emit({"type": "failed", "error": "批量图片不支持自定义分辨率"})
            return 1
        if not (width <= tw <= width * scale and height <= th <= height * scale):
            emit({"type": "failed", "error": (
                f"目标分辨率 {tw}x{th} 超出范围（源 {width}x{height} ~ "
                f"原生上限 {width * scale}x{height * scale}）"
            )})
            return 1
        target_size = (tw, th)
    tile = int(params.get("tile") or spec.tile_hint)

    denoise = params.get("denoise")
    variant = f"denoise{int(denoise)}" if denoise is not None else None
    if variant is None:
        from sv.models.registry import auto_variant
        variant = auto_variant(spec, scale, height)  # MangaJaNai 系按源高度选权重
        if variant:
            emit({"type": "log", "line": f"按源高度 {height}p 自动选择权重档: {variant}"})
            if n > 1:
                heights = _batch_heights(images)
                tiers = {auto_variant(spec, scale, h) for h in heights} - {None}
                if len(tiers) > 1:
                    # 逐图换档 = 逐档换 engine（销毁旧 session 再建新的），DML 下
                    # 「创建后再析构」会话有原生崩溃前科（CUGAN/V3.1 实证）——
                    # 统一首图档 + 明示取舍，要逐档精确就按高度分批
                    emit({"type": "log", "line":
                          f"批量图片高度不一（{min(heights)}~{max(heights)}p），"
                          f"理想档含 {'/'.join(sorted(tiers))}，"
                          f"已统一按首图 {height}p 的 {variant} 档处理；"
                          "如需逐档精确请按高度分批创建任务"})
    try:
        from sv.models.registry import file_for_scale
        need = file_for_scale(spec, scale, variant)
        manager.ensure_files(spec, [need])
    except Exception as e:  # 下载/校验失败
        emit({"type": "failed", "error": f"模型文件不可用: {e}"})
        return 1

    emit({
        "type": "started", "total_frames": n,
        "src_w": width, "src_h": height,
        "out_w": target_size[0] if target_size else width * target,
        "out_h": target_size[1] if target_size else height * target,
        "output": images[0]["out"],
    })
    t0 = time.perf_counter()
    t_load = time.perf_counter()
    used_prec = "fp16-autocast"

    # ---- 引擎一次性加载（首图真实尺寸预热，后续图各自形状自然重配）----
    try:
        if spec.engine == "torch":
            from sv.engines.torch_engine import TorchSrEngine

            engine = TorchSrEngine(
                model_file(spec, scale), scale, io=spec.io,
                tile=tile or 512)
            engine.load()
        else:
            precision = settings.load().get("precision", "fp32")
            weight = model_file(spec, scale, precision, variant)
            engine, used_prec = _load_onnx_engine(
                weight, spec, scale, variant, precision, tile,
                (height, width), batch=1, log=emit)
    except Exception as e:  # noqa: BLE001 — worker 兜底，任何异常都要上报
        emit_failed("引擎加载失败", e)
        return 1
    load_s = time.perf_counter() - t_load

    ok = 0
    out_bytes_total = 0
    failed_names: list[str] = []
    written: list[Path] = []  # 本轮成功落盘的输出（PDF 按此清单与顺序封装）
    t_dec = t_inf = t_sav = 0.0  # 剖析口径：解码 / 推理 / 编码落盘 合计
    first_pair: tuple[np.ndarray, np.ndarray] | None = None

    for k, meta in enumerate(images):
        src_p = Path(meta["in"])
        name = src_p.name
        _t = time.perf_counter()
        try:
            with Image.open(str(src_p)) as im:
                img = ImageOps.exif_transpose(im)  # 手机竖拍按 EXIF 转正
                frame = np.asarray(img.convert("RGB"), dtype=np.uint8)
        except Exception as e:  # noqa: BLE001
            emit({"type": "log", "line": f"跳过 {name}（无法读取: {e}）"})
            failed_names.append(name)
        else:
            t_dec += time.perf_counter() - _t
            _t = time.perf_counter()
            try:
                out = engine.process(frame)  # 引擎统一 RGB 进 RGB 出
                if target_size is not None and (out.shape[1], out.shape[0]) != target_size:
                    out = np.asarray(
                        Image.fromarray(out).resize(target_size, Image.LANCZOS).convert("RGB"))
            except Exception as e:  # noqa: BLE001
                emit({"type": "log", "line": f"跳过 {name}（推理失败: {type(e).__name__}: {e}）"})
                failed_names.append(name)
            else:
                t_inf += time.perf_counter() - _t
                _t = time.perf_counter()
                dst = Path(meta["out"])
                tmp = dst.with_name(dst.name + f".{os.getpid()}.part")
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    result = Image.fromarray(out)
                    if fmt == "jpg":
                        result.save(str(tmp), "JPEG", quality=jpg_quality)
                    else:
                        result.save(str(tmp), "PNG")
                    os.replace(tmp, dst)  # 原子：取消/中断不留半个文件
                except Exception as e:  # noqa: BLE001
                    tmp.unlink(missing_ok=True)
                    emit({"type": "log", "line": f"跳过 {name}（写入失败: {e}）"})
                    failed_names.append(name)
                else:
                    t_sav += time.perf_counter() - _t
                    ok += 1
                    written.append(dst)
                    out_bytes_total += dst.stat().st_size
                    if first_pair is None:
                        first_pair = (frame, out)
        done_now = k + 1  # 进度按处理位数计（含失败），保证走满 total
        el = time.perf_counter() - t0
        emit({"type": "progress", "frames": done_now, "total": n,
              "fps": round(done_now / el, 2) if el > 0 else 0,
              "eta_sec": int((n - done_now) * el / done_now) if done_now else 0})

    if ok == 0:
        err = f"全部 {n} 张图片处理失败" + (f"（如 {failed_names[0]}）" if failed_names else "")
        emit({"type": "failed", "error": err})
        return 1

    if failed_names:
        emit({"type": "log", "line":
              f"{len(failed_names)} 张失败/跳过: {', '.join(failed_names[:10])}"
              + ("…" if len(failed_names) > 10 else "")})

    # ---- 合并输出 PDF（merge_pdf）：成功页按处理顺序无损封装 ----
    pdf_pages = 0
    if params.get("merge_pdf") and written:
        if not params.get("pdf_out"):
            emit({"type": "failed", "error": "merge_pdf 任务缺少 pdf_out 参数"})
            return 1
        pdf_out = Path(str(params["pdf_out"]))
        _t = time.perf_counter()
        try:
            from sv.pdfmerge import write_pdf

            pdf_pages = write_pdf(written, pdf_out)["pages"]
        except Exception as e:  # noqa: BLE001 — 合并失败必须显式上报，不静默
            emit({"type": "failed", "error":
                  f"图片已全部产出（{ok} 张保留在输出目录），但 PDF 合并失败: "
                  f"{type(e).__name__}: {e}"})
            return 1
        dt = time.perf_counter() - _t
        out_bytes_total += pdf_out.stat().st_size
        emit({"type": "log", "line":
              f"已无损合并 {pdf_pages} 页 → {pdf_out.name}"
              f"（{pdf_out.stat().st_size / 1048576:.1f} MB，用时 {dt:.1f}s"
              + (f"，另 {len(failed_names)} 张失败图未含" if failed_names else "")
              + "）"})

    if _prof_enabled():
        el = time.perf_counter() - t0
        _prof_write(task["id"], "\n".join([
            f"==== {time.strftime('%Y-%m-%d %H:%M:%S')} 图片超分任务 ====",
            f"模型 {spec.id} · 推理后端 {'/'.join(engine.provider_used) if hasattr(engine, 'provider_used') else 'torch'}"
            f" · 精度 {used_prec} · tile={tile} · {n}张 → {fmt.upper()}",
            f"引擎加载 {load_s:.1f}s · 解码合计 {t_dec:.2f}s · 推理合计 {t_inf:.2f}s"
            f" · 编码落盘合计 {t_sav:.2f}s",
            f"成功 {ok}/{n} 张 · 总用时 {el:.1f}s · 平均 {ok / el if el > 0 else 0:.2f} 张/秒（端到端口径）",
            *([f"PDF 合并 {pdf_pages} 页（无损封装）"] if pdf_pages else []),
        ]) + "\n\n")

    # 预览缩略图（对照页/任务卡，取第一张成功的），失败不影响主流程
    preview_dir = TEMP_DIR / "previews"
    preview_dir.mkdir(parents=True, exist_ok=True)

    def _thumb(arr: np.ndarray, path: Path) -> None:
        try:
            t = Image.fromarray(arr).convert("RGB")
            t.thumbnail((960, 960))
            t.save(str(path), quality=88)
        except Exception:  # noqa: BLE001
            pass

    assert first_pair is not None
    _thumb(first_pair[1], preview_dir / f"{task['id']}.jpg")
    _thumb(first_pair[0], preview_dir / f"{task['id']}_src.jpg")

    emit({
        "type": "done", "frames": ok,
        "elapsed": round(time.perf_counter() - t0, 1),
        "out_bytes": out_bytes_total,
        "preview": str(preview_dir / f"{task['id']}.jpg"),
        "src_preview": str(preview_dir / f"{task['id']}_src.jpg"),
    })
    return 0


def main(task_id: str, shard: int | None = None, nshards: int = 1) -> int:
    task = db.get_task(task_id)
    if task is None:
        emit({"type": "failed", "error": f"任务 {task_id} 不存在"})
        return 2

    params = task["params"]
    model_id = task["model_id"]
    input_path = Path(task["input_path"])
    output_path = Path(task["output_path"])

    try:
        spec = get_model(model_id)
    except KeyError:
        emit({"type": "failed", "error": f"未知模型 {model_id}"})
        return 1

    # 图片任务：单帧专用路径（无分段/checkpoint/补帧/音频），在视频探测前分岔
    if params.get("kind") == "image":
        return _run_image_job(task, params, spec)

    try:
        info = probe(input_path)
        validate_m0(info)
    except (UnsupportedMedia, FileNotFoundError) as e:
        emit({"type": "failed", "error": str(e)})
        return 1

    scale = int(params.get("scale") or max(spec.scale))
    if scale not in spec.scale:
        emit({"type": "failed", "error": f"模型不支持 x{scale}"})
        return 1
    target = int(params.get("target_scale") or scale)
    if not (1 <= target <= scale):
        emit({"type": "failed", "error": f"目标倍率 x{target} 无效（1 ~ x{scale}）"})
        return 1
    # 精确目标分辨率（自定义分辨率模式）：优先于整数倍率，仍只允许"原生超分后缩小"
    tw = params.get("target_w")
    th = params.get("target_h")
    if tw is not None or th is not None:
        if not (isinstance(tw, int) and isinstance(th, int)):
            emit({"type": "failed", "error": "target_w/target_h 需同时提供整数宽高"})
            return 1
        if not (info.width <= tw <= info.width * scale and info.height <= th <= info.height * scale):
            emit({"type": "failed", "error": (
                f"目标分辨率 {tw}x{th} 超出范围（源 {info.width}x{info.height} ~ "
                f"原生上限 {info.width * scale}x{info.height * scale}）"
            )})
            return 1
        target_size = (tw, th)
    else:
        target_size = None
    interp_mode = params.get("interp", "off")
    if interp_mode not in ("off", "rife2x"):
        emit({"type": "failed", "error": f"未知补帧模式 {interp_mode}"})
        return 1
    out_kind = params.get("out_kind", "video")
    if out_kind not in ("video", "png", "jpg"):
        emit({"type": "failed", "error": f"未知输出类型 {out_kind}"})
        return 1
    # 字幕保留但容器装不下图形字幕（PGS/DVB 只能进 mkv）：明确告知丢弃，
    # 不静默——用户看到"字幕开关开着"却找不到轨，会以为功能坏了
    if (out_kind == "video" and params.get("subtitle_mode", "auto") == "auto"
            and info.subtitles
            and params.get("container", "mp4") in ("mp4", "mov")
            and not all(c in TEXT_SUBS for c in info.subtitles)):
        emit({"type": "log", "line":
              "源含图形字幕（PGS/DVB 等），MP4/MOV 容器无法封装，本次不保留字幕；"
              "如需保留请将封装容器改为 MKV"})
    denoise = params.get("denoise")  # real-cugan 降噪档：0/1/2/3 → 对应变体权重
    variant = f"denoise{int(denoise)}" if denoise is not None else None
    if variant is None:
        from sv.models.registry import auto_variant
        variant = auto_variant(spec, scale, info.height)  # MangaJaNai 系按源高度选权重
        if variant:
            emit({"type": "log", "line": f"按源高度 {info.height}p 自动选择权重档: {variant}"})

    try:
        from sv.models.registry import file_for_scale
        need = file_for_scale(spec, scale, variant)  # 只下本任务用到的权重
        manager.ensure_files(spec, [need])
    except Exception as e:  # 下载/校验失败
        emit({"type": "failed", "error": f"模型文件不可用: {e}"})
        return 1

    interp = None
    if interp_mode == "rife2x":
        try:
            rife_spec = get_model("rife-v4.26")
            manager.ensure_downloaded(rife_spec)
            precision = settings.load().get("precision", "fp32")
            rife_weight = model_file(rife_spec, 1, precision)
            # spec.fp16=False（转换不可用）必须尊重——曾在此无条件强转，DML 加载
            # 崩溃被下面的 except 吞掉，用户开的补帧无声失效
            if (precision == "fp16" and rife_spec.fp16
                    and not rife_weight.stem.endswith("_fp16")):
                rife_weight = ensure_fp16_file(rife_weight)
            interp = Rife2x(rife_weight)
            interp.load()
        except Exception as e:  # noqa: BLE001 — 补帧是增强项，失败降级为不补帧
            emit({"type": "log", "line": f"补帧不可用，按无补帧继续: {e}"})
            interp = None

    out_total = info.total_frames * (2 if interp is not None else 1)
    emit({
        "type": "started",
        "total_frames": out_total,
        "src_w": info.width, "src_h": info.height,
        "out_w": target_size[0] if target_size else info.width * target,
        "out_h": target_size[1] if target_size else info.height * target,
        "output": str(output_path),
    })

    # ---- 解码器：任务参数选择 + 真实源预验证（验证失败回退软解） ----
    # 预验证必须做在分段开始前：分段中途硬解初始化失败会被当成"帧中间截断"，
    # 段落落入 checkpoint 但内容缺失，破坏断点续跑语义。硬解是性能选项，
    # 创建后环境变化（换卡/驱动更新）不值得让任务硬失败，回退 + 日志说明
    # 前置滤镜串进同一验证命令：滤镜×硬解组合兼容性必须与真实解码一致地测
    decode_vf = prefilter_chain(bool(params.get("deinterlace")),
                                bool(params.get("deband")))
    if decode_vf:
        emit({"type": "log", "line": f"画面预处理已启用：{decode_vf}"})
    decoder_param = params.get("decoder", "sw")
    decode_hwaccel = None
    if decoder_param != "sw":
        hw_dec = DECODERS.get(decoder_param)
        if hw_dec and probe_hwaccel(input_path, hw_dec, info.video_codec, vf=decode_vf):
            decode_hwaccel = hw_dec
            emit({"type": "log", "line": f"硬件解码已启用（{decoder_param.upper()}）"})
        else:
            emit({"type": "log", "line": (
                f"{decoder_param} 硬件解码不可用（编码 {info.video_codec} 不支持"
                f" 或本机驱动不可用），本次使用软件解码")})

    # 批处理仅对小分辨率有益（≤720p 实测 +6%）；大分辨率单帧已喂饱，批了反而慢 9%
    batch = int(params.get("batch") or spec.io.get("batch_hint", 1) or 1)
    if info.width * info.height > 1280 * 720:
        batch = 1
    # u8 图手术（前后处理 GPU 化，BENCH 3.8x）只支持 batch=1；batch_hint 的
    # 批量收益（+6%）远小于禁用包装的代价——非用户显式指定且无分块时强制单帧。
    # 实测 960x720：batch4 无包装 104ms/帧 vs batch1 包装 26ms/帧（4x）。
    if batch > 1 and not params.get("batch") and not (params.get("tile") or spec.tile_hint):
        batch = 1

    t0 = time.perf_counter()
    prof = _prof_enabled()
    precision = settings.load().get("precision", "fp32")
    if spec.engine == "torch":
        # ---- torch 路径：分块管线 + checkpoint 续跑（PyTorch CUDA 环境）----
        from sv.engines.torch_engine import TorchSrEngine
        from sv.pipeline.chunked import ChunkedPipeline

        t_load = time.perf_counter()
        engine = TorchSrEngine(
            model_file(spec, scale), scale, io=spec.io,
            tile=int(params.get("tile") or 512),
        )
        engine.load()
        if prof:
            _prof_write_video_header(task_id, {
                "model": spec.id, "provider": str(getattr(engine, "provider_used", "?")),
                "precision": "fp16-autocast", "u8": False,
                "engine_setting": "cuda(torch)", "tile": int(params.get("tile") or 512),
                "batch": 1, "interp": interp_mode,
                "decoder": decoder_param, "prefilter": decode_vf,
                "src_w": info.width, "src_h": info.height, "fps": info.fps,
                "frames": info.total_frames,
                "target": (f"{target_size[0]}x{target_size[1]}" if target_size
                           else f"x{target}（{info.width * target}x{info.height * target}）"),
                "load_s": time.perf_counter() - t_load,
                "seg": int(params.get("chunk") or 32), "n_segs": 0,
                "parallel": False,
                "resumed": (TEMP_DIR / "chunked" / task_id).exists(),
            })
        emit({"type": "loaded", "provider": engine.provider_used, "precision": "fp16-autocast"})

        preview_dir = TEMP_DIR / "previews"
        preview_dir.mkdir(parents=True, exist_ok=True)

        def on_progress(frames, total, fps, eta):
            emit({
                "type": "progress", "frames": frames, "total": total,
                "fps": round(fps, 2), "eta_sec": int(eta),
            })

        pipeline = ChunkedPipeline(
            info, output_path, engine,
            _encode_opts(params, out_kind),
            task_id=task_id, chunk=int(params.get("chunk") or 32),
            progress_cb=on_progress,
            preview_path=preview_dir / f"{task_id}.jpg",
            src_preview_path=preview_dir / f"{task_id}_src.jpg",
            target_size=target_size,
            decode_hwaccel=decode_hwaccel,
            decode_vf=decode_vf,
        )
        try:
            stats = asyncio.run(pipeline.run())
        except TaskCanceled:
            emit({"type": "canceled"})
            return 3
        except PipelineError as e:
            emit({"type": "failed", "error": str(e)})
            return 1
        except Exception as e:  # noqa: BLE001
            emit({"type": "failed", "error": f"{type(e).__name__}: {e}"})
            return 1
        if prof:
            _fps = stats.frames / stats.elapsed_s if stats.elapsed_s > 0 else 0
            _prof_write(task_id, f"==== 运行结束 {stats.frames}帧 · {stats.elapsed_s:.1f}s"
                        f" · 平均 {_fps:.2f} fps（端到端口径） ====\n\n")
        emit({
            "type": "done", "frames": stats.frames,
            "elapsed": round(stats.elapsed_s, 1),
            "out_bytes": stats.out_bytes,
            "preview": str(preview_dir / f"{task_id}.jpg"),
            "src_preview": str(preview_dir / f"{task_id}_src.jpg"),
        })
        return 0

    try:
        weight = model_file(spec, scale, precision, variant)
        tile = int(params.get("tile") or spec.tile_hint)
        t_load = time.perf_counter()
        engine, used_precision = _load_onnx_engine(
            weight, spec, scale, variant, precision, tile,
            (info.height, info.width), batch=batch, log=emit)
    except Exception as e:  # noqa: BLE001 — 与图片路径同：失败带堆栈落 sidecar.log
        emit_failed("引擎加载失败", e)
        return 1
    load_s = time.perf_counter() - t_load
    emit({"type": "loaded", "provider": engine.provider_used, "precision": used_precision})

    preview_dir = TEMP_DIR / "previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    preview_path = preview_dir / f"{task_id}.jpg"
    src_preview_path = preview_dir / f"{task_id}_src.jpg"

    # ---- 双路并行判定（协调者身份才分叉；分片子进程只跑自己的段防递归）----
    # 条件：开关开 + onnx 模型 + 无补帧 + 视频输出 + 至少 8 段——每路要各自
    # 加载一遍推理引擎（TRT 反序列化 ~5-8s），段太少时双路省的计算抵不过
    # 双倍的引擎加载（91 帧小视频实测 51fps → 29fps 反而变慢）
    seg_est = min(600, max(60, info.total_frames // 8))
    n_segs = -(-info.total_frames // seg_est)  # ceil(total/seg)
    parallel = (
        shard is None
        and settings.load().get("parallel_streams") is True
        and spec.engine == "onnx"
        and interp is None
        and out_kind == "video"
        and n_segs >= 8
    )
    child = None
    child_state: dict = {"frames": 0, "done": False, "error": None}

    if prof and shard is None:
        _prof_write_video_header(task_id, {
            "model": spec.id,
            "provider": "/".join(engine.provider_used or []) or "?",
            "precision": used_precision,
            "u8": bool(getattr(engine, "u8_wrapped", False)),
            "engine_setting": settings.load().get("engine", "auto"),
            "tile": tile, "batch": batch, "interp": interp_mode,
            "decoder": decoder_param, "prefilter": decode_vf,
            "src_w": info.width, "src_h": info.height, "fps": info.fps,
            "frames": info.total_frames,
            "target": (f"{target_size[0]}x{target_size[1]}" if target_size
                       else f"x{target}（{info.width * target}x{info.height * target}）"),
            "load_s": load_s, "seg": seg_est, "n_segs": n_segs,
            "parallel": parallel,
            "resumed": (TEMP_DIR / "segmented" / task_id / "checkpoint.json").exists(),
        })

    if parallel:
        child = _spawn_shard_child(task_id)
        threading.Thread(target=_read_child_events, args=(child, child_state),
                         daemon=True).start()
        emit({"type": "log", "line": "双路并行已启用：两个进程分段同时处理"})

        own = {"last": 0, "done": 0}  # 本路帧号 → 增量累计完成数

        def on_progress(frames, total, fps, eta):
            # 两路各自增量累计之和 = 真实总进度（全局帧号直接相加会翻倍）
            own["done"] += max(0, frames - own["last"])
            own["last"] = frames
            f = own["done"] + child_state["frames"]
            el = time.perf_counter() - t0
            fps_all = f / el if el > 0 else 0.0
            emit({"type": "progress", "frames": f, "total": total,
                  "fps": round(fps_all, 2),
                  "eta_sec": int((total - f) / fps_all) if fps_all > 0 else 0})
    else:
        def on_progress(frames, total, fps, eta):
            emit({
                "type": "progress", "frames": frames, "total": total,
                "fps": round(fps, 2), "eta_sec": int(eta),
            })

    # 分段管线：取消/崩溃后"继续"可跳过已完成段（checkpoint 在 .tmp/segmented/<task_id>）
    # 协调者=第 0 路（其余段由子进程跑），分片子进程只跑自己的段；两者都不做
    # 收尾合成——双路都完成后由协调者统一 concat + 清理
    my_shard: int | None = shard
    my_nshards = nshards
    if parallel:
        my_shard, my_nshards = 0, 2
    sharded = my_shard is not None
    pipeline = SegmentedPipeline(
        info, output_path, engine,
        _encode_opts(params, out_kind),
        task_id=task_id,
        progress_cb=on_progress,
        preview_path=None if sharded else preview_path,
        src_preview_path=None if sharded else src_preview_path,
        target_scale=None if target_size else target,
        target_size=target_size,
        interp=interp,
        shard=my_shard, nshards=my_nshards,
        # 剖析模式：成功后不删工作目录，等 _prof_collect 把分段耗时并入日志再清
        cleanup=(not sharded) and not prof,
        decode_hwaccel=decode_hwaccel,
        decode_vf=decode_vf,
    )
    work_dir = TEMP_DIR / "segmented" / task_id
    try:
        stats = asyncio.run(pipeline.run())
        if parallel:
            # 双路收尾：等子进程退出 + 校验成败 + 合成 + 清理
            if child_state["error"]:
                raise PipelineError(f"并行分片失败: {child_state['error']}")
            try:
                child.wait(timeout=600)
            except subprocess.TimeoutExpired:
                raise PipelineError("并行分片超时未退出")
            if child.returncode != 0 or not child_state["done"]:
                raise PipelineError(f"并行分片异常退出 (rc={child.returncode})")
            from sv.pipeline.segmented import concat_segments

            concat_segments(work_dir, info, _encode_opts(params, out_kind),
                            output_path)
            if prof:
                _prof_collect(task_id, work_dir)
            # 真实片尾修正（probe 高估被 EOF 证伪）：done 帧数/进度按实际值；
            # 先读标记再清目录
            real_in = read_eof_marker(work_dir)
            real_out = (real_in * (2 if interp is not None else 1)
                        if real_in is not None else out_total)
            shutil.rmtree(work_dir, ignore_errors=True)
            stats = RunStats(
                frames=real_out,
                elapsed_s=time.perf_counter() - t0,
                fps=real_out / max(time.perf_counter() - t0, 1e-6),
                out_path=output_path,
                out_bytes=output_path.stat().st_size if output_path.exists() else 0,
            )
    except TaskCanceled:
        emit({"type": "canceled"})
        return 3
    except PipelineError as e:
        emit({"type": "failed", "error": str(e)})
        return 1
    except Exception as e:  # noqa: BLE001 — worker 兜底，任何异常都要上报
        emit({"type": "failed", "error": f"{type(e).__name__}: {e}"})
        return 1
    finally:
        if child is not None and child.poll() is None:
            from sv.utils.process import kill_tree

            kill_tree(child.pid)  # 本路失败/取消时带走分片子进程

    if prof and shard is None:
        if not parallel:
            # 单路成功收尾：并入分段耗时明细后清掉工作目录（剖析模式 pipeline 未自清）
            _prof_collect(task_id, work_dir)
            shutil.rmtree(work_dir, ignore_errors=True)
        fps_e2e = stats.frames / stats.elapsed_s if stats.elapsed_s > 0 else 0
        _prof_write(task_id,
                    f"==== 运行结束 {stats.frames}帧 · {stats.elapsed_s:.1f}s"
                    f" · 平均 {fps_e2e:.2f} fps（端到端口径：含引擎加载与合成） ====\n\n")

    emit({
        "type": "done", "frames": stats.frames,
        "total_frames": stats.frames,  # 真实片尾修正后回填 DB（旧事件无此键按估算）
        "elapsed": round(stats.elapsed_s, 1),
        "out_bytes": stats.out_bytes, "preview": str(preview_path),
        "src_preview": str(src_preview_path),
    })
    return 0


if __name__ == "__main__":
    import argparse

    # Windows 管道默认走系统 locale：中文系统 GBK 尚可、英文系统 cp1252 直接
    # UnicodeEncodeError（一行事件都吐不出）。runner 真实链路带 PYTHONIOENCODING；
    # 直跑（-m / 调试 / 测试）由此兜底，与 cli.main 同款
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    ap = argparse.ArgumentParser(prog="worker")
    ap.add_argument("task_id")
    ap.add_argument("--shard", nargs=2, type=int, metavar=("I", "N"),
                    default=None, help="[内部] 双路并行：本进程跑第 I 路(0基)/共 N 路")
    args = ap.parse_args()
    sh = args.shard[0] if args.shard else None
    ns = args.shard[1] if args.shard else 1
    sys.exit(main(args.task_id, sh, ns))
