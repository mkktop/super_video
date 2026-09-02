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
    kind: str = "sr"  # sr（超分）| interp（补帧）
    fp16: bool = True  # False = 该模型 fp16 转换不可用（如 DML 加载崩溃）
    u8_wrap: bool = True  # False = 禁用 uint8 包装双会话结构（CUGAN×DML 0x887A0006 前科）
    scenes: list[str] = field(default_factory=lambda: ["video", "image"])  # 适用场景标签：video/manga/image
    files: list[dict] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "ModelSpec":
        return cls(
            id=d["id"], name=d["name"], engine=d["engine"], scale=d["scale"],
            content=d.get("content", []), speed=d.get("speed", "balanced"),
            vram_gb=d.get("vram_gb", 4), io=d.get("io", {}),
            tile_hint=d.get("tile_hint", 0), description=d.get("description", ""),
            vendor=d.get("vendor", ""), license=d.get("license", ""),
            kind=d.get("kind", "sr"), fp16=d.get("fp16", True),
            u8_wrap=d.get("u8_wrap", True),
            scenes=d.get("scenes", ["video", "image"]),
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


def file_for_scale(spec: ModelSpec, scale: int, variant: str | None = None) -> dict:
    """按倍率+变体选权重：先精确匹配（scale+variant），再 scale 无变体，再通用。"""
    if variant:
        for f in spec.files:
            if f.get("scale") == scale and f.get("variant") == variant:
                return f
    for f in spec.files:
        if f.get("scale") == scale and "variant" not in f:
            return f
    for f in spec.files:
        if f.get("scale") == scale:
            return f
    for f in spec.files:
        if "scale" not in f:
            return f
    raise ModelNotFoundError(f"{spec.id} 缺少 x{scale} 权重")


def auto_variant(spec: ModelSpec, scale: int, src_h: int | None) -> str | None:
    """按源高度自动选变体（io.auto_variant == "height"——MangaJaNai 系）。

    该家族权重按设计源高度分档（"1200p".."2048p"，网点频率对位），取与源
    高度最近的档；平手取更低档（欠拟合网点比过拟合稳）。显式 variant /
    非该家族 / 未知高度一律返回 None 走 file_for_scale 的常规解析。
    """
    if src_h is None or spec.io.get("auto_variant") != "height":
        return None
    cands = [
        (int(f["variant"][:-1]), f["variant"])
        for f in spec.files
        if f.get("scale") == scale
        and isinstance(f.get("variant"), str)
        and f["variant"].endswith("p") and f["variant"][:-1].isdigit()
    ]
    if not cands:
        return None
    return min(cands, key=lambda t: (abs(t[0] - src_h), t[0]))[1]


def model_file(spec: ModelSpec, scale: int, precision: str = "fp32",
               variant: str | None = None) -> Path:
    """权重解析：models_store 优先，其次随包 bundled 目录。

    precision=="fp16" 且模型允许 fp16 时优先取 `_fp16` 兄弟文件
    （转换缓存或随包分发），不存在则回退 fp32 原件。
    """
    f = file_for_scale(spec, scale, variant)
    in_store = model_dir(spec.id) / f["name"]
    if in_store.exists():
        base = in_store
    else:
        base = BUNDLED_DIR / f["name"]
        if not base.exists():
            return in_store
    if precision == "fp16" and spec.fp16:
        from .fp16 import fp16_path

        alt = fp16_path(base)
        if alt.exists():
            return alt
    return base
