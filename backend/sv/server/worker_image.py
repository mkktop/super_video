"""Worker 图片作业：单张/批量超分，一次模型加载逐图处理。"""
from __future__ import annotations

import gc
import os
import time
from pathlib import Path

from sv.models import manager
from sv.models.registry import model_file
from sv.paths import TEMP_DIR
from sv.server import settings
from sv.server.worker_common import _prof_enabled, _prof_write, emit, emit_failed
from sv.server.worker_engine import _load_onnx_engine, _release_slot


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

    漫画超分（params.kind="manga"）现阶段复用本管线；漫画专属处理
    （去网点/双页拆合/灰度保持等）后续按 kind 在此分岔。

    单图失败跳过并记日志（个别坏图不拖垮整批），全部失败才算任务失败。
    merge_pdf：全部图片落盘后把成功页无损封装成一份 PDF（PNG→Flate 逐像素
    一致，JPG→原样直嵌）；合并失败任务判失败——图片产物保留（清理链对图片
    任务豁免），错误信息说明这一层。
    不走分段/checkpoint（单帧无意义）；取消靠 runner 杀进程兜底，已完成
    的输出文件保留（runner._cleanup_partial 对图片任务豁免）。

    混装双模型执行模式（params.mix_pass，仅 model_id_color 存在时有意义）：
    coexist=缺省并存——两引擎同时驻留，分派只换对象（无会话重建风险）；
    split=分趟省显存——先按原页序跑完黑白趟，释放主引擎后再建彩模跑
    彩色趟，显存峰值≈单个模型。代价：趟间销毁重建引擎（几秒 + DML 下
    析构偶发不稳，自愈降链兜底）、进度按「先黑白后彩色」推进。两种模式
    下输出与 PDF 均按原页序封装。
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
    # 混装双模型：彩页模型（创建期已识别每页 lane；worker 端双引擎按页分派）
    model_id_color = str(params.get("model_id_color") or "").strip()
    color_spec = None
    if model_id_color:
        if model_id_color == task.get("model_id"):
            emit({"type": "failed", "error": "彩色页模型与主模型相同，无需混装"})
            return 1
        try:
            from sv.models.registry import get_model

            color_spec = get_model(model_id_color)
        except KeyError:
            emit({"type": "failed", "error": f"未知的彩色页模型 {model_id_color}"})
            return 1
        if scale not in color_spec.scale:
            emit({"type": "failed",
                  "error": f"彩色页模型 {color_spec.id} 不支持 x{scale}（两模型需共同支持同一倍率）"})
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

    def _lane_images(lane: str) -> list[dict]:
        """按分派车道过滤图片清单（无彩模时全部归 bw）。"""
        if color_spec is None:
            return images if lane == "bw" else []
        want_color = lane == "color"
        return [m for m in images
                if (m.get("lane") == "color") == want_color]

    def _first_hw(lane: str) -> tuple[int, int]:
        """该车道首图的 EXIF 转正宽高（auto_variant 选档与引擎预热基准）。"""
        for m in _lane_images(lane):
            with Image.open(str(m["in"])) as im:
                im = ImageOps.exif_transpose(im)
                return im.size
        return width, height  # 空车道（理论不可达）：回落任务首图

    if variant is None:
        from sv.models.registry import auto_variant

        if color_spec is None:
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
        else:
            # 混装：各车道独立选档（黑白档按首张黑白页高度、彩模档按首张彩页高度）。
            # 同理只在各自车道内统一档——引擎跨档切换 = session 销毁重建，有崩溃前科
            hw_bw = _first_hw("bw")
            variant = auto_variant(spec, scale, hw_bw[1])
            if variant:
                emit({"type": "log", "line":
                      f"黑白页按首张 {hw_bw[1]}p 选择权重档: {variant}"})
    variant_color = variant if denoise is not None else None  # denoise 档两模型同用
    if color_spec is not None and denoise is None:
        from sv.models.registry import auto_variant

        hw_color = _first_hw("color")
        variant_color = auto_variant(color_spec, scale, hw_color[1])
        if variant_color:
            emit({"type": "log", "line":
                  f"彩色页按首张 {hw_color[1]}p 选择权重档: {variant_color}"})
    try:
        from sv.models.registry import file_for_scale

        need = file_for_scale(spec, scale, variant)  # 只下本任务用到的权重
        manager.ensure_files(spec, [need])
        if color_spec is not None:
            need_color = file_for_scale(color_spec, scale, variant_color)
            manager.ensure_files(color_spec, [need_color])
    except Exception as e:  # 下载/校验失败
        emit({"type": "failed", "error": f"模型文件不可用: {e}"})
        return 1

    split = color_spec is not None and str(params.get("mix_pass") or "") == "split"
    if color_spec is not None:
        n_color = sum(1 for m in images if m.get("lane") == "color")
        emit({"type": "log", "line":
              f"混装分派：识别出彩色 {n_color} 页 / 黑白 {n - n_color} 页，"
              f"分别走 {color_spec.id} / {spec.id}（未出现的车道不加载引擎）"
              + ("；分趟模式：先黑白后彩色，显存峰值≈单个模型" if split else
                 "；两引擎同时驻留，显存约为两者之和")})

    emit({
        "type": "started", "total_frames": n,
        "src_w": width, "src_h": height,
        "out_w": target_size[0] if target_size else width * target,
        "out_h": target_size[1] if target_size else height * target,
        "output": images[0]["out"],
    })
    t0 = time.perf_counter()
    load_s_total = 0.0
    used_prec = "fp16-autocast"

    # ---- 引擎加载（车道懒加载：首张该车道图片时构建；缺省并存——双引擎同时
    # 驻留、分派只换对象调用，避开 session 销毁重建在 DML 下的崩溃前科；分趟
    # 模式 split 改为黑白趟末释放主引擎再建彩模，显存峰值≈单个模型）----
    def _build_engine(spec_, variant_, warm_hw, slot: str):
        if spec_.engine == "torch":
            from sv.engines.torch_engine import TorchSrEngine

            eng = TorchSrEngine(
                model_file(spec_, scale), scale, io=spec_.io,
                tile=tile or 512)
            eng.load()
            return eng, "fp16-autocast"
        precision = settings.load().get("precision", "fp32")
        weight = model_file(spec_, scale, precision, variant_)
        return _load_onnx_engine(
            weight, spec_, scale, variant_, precision, tile,
            warm_hw, batch=1, log=emit, slot=slot)

    engines: dict[str, object] = {}
    precisions: dict[str, str] = {}
    # provider 在构建时记下（分趟趟末引擎已释放，剖析仍要能列出两车道后端）
    lane_providers: dict[str, str] = {}

    def _engine_for(lane: str):
        eng = engines.get(lane)
        if eng is not None:
            return eng
        nonlocal load_s_total
        spec_ = color_spec if lane == "color" else spec
        variant_ = variant_color if lane == "color" else variant
        warm_hw = _first_hw(lane)
        _tl = time.perf_counter()
        built, prec = _build_engine(spec_, variant_, warm_hw,
                                    "color" if lane == "color" else "main")
        load_s_total += time.perf_counter() - _tl
        engines[lane] = built
        precisions[lane] = prec
        lane_providers[lane] = ("/".join(built.provider_used)
                                if hasattr(built, "provider_used") else "torch")
        return built

    ok = 0
    done_count = 0
    out_bytes_total = 0
    failed_names: list[str] = []
    written_idx: dict[int, Path] = {}  # 页序 k → 落盘输出（PDF 按原页序封装）
    t_dec = t_inf = t_sav = 0.0  # 剖析口径：解码 / 推理 / 编码落盘 合计
    first_pair: tuple[np.ndarray, np.ndarray] | None = None
    first_pair_k = 1 << 60  # 预览取原页序最前的成功页（分趟下处理序≠页序）

    def _process_page(k: int, meta: dict, engine) -> None:
        nonlocal ok, done_count, out_bytes_total, t_dec, t_inf, t_sav
        nonlocal first_pair, first_pair_k
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
                    written_idx[k] = dst
                    out_bytes_total += dst.stat().st_size
                    if k < first_pair_k:
                        first_pair_k = k
                        first_pair = (frame, out)
        done_count += 1  # 进度按处理位数计（含失败），保证走满 total
        el = time.perf_counter() - t0
        emit({"type": "progress", "frames": done_count, "total": n,
              "fps": round(done_count / el, 2) if el > 0 else 0,
              "eta_sec": int((n - done_count) * el / done_count) if done_count else 0})

    def _run_lane(lane: str, order: list[int]) -> None:
        for k in order:
            _process_page(k, images[k], _engine_for(lane))

    try:
        if split:
            # 分趟：先黑白趟（原页序）→ 释放主引擎 → 彩色趟。释放必须发生在
            # 彩模构建之前（先放再建——建新再析构旧有崩溃前科）；纯黑白/全彩
            # 本只跑存在的趟，不空建引擎
            bw_order = [k for k, m in enumerate(images) if m.get("lane") != "color"]
            color_order = [k for k, m in enumerate(images) if m.get("lane") == "color"]
            if bw_order:
                _run_lane("bw", bw_order)
                if color_order:
                    engines.pop("bw", None)  # 丢引用+清槽位+gc，session 析构才归还显存
                    _release_slot("main")
                    gc.collect()
                    emit({"type": "log", "line":
                          f"黑白趟完成（{len(bw_order)} 页），已释放黑白引擎显存，"
                          f"开始彩色趟（{len(color_order)} 页，重建引擎需几秒）"})
            _run_lane("color", color_order)
        else:
            # 并存：各车道按需预建（纯黑白本不建彩模、全彩本不建主引擎）；
            # 构建失败=该车道全灭，走主失败路径显式报错，好过循环里逐页重试跳过
            if color_spec is None or any(m.get("lane") != "color" for m in images):
                _engine_for("bw")
            if color_spec is not None and any(m.get("lane") == "color" for m in images):
                _engine_for("color")
            for k, meta in enumerate(images):
                _process_page(k, meta, _engine_for(
                    "color" if color_spec is not None and meta.get("lane") == "color"
                    else "bw"))
    except Exception as e:  # noqa: BLE001 — worker 兜底，任何异常都要上报
        stage = ("彩色趟引擎重建失败（黑白页产物已保留在输出目录）"
                 if split and done_count else "引擎加载失败")
        emit_failed(stage, e)
        return 1
    used_prec = precisions.get("bw", used_prec)
    load_s = load_s_total
    written = [written_idx[k] for k in sorted(written_idx)]

    if ok == 0:
        err = f"全部 {n} 张图片处理失败" + (f"（如 {failed_names[0]}）" if failed_names else "")
        emit({"type": "failed", "error": err})
        return 1

    if failed_names:
        emit({"type": "log", "line":
              f"{len(failed_names)} 张失败/跳过: {', '.join(failed_names[:10])}"
              + ("…" if len(failed_names) > 10 else "")})

    # ---- 合并输出 PDF（merge_pdf）：成功页按原页序无损封装（分趟下处理序≠页序） ----
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

        if color_spec is not None:
            model_line = (f"模型 {spec.id}(黑白{sum(1 for m in images if m.get('lane') != 'color')})"
                          f" + {color_spec.id}(彩色{sum(1 for m in images if m.get('lane') == 'color')})")
            backend_line = " · ".join(f"{ln}:{p}" for ln, p in lane_providers.items())
            prec_line = " · ".join(f"{ln}:{p}" for ln, p in precisions.items())
        else:
            model_line = f"模型 {spec.id}"
            backend_line = lane_providers.get("bw", "torch")
            prec_line = used_prec
        _prof_write(task["id"], "\n".join([
            f"==== {time.strftime('%Y-%m-%d %H:%M:%S')} 图片超分任务 ====",
            f"{model_line} · 推理后端 {backend_line}"
            f" · 精度 {prec_line} · tile={tile} · {n}张 → {fmt.upper()}",
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

