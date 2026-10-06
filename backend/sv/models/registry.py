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
    speed: str  # fastest | fast | balanced | slow
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
    category: str = ""  # 主用途；与 content 内容标签、scenes 任务场景独立
    version: str = ""
    temporal: bool = False

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
            category=d.get("category", ""), version=d.get("version", ""),
            temporal=bool(d.get("temporal", False)),
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
            except (ValueError, KeyError, OSError) as e:
                # 单个 manifest 损坏不影响整体（ValueError 兼盖 JSON/UTF-8 解码错误）
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
    """显式变体必须精确匹配；未指定时按倍率取默认权重。"""
    if variant:
        for f in spec.files:
            if f.get("scale") == scale and f.get("variant") == variant:
                return f
        raise ModelNotFoundError(f"{spec.id} 缺少 x{scale} 的 {variant} 权重")
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


def denoise_levels(spec: ModelSpec, scale: int) -> list[int]:
    return sorted({int(f["variant"][7:]) for f in spec.files
                   if f.get("scale") == scale
                   and str(f.get("variant", "")).startswith("denoise")
                   and str(f["variant"])[7:].isdigit()})


def validate_denoise(spec: ModelSpec, scale: int, value) -> int | None:
    if value is None:
        return None
    try:
        level = int(value)
        if isinstance(value, bool) or (isinstance(value, float) and value != level):
            raise ValueError()
    except (TypeError, ValueError, OverflowError):
        raise ValueError("denoise 需为整数降噪档位") from None
    levels = denoise_levels(spec, scale)
    if level not in levels:
        raise ValueError(f"模型 {spec.id} 的 x{scale} 不支持降噪 {level}，"
                         f"可选 {levels or '无（请清空降噪参数）'}")
    return level


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
