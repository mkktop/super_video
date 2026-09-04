"""任务/预设校验共用的编解码常量与扩展名清单（tasks 与 models 路由共用）。"""
from __future__ import annotations

# codec → 硬件能力位（None = 软编总是可用）
_CODECS = {
    "h264": None, "h265": None,
    "h264_nvenc": "nvenc", "hevc_nvenc": "nvenc", "av1_nvenc": "av1_nvenc",
    "h264_amf": "amf", "hevc_amf": "amf",
    "av1_svt": "svt_av1",
}
_AUDIO_MODES = ("auto", "copy", "aac", "flac", "none")
_CONTAINERS = ("mp4", "mkv", "mov")

_PRESETS_XCODE = (
    "ultrafast", "superfast", "veryfast", "faster", "fast",
    "medium", "slow", "slower", "veryslow",
)

# 图片超分接受的输入扩展名（识别失败/损坏文件由 PIL 打开时报 400）
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
