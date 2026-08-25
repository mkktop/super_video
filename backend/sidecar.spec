# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec: sidecar（FastAPI+onnxruntime-directml，onedir）
# 用法: cd backend && ../.venv/Scripts/pyinstaller.exe sidecar.spec
import sys
from pathlib import Path

HERE = Path(SPECPATH)

a = Analysis(
    ["cli.py"],
    pathex=[str(HERE)],
    binaries=[],
    datas=[
        # 注册表 manifest 与 bundled 模型权重随包分发
        (str(HERE / "sv" / "models" / "registry_json"), "sv/models/registry_json"),
        (str(HERE / "sv" / "models" / "bundled"), "sv/models/bundled"),
        (str(HERE / "sv" / "server" / "presets.json"), "sv/server"),
    ],
    hiddenimports=[
        "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on",
        "py7zr", "py7zr.callbacks", "py7zr.py7zr",
        "sv.engines.rife", "sv.engines.torch_engine",
        # u8 图手术 / fp16 转换是惰性 import，静态分析可能漏收
        "onnx", "onnxconverter_common",
    ],
    hookspath=[],
    excludes=["tkinter", "matplotlib", "torch", "IPython", "jedi"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="sidecar",
    debug=False,
    console=True,
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="sidecar",
)
