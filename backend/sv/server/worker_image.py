"""Worker 图片作业：单张/批量超分，一次模型加载逐图处理。"""
from __future__ import annotations

import os
import time
from pathlib import Path

from sv.models import manager
from sv.models.registry import model_file
from sv.paths import TEMP_DIR
from sv.server import settings
from sv.server.worker_common import _prof_enabled, _prof_write, emit, emit_failed
from sv.server.worker_engine import _load_onnx_engine


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

