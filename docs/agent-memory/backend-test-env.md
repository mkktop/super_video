---
name: backend-test-env
description: 本机跑后端测试用仓库根 .venv，项目不依赖 PyAV，测试夹具用 ffmpeg 命令行
metadata:
  node_type: memory
  type: project
  originSessionId: sess_ea43e529-a63b-4e0e-a2e3-a268ebeb83f8
---

本机跑 backend 测试：`cd backend && ../.venv/Scripts/python.exe -m pytest tests/ -q`（仓库根有 `.venv` 和 `.venv-cuda` 两个 venv，都装了 pytest/numpy/onnx；backend 下没有自己的 venv，全局 Python 无依赖）。项目**不用 PyAV**——不要拿 `import av` 探测环境；测试造视频走 `tests/test_stream.py::make_video` 的 ffmpeg 命令行（`ffmpeg_bin()`）。TEMP_DIR 无 SV_DATA 时=仓库根 `.tmp`。全量约 85 秒。相关：[[only-own-tests-scope]]
