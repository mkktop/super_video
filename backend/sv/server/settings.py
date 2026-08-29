"""应用设置：ROOT/data/settings.json（运行时可改，gitignore）。"""
from __future__ import annotations

import json
from pathlib import Path

from ..paths import DATA_ROOT

DEFAULTS = {
    "engine": "auto",  # auto | cuda | trt（CUDA+TensorRT，需 .venv-cuda + tensorrt 组件）| directml
    "precision": "fp16",  # fp16（实测提速 1.36~1.73x，PSNR 74dB+）| fp32
    "download_proxy": "",  # 模型下载代理："" = 跟随系统代理 | direct = 直连 | http://host:port = 自定义
    "perf_sampling": True,  # 性能监控后台采样（CPU/GPU/内存，2s 一拍）
    "auto_update_check": True,  # 启动时自动检查 GitHub Releases 更新
    "parallel_streams": False,  # 双路并行：两进程分段同时推理（实测 +17~21%，显存翻倍）
    "output_dir": "",  # 默认输出目录：空 = 与源视频同目录；超分任务/剪切未显式指定输出时写到这里（不存在则自动建）
    "notify_task_done": True,  # 任务完成/失败时系统通知+闪任务栏（renderer 读；窗口聚焦时不打扰）
    "close_to_tray": False,  # 关闭按钮=最小化到系统托盘继续处理任务（托盘菜单退出应用）
    "sr_profiling": False,  # 超分性能日志：任务结束保留分段耗时明细到 DATA_ROOT/data/sr_logs，供分析速度瓶颈
    "compare_still_count": 4,  # 模型对比静帧样本数（1~8，与 compare.py MAX_STILL_COUNT 一致）：创建对比作业时快照
}

SETTINGS_PATH = DATA_ROOT / "data" / "settings.json"


def _valid_proxy(v: str) -> bool:
    if v in ("", "direct"):
        return True
    return v.startswith("http://") or v.startswith("https://")


def load() -> dict:
    data = dict(DEFAULTS)
    if SETTINGS_PATH.exists():
        try:
            data.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return data


def save(updates: dict) -> dict:
    data = load()
    for k in DEFAULTS:
        if k in updates:
            v = updates[k]
            if k == "engine" and v not in ("auto", "cuda", "trt", "directml"):
                raise ValueError(f"非法 engine 值: {v}")
            if k == "precision" and v not in ("fp16", "fp32"):
                raise ValueError(f"非法 precision 值: {v}")
            if k == "download_proxy" and not _valid_proxy(str(v)):
                raise ValueError(f"非法 download_proxy 值: {v}")
            if k == "perf_sampling" and not isinstance(v, bool):
                raise ValueError(f"非法 perf_sampling 值: {v}")
            if k == "auto_update_check" and not isinstance(v, bool):
                raise ValueError(f"非法 auto_update_check 值: {v}")
            if k == "parallel_streams" and not isinstance(v, bool):
                raise ValueError(f"非法 parallel_streams 值: {v}")
            if k == "notify_task_done" and not isinstance(v, bool):
                raise ValueError(f"非法 notify_task_done 值: {v}")
            if k == "close_to_tray" and not isinstance(v, bool):
                raise ValueError(f"非法 close_to_tray 值: {v}")
            if k == "sr_profiling" and not isinstance(v, bool):
                raise ValueError(f"非法 sr_profiling 值: {v}")
            if k == "compare_still_count":
                # bool 是 int 子类，须显式排除
                if not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= 8:
                    raise ValueError(f"非法 compare_still_count 值: {v}（1~8）")
            if k == "output_dir":
                if not isinstance(v, str):
                    raise ValueError(f"非法 output_dir 值: {v}")
                v = v.strip()  # 目录不存在是合法状态：任务创建时才 mkdir 并报可读错误
            data[k] = v
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    # tmp + 原子换名：写一半崩溃/断电不留损坏的 JSON——load() 会静默吞
    # JSONDecodeError 回默认值，用户的全部设置就丢了
    tmp = SETTINGS_PATH.with_name(SETTINGS_PATH.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SETTINGS_PATH)
    return data
