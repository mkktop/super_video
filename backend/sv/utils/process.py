"""子进程工具：无窗口创建标志 + 进程树终止。"""
from __future__ import annotations

import sys

import psutil

# CREATE_NO_WINDOW：Windows 下避免每次拉起 ffmpeg 闪控制台窗口
WINDOWS_CREATE_FLAGS = 0x08000000 if sys.platform == "win32" else 0


def kill_tree(pid: int) -> None:
    """终止 pid 及其全部子进程：先 terminate，3 秒后仍存活则 kill。全程幂等。"""
    try:
        parent = psutil.Process(pid)
        procs = [parent, *parent.children(recursive=True)]
    except psutil.Error:
        return  # 进程已消失
    for p in procs:
        try:
            p.terminate()
        except psutil.Error:
            pass
    try:
        _, alive = psutil.wait_procs(procs, timeout=3)
    except psutil.Error:
        return
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
