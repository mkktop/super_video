"""CPU watermark fill and local repair, with independent batch progress."""
from __future__ import annotations

import base64
import io
import math
import threading
import time
import uuid
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from PIL import Image, ImageOps
from pydantic import BaseModel, Field

from ..consts import _IMAGE_EXTS
from ...watermark_match import MatchSkipped, WatermarkTemplate, locate, make_template
from ...watermark_repair import Removal, erase

router = APIRouter(prefix="/api/watermark", tags=["watermark"])


class WhiteMask(BaseModel):
    unit: Literal["px", "percent"] = "px"
    width: float = Field(default=175, gt=0, le=100000, allow_inf_nan=False)
    height: float = Field(default=75, gt=0, le=100000, allow_inf_nan=False)
    right: float = Field(default=0, ge=0, le=100000, allow_inf_nan=False)
    bottom: float = Field(default=0, ge=0, le=100000, allow_inf_nan=False)

    def box(self, size: tuple[int, int]) -> tuple[int, int, int, int]:
        w, h = size
        sx, sy = (w / 100, h / 100) if self.unit == "percent" else (1, 1)
        mw, mh = math.ceil(self.width * sx - 1e-9), math.ceil(self.height * sy - 1e-9)
        right, bottom = round(self.right * sx), round(self.bottom * sy)
        x, y = w - right - mw, h - bottom - mh
        if x < 0 or y < 0:
            raise ValueError(f"填白区域超出图片范围（{w} × {h}），请缩小区域或使用百分比")
        return x, y, w - right, h - bottom


class SmartSample(BaseModel):
    path: str
    mask: WhiteMask


class PreviewIn(BaseModel):
    path: str
    mask: WhiteMask = Field(default_factory=WhiteMask)
    mode: Literal["fixed", "smart"] = "fixed"
    sample: SmartSample | None = None
    threshold: float = Field(default=.88, ge=.80, le=.99, allow_inf_nan=False)
    removal: Removal = "white"


class BatchIn(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=100000)
    folder: str | None = None
    output_dir: str | None = None
    mask: WhiteMask = Field(default_factory=WhiteMask)
    mode: Literal["fixed", "smart"] = "fixed"
    sample: SmartSample | None = None
    threshold: float = Field(default=.88, ge=.80, le=.99, allow_inf_nan=False)
    removal: Removal = "white"


def load_image(path: Path) -> Image.Image:
    if path.suffix.lower() not in _IMAGE_EXTS:
        raise ValueError("不支持的图片格式")
    with Image.open(path) as src:
        if getattr(src, "n_frames", 1) > 1:
            raise ValueError("暂不支持动画或多页图片，请先拆成单张图片")
        im = ImageOps.exif_transpose(src)
        # Preserve actual pixels and alpha; palette white may not exist in the palette.
        if im.mode not in {"RGB", "RGBA", "L", "LA"}:
            im = im.convert("RGBA" if "transparency" in im.info else "RGB")
        else:
            im = im.copy()
    return im


def fill_white(im: Image.Image, mask: WhiteMask) -> tuple[int, int, int, int]:
    box = mask.box(im.size)
    fill_box(im, box)
    return box


def fill_box(im: Image.Image, box: tuple[int, int, int, int]) -> None:
    erase(im, box, "white")


def prepare_sample(sample: SmartSample | None, removal: Removal = "white", *, trim: bool = True) -> WatermarkTemplate:
    if sample is None:
        raise ValueError("请先框选水印，并点击「设为水印样本」")
    with load_image(Path(sample.path)) as im:
        return make_template(im, sample.mask.box(im.size), allow_dark=removal != "white", trim=trim)


def process_image(im: Image.Image, body: PreviewIn | BatchIn,
                  template: WatermarkTemplate | None = None) -> tuple[tuple[int, int, int, int], float | None, str]:
    if template and body.mode == "smart":
        match = locate(im, template, body.threshold, allow_dark=body.removal != "white",
                       allow_artwork=body.removal == "repair")
        box, score = match.box, match.score
    else:
        box, score = body.mask.box(im.size), None
    method = erase(im, box, body.removal, template)
    return box, score, method


def preview_url(im: Image.Image) -> str:
    thumb = im.copy()
    try:
        thumb.thumbnail((1000, 1200))
        buf = io.BytesIO()
        thumb.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    finally:
        thumb.close()


@router.post("/preview")
def preview(body: PreviewIn) -> dict:
    try:
        with load_image(Path(body.path)) as im:
            original = preview_url(im)
            detected = None
            reason = ""
            score = None
            method = None
            sample = None
            if body.sample and (body.mode == "smart" or body.removal == "repair"):
                sample = prepare_sample(body.sample, body.removal, trim=body.mode == "smart")
            try:
                box, score, method = process_image(im, body, sample)
                detected = True if sample or body.removal != "white" else None
            except MatchSkipped as e:
                box, detected, reason = None, False, str(e)
            finally:
                if sample:
                    sample.pixels.close()
            return {"width": im.width, "height": im.height, "box": box,
                    "original": original, "processed": preview_url(im),
                    "detected": detected, "score": score, "reason": reason, "method": method}
    except (OSError, ValueError, Image.DecompressionBombError) as e:
        raise HTTPException(400, str(e)) from e


_lock = threading.Lock()
_jobs: dict[str, dict] = {}
_cancel: dict[str, threading.Event] = {}


def snapshot(job_id: str) -> dict:
    with _lock:
        if job_id not in _jobs:
            raise HTTPException(404, "作业不存在，服务可能已重启")
        job = _jobs[job_id]
        return {**job, "errors": list(job["errors"]),
                "elapsed_s": round((job.get("finished_at") or time.monotonic()) - job["started_at"], 2)}


def _run(job_id: str, body: BatchIn, out: Path, source: Path | None,
         template: WatermarkTemplate | None = None) -> None:
    event = _cancel[job_id]
    results: list[dict] = []
    try:
        for path in body.paths:
            if event.is_set():
                break
            src = Path(path).resolve()
            with _lock:
                _jobs[job_id]["current"] = str(src)
            try:
                rel = src.relative_to(source) if source else Path(src.name)
                target = out / rel.with_suffix(".png")
                target.parent.mkdir(parents=True, exist_ok=True)
                with load_image(src) as im:
                    box, score, method = process_image(im, body, template)
                    # Exclusive creation also protects same stems from different extensions.
                    suffix = 0
                    while True:
                        candidate = target if suffix == 0 else target.with_stem(f"{target.stem}_{suffix}")
                        try:
                            stream = candidate.open("xb")
                            break
                        except FileExistsError:
                            suffix += 1
                    try:
                        with stream:
                            im.save(stream, format="PNG")
                    except Exception:
                        candidate.unlink(missing_ok=True)
                        raise
                with _lock:
                    _jobs[job_id]["succeeded"] += 1
                results.append({"path": str(src), "output": str(candidate), "box": box,
                                "score": score, "method": method, "status": "done"})
            except MatchSkipped as e:
                with _lock:
                    job = _jobs[job_id]
                    job["skipped"] += 1
                    job["errors"].append({"path": str(src), "error": str(e), "kind": "skipped"})
                results.append({"path": str(src), "status": "skipped", "reason": str(e)})
            except Exception as e:
                with _lock:
                    job = _jobs[job_id]
                    job["failed"] += 1
                    job["errors"].append({"path": str(src), "error": str(e)})
                results.append({"path": str(src), "status": "failed", "reason": str(e)})
            with _lock:
                _jobs[job_id]["completed"] += 1
    finally:
        if template:
            template.pixels.close()
        if template or body.removal != "white":
            try:
                import json
                (out / "watermark-report.json").write_text(
                    json.dumps({"mode": body.mode, "removal": body.removal,
                                "sample": body.sample.model_dump() if body.sample else None,
                                "threshold": body.threshold, "total": len(body.paths),
                                "cancelled": event.is_set(), "results": results}, ensure_ascii=False, indent=2),
                    encoding="utf-8")
            except OSError as e:
                with _lock:
                    _jobs[job_id]["errors"].append({"path": str(out), "error": f"报告保存失败：{e}"})
        with _lock:
            job = _jobs[job_id]
            job["status"] = "cancelled" if event.is_set() else "done"
            job["current"] = ""
            job["finished_at"] = time.monotonic()


@router.post("/batch")
def start_batch(body: BatchIn) -> dict:
    source = Path(body.folder).resolve() if body.folder else None
    paths = list(dict.fromkeys(str(Path(p).resolve()) for p in body.paths))
    if source and (not source.is_dir() or any(not Path(p).is_relative_to(source) for p in paths)):
        raise HTTPException(400, "图片必须位于所选源文件夹内")
    if any(Path(p).suffix.lower() not in _IMAGE_EXTS or not Path(p).is_file() for p in paths):
        raise HTTPException(400, "部分图片不存在或格式不支持，请重新选择")
    body = body.model_copy(update={"paths": paths})
    template = None
    if body.mode == "smart" or (body.removal == "repair" and body.sample):
        try:
            template = prepare_sample(body.sample, body.removal, trim=body.mode == "smart")
        except (OSError, ValueError, Image.DecompressionBombError) as e:
            raise HTTPException(400, str(e)) from e
    parent = Path(body.output_dir).resolve() if body.output_dir else (
        source.parent if source else Path(paths[0]).parent)
    stem = source.name + "_去水印" if source else "图片_去水印"
    with _lock:
        if any(j["status"] == "running" for j in _jobs.values()):
            if template:
                template.pixels.close()
            raise HTTPException(409, "已有去水印作业正在进行")
        # Bound retained history; this tool does not alter the SR queue or GPU lease.
        while len(_jobs) >= 20:
            old = next(iter(_jobs))
            _jobs.pop(old)
            _cancel.pop(old)
        try:
            parent.mkdir(parents=True, exist_ok=True)
            i = 0
            while True:
                out = parent / (stem if i == 0 else f"{stem}_{i}")
                try:
                    out.mkdir()
                    break
                except FileExistsError:
                    i += 1
        except OSError as e:
            if template:
                template.pixels.close()
            raise HTTPException(400, f"无法创建输出目录：{e}") from e
        job_id = uuid.uuid4().hex
        _jobs[job_id] = {"id": job_id, "status": "running", "total": len(paths),
                         "completed": 0, "succeeded": 0, "failed": 0, "skipped": 0, "errors": [],
                         "current": "", "output_dir": str(out), "mode": body.mode, "removal": body.removal,
                         "started_at": time.monotonic()}
        _cancel[job_id] = threading.Event()
        threading.Thread(target=_run, args=(job_id, body, out, source, template), daemon=True,
                         name=f"watermark-{job_id[:8]}").start()
    return snapshot(job_id)


@router.get("/batch/{job_id}")
def get_batch(job_id: str) -> dict:
    return snapshot(job_id)


@router.post("/batch/{job_id}/cancel")
def cancel_batch(job_id: str) -> dict:
    with _lock:
        if job_id not in _cancel:
            raise HTTPException(404, "作业不存在")
        _cancel[job_id].set()
    return snapshot(job_id)
