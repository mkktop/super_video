"""智能推荐：源探测 + 采样帧统计 → 模型/倍率/预处理建议（规则引擎，非 AI）。

设计取向：保守推荐、理由透明。推荐的每个字段前端都会展示出处（如"检测到
大色块平涂 → 判定动画内容"），用户一键应用前可复核；判断不了的维度
（如补帧——涉及流畅度口味）只给提示不自动开启。
"""
from __future__ import annotations

from .analyze import COMB_MIN, FLAT_ANIME_MIN
from .probe import MediaInfo

# 老编码：DVD/VHS rip/早期网络视频，普遍带压缩噪点与色带
OLD_CODECS = {
    "mpeg1video", "mpeg2video", "mpeg4", "msmpeg4v1", "msmpeg4v2",
    "msmpeg4v3", "vc1", "flv1", "h263", "wmv1", "wmv2", "rv40",
}

# 内容类型 → 模型偏好（前者缺注册时顺延）；只推荐 onnx 系（torch 需独立环境）
_ANIME_PREF = ["realesr-animevideov3", "real-cugan"]
_LIVE_PREF = ["realesrgan-x4plus", "realesr-animevideov3"]

_INTERLACED_FIELD_ORDER = {"tt", "bb", "tb", "bt"}


def _pick_scale(scales: list[int], h: int) -> int:
    """倍率：取能达到 ≥1080p 的最小档（省算力）；都达不到取最大档。"""
    reach = [s for s in sorted(scales) if h * s >= 1080]
    return reach[0] if reach else max(scales)


def recommend(info: MediaInfo, stats: dict | None) -> dict:
    """纯规则推荐（不查注册表，模型可用性由 build_recommendation 收口）。"""
    reasons: list[str] = []
    animated: bool | None = None
    if stats and stats.get("frames"):
        animated = stats.get("flat_ratio", 0.0) >= FLAT_ANIME_MIN
        pct = stats["flat_ratio"] * 100
        reasons.append(
            f"采样 {stats['frames']} 帧平坦像素占 {pct:.0f}%，判定为"
            + ("动画内容（大色块平涂）" if animated else "真人/实拍内容（纹理连续）")
        )
    old_codec = info.video_codec in OLD_CODECS
    if old_codec:
        reasons.append(f"源编码 {info.video_codec} 属老编码，常见压缩噪点与色带")

    # 隔行检测：容器场序声明 ∪ 帧梳状统计（两边都常漏报，取并集）
    interlaced = info.field_order in _INTERLACED_FIELD_ORDER
    if stats and stats.get("comb_frac", 0.0) >= COMB_MIN:
        interlaced = True
    if interlaced:
        src = f"场序 {info.field_order}" if info.field_order in _INTERLACED_FIELD_ORDER else "梳状纹理"
        reasons.append(f"检测到隔行扫描（{src}），建议开启反交错")

    # 模型偏好：动画 → 动画系；真人 → 修复系。高清真人（≥960p）特殊处理：
    # x4plus 只有 x4，1080p×4=7680×4320 踩 8K 编码上限（H.264 直接编不了、
    # 报误导性"无可用设备"），退到视频模型的 x2 档（RealESRGAN v3 对实拍
    # 视频同样可用）。stats 缺失（分析失败）按未知内容走保守的动画线
    # （视频向模型、有 x2 档，最坏情况不踩陷阱）。
    if animated is False:
        if info.height >= 960:
            model_pref = ["realesr-animevideov3", "realesrgan-x4plus"]
            reasons.append(
                f"真人高清源（{info.height}p）：x2 超分避开 8K 编码上限，画质与速度更稳")
        else:
            model_pref = _LIVE_PREF
    else:
        model_pref = _ANIME_PREF
    # 倍率先按首选模型的档位估（模型缺失时 build_recommendation 会换备选并重算）
    scales = [2, 3, 4] if model_pref[0] == "realesr-animevideov3" else [4]
    target_scale = _pick_scale(scales, info.height)
    reasons.append(f"源高 {info.height}px，推荐 x{target_scale}（→ {info.height * target_scale}p）")

    deband = animated is True and (info.height <= 600 or old_codec)
    if deband:
        reasons.append("动画低分辨率/老编码源色带常见，建议开启去色带")

    interp = "off"
    if info.fps and info.fps <= 25:
        reasons.append(f"源帧率 {info.fps:.2f}，如需更流畅可手动开启 RIFE 补帧（默认不自动开）")

    if old_codec and animated is True:
        reasons.append("如压缩噪点明显，可在模型列表换 Real-CUGAN 并选降噪档")

    return {
        "model_id": model_pref[0],
        "target_scale": target_scale,
        "deinterlace": interlaced,
        "deband": deband,
        "interp": interp,
        "animated": animated,
        "reasons": reasons,
    }


def build_recommendation(info: MediaInfo, stats: dict | None) -> dict:
    """recommend() + 注册表收口：首选模型缺失/无该倍率时顺延备选并重算倍率。"""
    from ..models.registry import load_registry

    rec = recommend(info, stats)
    specs = load_registry()
    pref = _ANIME_PREF if rec["animated"] is not False else (
        ["realesr-animevideov3", "realesrgan-x4plus"]
        if info.height >= 960 else _LIVE_PREF)
    chosen = next((m for m in pref
                   if m in specs and specs[m].kind == "sr" and specs[m].engine == "onnx"
                   and specs[m].scale), None)
    if chosen is None:  # 注册表大改导致偏好全缺：如实透出"无法推荐模型"
        rec["model_id"] = None
        rec["model_name"] = ""
        return rec
    rec["model_id"] = chosen
    rec["model_name"] = specs[chosen].name
    if rec["target_scale"] not in specs[chosen].scale:
        rec["target_scale"] = _pick_scale(specs[chosen].scale, info.height)
    return rec
