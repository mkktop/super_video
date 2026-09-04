"""模型对比路由：同一素材 × 多模型并排处理（作业体在 sv/server/compare.py）。"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ...pipeline.probe import UnsupportedMedia, probe
from .. import compare, task_stills
from ..consts import _IMAGE_EXTS
from ...models.registry import load_registry

router = APIRouter(tags=["compare"])


class CompareCreate(BaseModel):
    kind: str  # image | video
    input: str
    start_s: float = 0.0
    end_s: float = 0.0
    models: list[str]
    scale: int


@router.post("/api/compare", status_code=201)
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


@router.get("/api/compare/cache")
def compare_cache_stats() -> dict:
    """对比产物占用统计（设置页展示）：模型对比作业 + 任务静帧缓存合并口径。
    注意必须注册在 /{job_id} 之前，否则 "cache" 会被当作 job_id 吞掉。"""
    a, b = compare.cache_stats(), task_stills.cache_stats()
    return {"jobs": a["jobs"] + b["jobs"], "bytes": a["bytes"] + b["bytes"]}


@router.delete("/api/compare/cache")
def clear_compare_cache() -> dict:
    """清理全部对比产物（模型对比作业目录 + 任务静帧缓存）；有正在写的拒绝。"""
    try:
        r1 = compare.clear_cache()
        r2 = task_stills.clear_all()
        return {"removed_jobs": r1["removed_jobs"] + r2["removed_jobs"],
                "freed_bytes": r1["freed_bytes"] + r2["freed_bytes"]}
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


@router.get("/api/compare/{job_id}")
def compare_status(job_id: str) -> dict:
    job = compare.get(job_id)
    if job is None:
        raise HTTPException(404)
    return _compare_view(job)


@router.post("/api/compare/{job_id}/cancel")
def cancel_compare(job_id: str) -> dict:
    if not compare.request_cancel(job_id):
        raise HTTPException(409, "作业已结束")
    return {"ok": True}


@router.get("/api/compare/{job_id}/asset/{key:path}")
def compare_asset(job_id: str, key: str):
    """对比产物文件：key 为白名单标识（seg/src_still/<i>/out/<mid>/still/<mid>[/<i>]），
    不开放任意路径。"""
    p = compare.asset_path(job_id, key)
    if p is None:
        raise HTTPException(404)
    media = "video/mp4" if p.suffix == ".mp4" else "image/png"
    return FileResponse(p, media_type=media)
