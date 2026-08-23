"""模型注册表：从 registry_json/ 加载内置 manifest，支持用户目录扩展。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..paths import MODELS_DIR, ROOT

REGISTRY_DIR = Path(__file__).parent / "registry_json"
BUNDLED_DIR = Path(__file__).parent / "bundled"  # 随仓库分发的模型权重（小体积）
USER_REGISTRY_DIR = MODELS_DIR / "custom"


class ModelNotFoundError(KeyError):
    pass


@dataclass
class ModelSpec:
    id: str
    name: str
    engine: str  # onnx | torch
    scale: list[int]
    content: list[str]
    speed: str  # fast | balanced | slow
    vram_gb: float
    io: dict = field(default_factory=dict)
    tile_hint: int = 0
    description: str = ""
    vendor: str = ""
    license: str = ""
    files: list[dict] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "ModelSpec":
        return cls(
            id=d["id"], name=d["name"], engine=d["engine"], scale=d["scale"],
            content=d.get("content", []), speed=d.get("speed", "balanced"),
            vram_gb=d.get("vram_gb", 4), io=d.get("io", {}),
            tile_hint=d.get("tile_hint", 0), description=d.get("description", ""),
            vendor=d.get("vendor", ""), license=d.get("license", ""),
            files=d.get("files", []),
        )

    def engine_kwargs(self, scale: int, tile: int = 0) -> dict:
        """按引擎类型生成引擎构造参数。"""
        return {"io": self.io, "tile": tile or self.tile_hint}


def load_registry() -> dict[str, ModelSpec]:
    specs: dict[str, ModelSpec] = {}
    for d in (REGISTRY_DIR, USER_REGISTRY_DIR):
        if not d.exists():
            continue
        for f in sorted(d.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                spec = ModelSpec.from_dict(data)
                specs[spec.id] = spec
            except (json.JSONDecodeError, KeyError) as e:
                # 单个 manifest 损坏不影响整体
                print(f"[registry] 跳过无效 manifest {f.name}: {e}")
    return specs


def get_model(model_id: str) -> ModelSpec:
    specs = load_registry()
    if model_id not in specs:
        raise ModelNotFoundError(
            f"未知模型 {model_id}，可用: {', '.join(specs) or '(无)'}"
        )
    return specs[model_id]


def model_dir(model_id: str) -> Path:
    return MODELS_DIR / model_id


def local_files(spec: ModelSpec) -> list[Path]:
    d = model_dir(spec.id)
    return [d / f["name"] for f in spec.files if "name" in f]


def file_for_scale(spec: ModelSpec, scale: int) -> dict:
    """按倍率选权重文件：files 带 scale 字段的精确匹配，无字段的通用于所有倍率。"""
    for f in spec.files:
        if f.get("scale") == scale:
            return f
    for f in spec.files:
        if "scale" not in f:
            return f
    raise ModelNotFoundError(f"{spec.id} 缺少 x{scale} 权重")


def model_file(spec: ModelSpec, scale: int, precision: str = "fp32") -> Path:
    """权重解析：models_store 优先，其次随包 bundled 目录。

    precision=="fp16" 时优先取 `_fp16` 兄弟文件（转换缓存或随包分发），
    不存在则回退 fp32 原件。
    """
    f = file_for_scale(spec, scale)
    in_store = model_dir(spec.id) / f["name"]
    if in_store.exists():
        base = in_store
    else:
        base = BUNDLED_DIR / f["name"]
        if not base.exists():
            return in_store
    if precision == "fp16":
        from .fp16 import fp16_path

        alt = fp16_path(base)
        if alt.exists():
            return alt
    return base
