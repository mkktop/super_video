"""睡眠唤醒模拟验证（README 灾难场景清单"睡眠唤醒"项）。

方法：跑一个真实超分任务（CLI run），推理中途用 psutil 把整个进程树
（python + 2 个 ffmpeg）全部挂起 N 秒——等价于系统睡眠期间所有进程冻结、
管道停摆——然后恢复，验证任务继续跑完且产物完整（分辨率/帧数/时长/音轨）。

用法：cd backend && ../.venv/Scripts/python.exe scripts/sim_sleep_wake.py
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sv.paths import TEMP_DIR, ffmpeg_bin
from sv.pipeline.probe import probe
from sv.utils.process import WINDOWS_CREATE_FLAGS

IN_CLIP = TEMP_DIR / "wake_in.mp4"
OUT_CLIP = TEMP_DIR / "wake_out.mp4"
FREEZE_S = 6
WARMUP_S = 4


def make_clip() -> None:
    subprocess.run([
        ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24",
        "-f", "lavfi", "-i", "sine=frequency=440",
        "-t", "12", "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(IN_CLIP),
    ], check=True, creationflags=WINDOWS_CREATE_FLAGS)


def tree_of(pid: int) -> list[psutil.Process]:
    p = psutil.Process(pid)
    return [p, *p.children(recursive=True)]


def main() -> int:
    TEMP_DIR.mkdir(exist_ok=True)
    if not IN_CLIP.exists():
        print(f"[1/5] 生成测试片 {IN_CLIP.name} (640x360 12s)")
        make_clip()
    else:
        print(f"[1/5] 复用已有测试片 {IN_CLIP.name}")
    src = probe(IN_CLIP)
    OUT_CLIP.unlink(missing_ok=True)

    print(f"[2/5] 启动超分任务 x2（{src.total_frames} 帧）")
    t0 = time.perf_counter()
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve().parents[1] / "cli.py"),
         "run", str(IN_CLIP), "-m", "realesr-animevideov3", "-s", "2",
         "-o", str(OUT_CLIP)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        creationflags=WINDOWS_CREATE_FLAGS, cwd=str(Path(__file__).resolve().parents[1]),
    )
    time.sleep(WARMUP_S)
    procs = tree_of(proc.pid)
    states = [(p.pid, p.name()) for p in procs]
    if proc.poll() is not None:
        print(f"FAIL: 任务 {WARMUP_S}s 内就结束了，挂起窗口未覆盖（考虑加大视频时长）")
        return 1
    print(f"[3/5] 挂起进程树 {FREEZE_S}s（模拟系统睡眠）: {states}")
    for p in procs:
        p.suspend()
    time.sleep(FREEZE_S)
    for p in procs:
        try:
            p.resume()
        except psutil.NoSuchProcess:
            pass
    print("[4/5] 已恢复，等待任务完成…")
    out, _ = proc.communicate(timeout=600)
    elapsed = time.perf_counter() - t0
    if proc.returncode != 0:
        print(f"FAIL: CLI 退出码 {proc.returncode}\n{out[-2000:]}")
        return 1

    print(f"[5/5] 校验产物（总耗时 {elapsed:.0f}s，含 {FREEZE_S}s 冻结）")
    dst = probe(OUT_CLIP)
    checks = [
        ("分辨率 1280x720", (dst.width, dst.height) == (1280, 720)),
        ("帧数一致(±2)", abs(dst.total_frames - src.total_frames) <= 2),
        ("时长一致(±0.5s)", abs(dst.duration_s - src.duration_s) < 0.5),
        ("音轨保留", dst.has_audio),
    ]
    ok = True
    for name, passed in checks:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
        ok = ok and passed
    print("RESULT: PASS — 睡眠唤醒模拟通过，任务冻结后恢复继续，产物完整" if ok
          else "RESULT: FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
