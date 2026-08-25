#!/usr/bin/env python
"""super_video CLI（M0）：probe / gen / models / run。"""
from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeRemainingColumn
from rich.table import Table

from sv.engines.onnx_engine import OnnxSrEngine
from sv.models import manager as model_manager
from sv.models.registry import get_model, load_registry, model_file
from sv.paths import TEMP_DIR, ffmpeg_bin
from sv.pipeline.probe import UnsupportedMedia, probe, validate_m0
from sv.pipeline.stream import EncodeOpts, PipelineError, StreamPipeline, TaskCanceled
from sv.utils.process import WINDOWS_CREATE_FLAGS

console = Console()


def cmd_probe(args):
    info = probe(args.input)
    validate_m0(info)  # 只抛异常，不中断打印
    table = Table(title=str(info.path.name))
    table.add_column("属性", style="cyan")
    table.add_column("值")
    table.add_row("分辨率", f"{info.width}x{info.height}")
    table.add_row("时长", f"{info.duration_s:.2f}s")
    table.add_row("帧率", f"{info.fps:.3f} ({info.fps_str}){' [VFR!]' if info.vfr else ''}")
    table.add_row("总帧数", str(info.total_frames))
    table.add_row("视频编码", f"{info.video_codec} / {info.pix_fmt} ({info.bit_depth}bit)")
    table.add_row("色彩", info.color_transfer)
    table.add_row("音轨", ", ".join(f"{a.codec} {a.channels}ch" for a in info.audio) or "无")
    table.add_row("字幕流", str(info.subtitle_count))
    console.print(table)


def cmd_gen(args):
    cmd = [
        ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size={args.w}x{args.h}:rate={args.fps}",
        "-f", "lavfi", "-i", "sine=frequency=440",
        "-t", str(args.duration),
        "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(args.output),
    ]
    subprocess.run(cmd, check=True, creationflags=WINDOWS_CREATE_FLAGS)
    console.print(f"[green]已生成测试视频[/green] {args.output} ({args.w}x{args.h}@{args.fps} {args.duration}s)")


def cmd_models(args):
    if args.action == "list":
        table = Table(title="模型注册表")
        for col in ("ID", "名称", "倍率", "内容", "速度", "显存", "状态"):
            table.add_column(col)
        for spec in load_registry().values():
            installed = model_manager.is_downloaded(spec)
            status = "[green]已安装[/green]" if installed else "未下载"
            table.add_row(
                spec.id, spec.name, "x".join(map(str, spec.scale)),
                "/".join(spec.content), spec.speed, f"{spec.vram_gb}GB", status,
            )
        console.print(table)
    elif args.action == "download":
        spec = get_model(args.model_id)
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(), "[progress.percentage]{task.percentage:>3.0f}%",
            console=console,
        ) as prog:
            total = sum(f.get("size", 0) for f in spec.files) or 100
            task = prog.add_task(spec.id, total=total)

            def cb(done, tot, label):
                prog.update(task, completed=done)

            model_manager.download(spec, cb)
        console.print(f"[green]已下载[/green] {spec.id} -> {local_files(spec)}")
    elif args.action == "pin":
        console.print(model_manager.pin_checksums(args.model_id))


def cmd_run(args):
    spec = get_model(args.model)
    if not model_manager.is_downloaded(spec):
        console.print(f"[yellow]模型未下载，先执行:[/yellow] python cli.py models download {spec.id}")
        sys.exit(2)

    try:
        info = probe(args.input)
        validate_m0(info)
    except (UnsupportedMedia, FileNotFoundError) as e:
        console.print(f"[red]输入不支持:[/red] {e}")
        sys.exit(2)

    scale = args.scale or max(spec.scale)
    if scale not in spec.scale:
        console.print(f"[red]模型 {spec.id} 不支持 x{scale}，可用: {spec.scale}[/red]")
        sys.exit(2)

    model_path = model_file(spec, scale)
    engine = OnnxSrEngine(
        model_path, scale, io=spec.io, device=args.device,
        tile=args.tile or spec.tile_hint,
    )
    t0 = time.perf_counter()
    engine.load()
    console.print(f"[dim]模型加载 {time.perf_counter()-t0:.1f}s | EP: {engine.provider_used}[/dim]")

    # 预热一帧（首次 DML 图编译较慢，不计入统计）
    engine.process(np.zeros((64, 64, 3), dtype=np.uint8))

    out = Path(args.output) if args.output else info.path.with_name(
        info.path.stem + f"_{scale}x" + ".mp4"
    )
    TEMP_DIR.mkdir(exist_ok=True)
    preview = TEMP_DIR / f"{info.path.stem}_preview.jpg"

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(), "[progress.percentage]{task.percentage:>3.0f}%",
        TextColumn("{task.fields[frames]}"),
        TextColumn("{task.fields[fps]}"),
        TimeRemainingColumn(compact=True, elapsed_when_finished=True),
        console=console,
    ) as prog:
        task = prog.add_task(
            f"{info.path.name} -> {out.name}",
            total=info.total_frames,
            frames="-/-", fps="- fps",
        )

        def cb(frames, total, fps, eta):
            mm, ss = divmod(int(eta), 60)
            prog.update(
                task, completed=frames,
                frames=f"{frames}/{total}",
                fps=f"{fps:.1f}fps",
            )

        pipeline = StreamPipeline(
            info, out, engine,
            EncodeOpts(codec=args.codec, crf=args.crf, preset=args.preset),
            progress_cb=cb, preview_path=preview,
        )
        try:
            stats = asyncio.run(pipeline.run())
        except TaskCanceled:
            console.print("[yellow]已取消[/yellow]")
            sys.exit(1)

    mb = stats.out_bytes / 1e6
    console.print(
        f"[green]完成[/green] {stats.out_path} | "
        f"{stats.frames}帧 {stats.fps:.2f}fps | {stats.elapsed_s:.1f}s | {mb:.1f}MB"
    )


def cmd_serve(args):
    import uvicorn

    from sv.server.app import app

    console.print(f"[green]sidecar 启动[/green] http://127.0.0.1:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


def cmd_worker(args):
    """打包模式复用入口：sidecar.exe worker <task_id>（等价 python -m sv.server.worker）。"""
    from sv.server.worker import main as worker_main

    sys.exit(worker_main(args.task_id))


def cmd_ort_check(args):
    """ORT 后端探测：providers 列表 + 真会话验证，输出一行 JSON。

    打包版先激活 TRT 组件（存在时）；dev 版挂 site-packages 的 NVIDIA DLL。
    engine_select 的 frozen 探测与现场排障共用本入口。
    """
    import json as _json
    import os as _os
    import re as _re

    from sv.engines.nvidia_dlls import register_nvidia_dlls

    result: dict = {"ok": False, "providers": [], "cuda": False, "trt": False,
                    "component": False, "error": None}
    try:
        if getattr(sys, "frozen", False):
            from sv.engines.trt_runtime import activate_component

            result["component"] = activate_component()
        else:
            register_nvidia_dlls()
        import numpy as _np
        import onnxruntime as _ort

        provs = _ort.get_available_providers()
        result["providers"] = list(provs)
        result["cuda"] = "CUDAExecutionProvider" in provs
        result["trt"] = "TensorrtExecutionProvider" in provs
        if result["trt"]:
            # TRT EP 编译在 provider DLL 里，运行时另需 nvinfer_N.dll；
            # builder_resource/plugin 不算核心库，缺失按 TRT 不可用标注
            import ctypes

            cands: list[Path] = []
            if getattr(sys, "frozen", False):
                from sv.engines.trt_runtime import COMPONENT_DIR as _comp

                cands = [p for p in (_comp / "dlls").glob("*.dll")
                         if _re.fullmatch(r"nvinfer_\d+\.dll", p.name)]
            if not cands:
                import sysconfig

                site = Path(sysconfig.get_paths()["purelib"])
                cands = [p for p in (site / "tensorrt_libs").glob("*.dll")
                         if _re.fullmatch(r"nvinfer_\d+\.dll", p.name)]
            try:
                ctypes.WinDLL(str(cands[0]))
            except (OSError, IndexError):
                result["trt"] = False
        if args.session:
            sess = _ort.InferenceSession(
                str(Path(args.session)), providers=["CUDAExecutionProvider"])
            inp = sess.get_inputs()[0]
            fixed = [d if isinstance(d, int) else 0 for d in inp.shape]
            c = fixed[1] if len(fixed) > 1 and fixed[1] else 3
            h = fixed[2] if len(fixed) > 2 and fixed[2] else 64
            w = fixed[3] if len(fixed) > 3 and fixed[3] else 64
            x = _np.zeros((1, c, h, w), dtype=_np.float32)
            sess.run([o.name for o in sess.get_outputs()], {inp.name: x})
            if "CUDAExecutionProvider" not in sess.get_providers():
                raise RuntimeError(f"会话未使用 CUDA: {sess.get_providers()}")
        result["ok"] = result["cuda"]
    except Exception as e:  # noqa: BLE001 — DLL 缺失/驱动问题等都要上报而非崩溃
        result["error"] = f"{type(e).__name__}: {e}"
    print(_json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


def cmd_selftest(args):
    """打包自检：验证惰性 import 的运行库都随包（CI 构建环境漂移防线）。

    requirements.txt 漏包时 PyInstaller 只对 hiddenimports 警告不报错，打出的
    exe 运行期才炸（v0.1.18 组件安装 No module named 'py7zr' 事故）。release
    workflow 打包后跑本命令，缺库立即红。
    """
    import json as _json

    mods = ["py7zr", "onnx", "onnxconverter_common", "numpy", "psutil"]
    bad = []
    for m in mods:
        try:
            __import__(m)
        except Exception as e:  # noqa: BLE001 — 任何导入失败都要上报
            bad.append(f"{m}: {type(e).__name__}: {e}")
    print(_json.dumps({"ok": not bad, "missing": bad}, ensure_ascii=False))
    return 0 if not bad else 1


def main():
    # Windows 管道/重定向下 stdout 默认走系统 locale（GBK）：worker 中文事件行会以
    # GBK 字节到达 runner（PyInstaller frozen 无视 PYTHONIOENCODING，实测环境变量
    # 改不动内嵌解释器），sidecar 转发时炸 UnicodeEncodeError——全子命令统一 UTF-8。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    ap = argparse.ArgumentParser(prog="sv", description="super_video CLI (M0)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("probe", help="探测媒体信息")
    p.add_argument("input")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("gen", help="生成合成测试视频")
    p.add_argument("output")
    p.add_argument("-w", "--width", type=int, default=640)
    p.add_argument("--height", type=int, default=360)
    p.add_argument("--duration", type=float, default=10)
    p.add_argument("--fps", type=int, default=24)
    p.set_defaults(func=cmd_gen)

    p = sub.add_parser("models", help="模型管理")
    p.add_argument("action", choices=["list", "download", "pin"])
    p.add_argument("model_id", nargs="?")
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("run", help="超分一个视频")
    p.add_argument("input")
    p.add_argument("-m", "--model", required=True)
    p.add_argument("-s", "--scale", type=int, default=None)
    p.add_argument("-o", "--output", default=None)
    p.add_argument("--codec", default="h264", choices=["h264", "h265"])
    p.add_argument("--crf", type=int, default=18)
    p.add_argument("--preset", default="medium")
    p.add_argument("--tile", type=int, default=0)
    p.add_argument("--device", default="auto", choices=["auto", "cpu"])
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("serve", help="启动 sidecar 服务")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8730)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("worker", help="[内部] 执行一个任务（打包模式复用入口）")
    p.add_argument("task_id")
    p.set_defaults(func=cmd_worker)

    p = sub.add_parser(
        "ort-check", help="[内部] ORT 后端探测（组件激活后 CUDA/TRT 应在列）")
    p.add_argument("--session", default=None,
                   help="可选：用该模型创建 CUDA 会话并跑一帧（真验证）")
    p.set_defaults(func=cmd_ort_check)

    p = sub.add_parser("selftest", help="[内部] 打包自检：惰性导入的库是否都随包")
    p.set_defaults(func=cmd_selftest)

    args = ap.parse_args()
    if not getattr(args, "model_id", True) and args.cmd == "models" and args.action != "list":
        ap.error("models download/pin 需要模型 id")
    try:
        rc = args.func(args)
    except PipelineError as e:
        console.print(f"[red]管线失败:[/red] {e}")
        sys.exit(1)
    if rc:  # 子命令返回码必须传导（selftest 缺库要红掉 CI；worker 自带 sys.exit）
        sys.exit(rc)


if __name__ == "__main__":
    main()
