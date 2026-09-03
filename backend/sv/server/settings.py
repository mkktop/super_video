"""应用设置：ROOT/data/settings.json（运行时可改，gitignore）。"""
from __future__ import annotations

import json
from pathlib import Path

from ..paths import DATA_ROOT

DEFAULTS = {
    "engine": "auto",  # auto | cuda | trt（CUDA+TensorRT，需 .venv-cuda + tensorrt 组件）| directml | cpu（兜底，慢）
    "precision": "fp16",  # fp16（实测提速 1.36~1.73x，PSNR 74dB+）| fp32
    "download_proxy": "",  # 模型下载代理："" = 跟随系统代理 | direct = 直连 | http://host:port = 自定义
    "perf_sampling": True,  # 性能监控后台采样（CPU/GPU/内存，2s 一拍）
    "auto_update_check": True,  # 启动时自动检查 GitHub Releases 更新
    "update_channel": "stable",  # 更新通道：stable 只看正式版 Release | preview 额外可收到 -preview.N 预览版（主进程读，经 IPC 同步）
    "parallel_streams": False,  # 双路并行：两进程分段同时推理（实测 +17~21%，显存翻倍）
    "output_dir": "",  # 默认输出目录：空 = 与源视频同目录；超分任务/剪切未显式指定输出时写到这里（不存在则自动建）
    "notify_task_done": True,  # 任务完成/失败时系统通知+闪任务栏（renderer 读；窗口聚焦时不打扰）
    "close_to_tray": False,  # 关闭按钮=最小化到系统托盘继续处理任务（托盘菜单退出应用）
    "sr_profiling": False,  # 超分性能日志：任务结束保留分段耗时明细到 DATA_ROOT/data/sr_logs，供分析速度瓶颈
    "compare_still_count": 4,  # 对比静帧样本数（1~8，与 compare.py MAX_STILL_COUNT 一致）：模型对比创建作业时快照；任务对比页（task_stills.py）打开时按当前值构建
    "queue_done_action": "none",  # 队列全部完成后：none 无动作 | notify 系统通知 | shutdown 关机 | sleep 休眠
    "queue_schedule": "always",  # 处理时机：always 立即 | window 指定时段 | idle 电脑空闲（只拦新任务，不打断进行中）
    "schedule_start": "22:00",  # window 起始时刻（HH:MM；起>止=跨午夜，如 22:00~08:00；起=止视为全天）
    "schedule_end": "08:00",  # window 结束时刻
    "idle_minutes": 15,  # idle 模式：键鼠静置多少分钟后才开始处理
    "output_name_template": "",  # 输出命名模板：空=沿用原名（冲突加_倍率后缀）；变量 {name}/{model}/{scale}/{res}/{date}
}

SETTINGS_PATH = DATA_ROOT / "data" / "settings.json"


def _valid_proxy(v: str) -> bool:
    if v in ("", "direct"):
        return True
    return v.startswith("http://") or v.startswith("https://")


def _valid_hhmm(v: str) -> bool:
    try:
        h, m = v.split(":")
        return 0 <= int(h) <= 23 and 0 <= int(m) <= 59
    except ValueError:
        return False


def load() -> dict:
    data = dict(DEFAULTS)
    if SETTINGS_PATH.exists():
        try:
            raw = SETTINGS_PATH.read_bytes()
        except OSError:
            return data
        # UTF-8 是本应用唯一写入口径，但文件可能被外部工具重存成 ANSI/GBK
        # （中文系统记事本手改 output_dir/命名模板是实测踩法）：utf-8 解码
        # 抛 UnicodeDecodeError 会沿 worker 的 settings.load() 一路裸奔，
        # 任务以「引擎加载失败 UnicodeDecodeError: ...」硬失败（1060 实机）。
        # 解不动 UTF-8（含 BOM）退 GBK 读出内容——下次 save() 会整体按
        # UTF-8 原子回写，自然愈合回单一口径；两边都解不动才弃用回默认。
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = raw.decode("gbk")
            except (UnicodeDecodeError, ValueError):
                return data
        try:
            data.update(json.loads(text))
        except json.JSONDecodeError:
            pass
    return data


def save(updates: dict) -> dict:
    data = load()
    for k in DEFAULTS:
        if k in updates:
            v = updates[k]
            if k == "engine" and v not in ("auto", "cuda", "trt", "directml", "cpu"):
                raise ValueError(f"非法 engine 值: {v}")
            if k == "precision" and v not in ("fp16", "fp32"):
                raise ValueError(f"非法 precision 值: {v}")
            if k == "download_proxy" and not _valid_proxy(str(v)):
                raise ValueError(f"非法 download_proxy 值: {v}")
            if k == "perf_sampling" and not isinstance(v, bool):
                raise ValueError(f"非法 perf_sampling 值: {v}")
            if k == "auto_update_check" and not isinstance(v, bool):
                raise ValueError(f"非法 auto_update_check 值: {v}")
            if k == "update_channel" and v not in ("stable", "preview"):
                raise ValueError(f"非法 update_channel 值: {v}（stable/preview）")
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
            if k == "queue_done_action" and v not in ("none", "notify", "shutdown", "sleep"):
                raise ValueError(f"非法 queue_done_action 值: {v}（none/notify/shutdown/sleep）")
            if k == "queue_schedule" and v not in ("always", "window", "idle"):
                raise ValueError(f"非法 queue_schedule 值: {v}（always/window/idle）")
            if k in ("schedule_start", "schedule_end"):
                from re import fullmatch

                if not isinstance(v, str) or not fullmatch(r"\d{1,2}:\d{2}", v.strip()) \
                        or not _valid_hhmm(v.strip()):
                    raise ValueError(f"非法 {k} 值: {v}（需 HH:MM，如 22:00）")
                v = v.strip()
            if k == "idle_minutes":
                if not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= 240:
                    raise ValueError(f"非法 idle_minutes 值: {v}（1~240 分钟）")
            if k == "output_name_template":
                from re import sub

                if not isinstance(v, str) or len(v) > 80:
                    raise ValueError("输出命名模板需为 80 字符以内的字符串")
                if any(c in v for c in '<>:"/\\|?*') or "\x00" in v:
                    raise ValueError("命名模板含文件名非法字符（<>:\"/\\|?*）")
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
