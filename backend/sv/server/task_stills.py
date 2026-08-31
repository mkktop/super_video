"""任务结果对比页的多帧静帧：源与任务输出按同时间戳抽 N 对 PNG，懒构建缓存。

与模型对比（compare.py）的静帧是两回事：那边是作业管线顺带产出的样本
（创建时快照样本数）；这里任务的输出早已落盘，对比页打开（查状态）时才
按当前设置构建，缓存在 DATA_ROOT/.tmp/task_stills/<task_id>/，输入/输出
文件变化或样本数设置改动即整目录重建（产物小，重建比增量补便宜）。

形态路由（沿用主任务的三种输出形态）：
- 视频→视频（out_kind=video）：时长均分 + 源侧避黑选时间戳，源/输出同
  时间戳 -ss 抽帧（帧级对齐；interp 补帧不改变时间轴，同样成立）
- 视频→图片序列（out_kind=png/jpg，输出是帧目录）：按输出帧序号均分取帧，
  源按比例换算时间戳抽——两端都是等分采样，对补帧倍率免疫
- 图片任务：无多帧概念，unsupported（对比页沿用单对预览图）

分辨率纪律：PNG 无损（对比页放大镜按原图像素放大，压缩伪影会污染判读）；
仅长边超 3840 时等比 lanczos 缩到 3840——8K 任务的原始帧单张 50MB+，
缓存会失控，而 4K 级屏显 + 3x 放大采样不到更细的像素。
"""
from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path

from ..paths import TEMP_DIR
from .compare import _extract_still, _ffmpeg, _pick_still_times, _still_count_setting

STILLS_ROOT = TEMP_DIR / "task_stills"
MAX_LONG_EDGE = 3840  # 超过则等比缩取样帧（8K 任务防缓存失控）
DARK_MEAN = 20.0  # 与 compare._frame_dark 同口径：灰度均值低于此视为黑场
_SEQ_EXTS = {".png", ".jpg", ".jpeg"}

_BUILDING: set[str] = set()
_FAILED: dict[str, str] = {}
_FAIL_SIG: dict[str, tuple] = {}  # 失败时的失效签名：签名变了才允许重试，防打转


def _kind(task: dict) -> str:
    """任务输出形态：video 视频文件 / seq 图片序列目录 / image 图片任务。"""
    params = task.get("params") or {}
    if params.get("images") or params.get("out_kind") not in ("video", "png", "jpg"):
        return "image"  # 图片任务无 out_kind 键；兜底按不支持处理
    if params.get("out_kind") in ("png", "jpg") or Path(task["output_path"]).is_dir():
        return "seq"
    return "video"


def _stat(p: Path, is_dir: bool = False) -> tuple | None:
    """失效签名用的高精度指纹：文件 (mtime_ns, size)；序列目录 (mtime_ns, 帧数)。"""
    try:
        if is_dir:
            return (p.stat().st_mtime_ns, len(_seq_frames(p)))
        st = p.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _seq_frames(d: Path) -> list[Path]:
    """序列输出目录里的帧文件（000001.png 起零填充编号，字典序=帧序）。"""
    try:
        return sorted(p for p in d.iterdir() if p.suffix.lower() in _SEQ_EXTS)
    except OSError:
        return []


def _signature(task: dict, kind: str, n: int) -> tuple:
    src, out = Path(task["input_path"]), Path(task["output_path"])
    return (kind, n, _stat(src), _stat(out, is_dir=(kind == "seq")))


def _sig_json(sig: tuple) -> list:
    """签名的 JSON 口径（元组→列表）：落盘与读回比较用同一形状。"""
    return [list(v) if isinstance(v, tuple) else v for v in sig]


def _load_meta(d: Path) -> dict | None:
    try:
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        return meta if isinstance(meta.get("count"), int) else None
    except (OSError, ValueError):
        return None


def _files_complete(d: Path, n: int) -> bool:
    return all((d / f"{side}_{i}.png").is_file()
               for i in range(n) for side in ("src", "out"))


def _img_dark(p: Path) -> bool:
    """已有图片文件判黑（序列帧不用 ffmpeg 走管线，直接 PIL 读）。"""
    from PIL import Image, ImageStat

    try:
        im = Image.open(p).convert("L")
        return ImageStat.Stat(im).mean[0] < DARK_MEAN
    except Exception:  # noqa: BLE001 — 读不了的帧当可用，别因一张坏图废掉整组
        return False


def _pick_seq_frames(frames: list[Path], n: int) -> list[int]:
    """序列侧选帧：均分 n 段、段中点为锚、段内 ±4/±8 帧避黑（钳在段内）。"""
    total = len(frames)
    picks: list[int] = []
    for i in range(n):
        lo, hi = total * i / n, total * (i + 1) / n
        anchor = int((lo + hi) / 2)
        idx = min(anchor, max(int(hi) - 1, int(lo)))
        for off in (0, -4, 4, -8, 8):
            cand = min(max(anchor + off, int(lo)), max(int(hi) - 1, int(lo)))
            if not _img_dark(frames[cand]):
                idx = cand
                break
        picks.append(idx)
    return picks


def _extract_capped(video: Path, out_png: Path, t: float) -> None:
    """抽帧并遵守长边上限：超 3840 的（8K 任务）等比 lanczos 缩，否则原样。"""
    from ..pipeline.probe import probe

    info = probe(video)
    long_edge = max(info.width, info.height)
    if long_edge <= MAX_LONG_EDGE:
        _extract_still(video, out_png, t)
        return
    r = MAX_LONG_EDGE / long_edge
    w, h = int(info.width * r) // 2 * 2, int(info.height * r) // 2 * 2
    _ffmpeg(["-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1",
             "-vf", f"scale={w}:{h}:flags=lanczos", str(out_png)])


def _build(task_id: str, src: Path, out: Path, kind: str, n: int, sig: tuple) -> None:
    """整目录重建（先清后建，meta.json 最后落盘=完整性标记，半成品视为无效）。"""
    from ..pipeline.probe import probe
    from PIL import Image

    d = STILLS_ROOT / task_id
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    dur = max(probe(src).duration_s, 0.1)
    if kind == "video":
        for i, t in enumerate(_pick_still_times(src, dur, n)):
            _extract_capped(src, d / f"src_{i}.png", t)
            _extract_capped(out, d / f"out_{i}.png", t)
    else:  # seq：输出帧是现成图片，转无损 PNG 统一口径；源按比例时间戳抽
        frames = _seq_frames(out)
        if len(frames) < n:
            raise ValueError(f"序列输出只有 {len(frames)} 帧，不足 {n} 张样本")
        for i, idx in enumerate(_pick_seq_frames(frames, n)):
            t = max(0.0, min(dur * idx / len(frames), dur - 0.05))
            _extract_capped(src, d / f"src_{i}.png", t)
            Image.open(frames[idx]).convert("RGB").save(d / f"out_{i}.png", "PNG")
    meta = {"count": n, "sig": _sig_json(sig), "built_at": time.time()}
    (d / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def status(task: dict) -> dict:
    """对比页轮询入口：ready 直接用；缓存失效/未建则后台起线程构建（幂等）。

    返回 {status: ready|building|failed|unsupported, count, built_at, error}；
    unsupported=该任务没有多帧静帧概念（图片任务/未完成/文件已不存在/样本数 1），
    前端回落到既有的单对预览图。
    """
    tid = task["id"]
    if task.get("status") != "done" or _kind(task) == "image":
        return {"status": "unsupported", "count": 0, "built_at": None, "error": None}
    n = _still_count_setting()
    if n < 2:
        return {"status": "unsupported", "count": 1, "built_at": None, "error": None}
    kind = _kind(task)
    src, out = Path(task["input_path"]), Path(task["output_path"])
    if not src.exists() or (kind == "seq" and not out.is_dir()) \
            or (kind == "video" and not out.is_file()):
        return {"status": "unsupported", "count": 0, "built_at": None, "error": None}
    sig = _signature(task, kind, n)
    d = STILLS_ROOT / tid
    meta = _load_meta(d)
    if meta and meta.get("sig") == _sig_json(sig) \
            and _files_complete(d, meta["count"]):
        return {"status": "ready", "count": meta["count"],
                "built_at": meta.get("built_at"), "error": None}
    if tid in _BUILDING:
        return {"status": "building", "count": n, "built_at": None, "error": None}
    if _FAIL_SIG.get(tid) == sig:  # 同一签名已失败过：别再打转，等输入/设置变化
        return {"status": "failed", "count": 0, "built_at": None,
                "error": _FAILED.get(tid)}
    _BUILDING.add(tid)

    def run() -> None:
        try:
            _build(tid, src, out, kind, n, sig)
            _FAILED.pop(tid, None)
            _FAIL_SIG.pop(tid, None)
        except Exception as e:  # noqa: BLE001 — 抽帧失败降级为 failed 态，页面回落预览
            _FAILED[tid] = f"{type(e).__name__}: {e}"
            _FAIL_SIG[tid] = sig
        finally:
            _BUILDING.discard(tid)

    threading.Thread(target=run, daemon=True, name=f"stills-{tid[:8]}").start()
    return {"status": "building", "count": n, "built_at": None, "error": None}


def asset_path(task_id: str, i: int, src: bool) -> Path | None:
    """静帧文件解析（白名单索引，不存在/越界返回 None → 路由转 404）。"""
    meta = _load_meta(STILLS_ROOT / task_id)
    if meta is None or not 0 <= i < meta["count"]:
        return None
    p = STILLS_ROOT / task_id / f"{'src' if src else 'out'}_{i}.png"
    return p if p.is_file() else None


# ---- 缓存管理（设置页「对比」缓存卡的统计/清理覆盖到这里）----


def cache_stats() -> dict:
    """磁盘上的任务静帧目录数与总占用（并入对比缓存卡展示）。"""
    tasks, total = 0, 0
    if STILLS_ROOT.is_dir():
        for d in STILLS_ROOT.iterdir():
            if d.is_dir():
                tasks += 1
                for f in d.iterdir():
                    if f.is_file():
                        try:
                            total += f.stat().st_size
                        except OSError:
                            pass
    return {"jobs": tasks, "bytes": total}


def active() -> bool:
    return bool(_BUILDING)


def clear(task_id: str) -> None:
    """单任务清理（任务删除的 purge 链路调用；构建中概率极低，rmtree 容错）。"""
    shutil.rmtree(STILLS_ROOT / task_id, ignore_errors=True)
    _FAILED.pop(task_id, None)
    _FAIL_SIG.pop(task_id, None)


def clear_all() -> dict:
    """清空全部任务静帧缓存；有构建中的目录时抛 ValueError（路由转 409）。"""
    if active():
        raise ValueError("有任务的对比静帧正在抽取，请稍后再清理")
    freed, tasks = 0, 0
    if STILLS_ROOT.is_dir():
        for d in list(STILLS_ROOT.iterdir()):
            if not d.is_dir():
                continue
            for f in d.iterdir():
                if f.is_file():
                    try:
                        freed += f.stat().st_size
                    except OSError:
                        pass
            tasks += 1
            shutil.rmtree(d, ignore_errors=True)
    _FAILED.clear()
    _FAIL_SIG.clear()
    return {"removed_jobs": tasks, "freed_bytes": freed}


def sweep_orphans(max_age_s: float = 3600.0) -> None:
    """启动清扫：库中已无对应任务的静帧目录（删库行时 purge 失败的漏网）。
    与 segmented/chunked 同规则：只清修改时间超过 max_age_s 的，避免误删
    其他 sidecar 实例（测试/开发）刚建的目录。"""
    from . import db

    try:
        ids = db.all_task_ids()
        deadline = time.time() - max_age_s
        dirs = list(STILLS_ROOT.iterdir()) if STILLS_ROOT.is_dir() else []
    except OSError:
        return
    for d in dirs:
        try:
            if d.is_dir() and d.name not in ids and d.stat().st_mtime < deadline:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            continue
