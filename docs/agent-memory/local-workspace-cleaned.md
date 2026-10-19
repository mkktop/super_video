---
name: local-workspace-cleaned
description: 2026-09-04 应用户要求清理本地工作区约 9.4GB（TRT 组件 staging+成品 7z、sidecar dist、.tmp 大件、dist-app 旧包）——本地打包/TRT 改包前需重建的约定
metadata:
  node_type: memory
  type: project
  originSessionId: sess_f9e027fd-a96b-402a-bf3c-a90f940f6206
---

2026-09-04 用户问「本地环境有什么冗余可删」，审计后经确认删除约 **9.4GB**：

- `backend/dist/` **全清**（5.2G→4K）：TRT 组件 staging（2.9G，内嵌 Python+解包 ORT）、TRT 成品 7z（约 2G：trt-core 1.3G + 各架构 builder，**已上传在 GitHub release tag `runtime-v1`，用户随时可下载**）、PyInstaller sidecar 产物（156M）。
- `.tmp/`（3.1G→53M）：modelscope_cache（1.9G 同步脚本缓存）、model_import（619M 导入测试副本）、u8_wrap（238M 缓存，按模型×后端自动重建）、vv-dml（226M 实验 venv）、dat2（DAT2 排障复现件）、散落测试 mp4/db。
- `dist-app/`（1.1G→空）：0.2.12 时代旧安装包 + win-unpacked。

**刻意保留**：`models_store/` 2.1G（用户拍板留——漫画路径密集测试期，mangajanai 962M）、`bin/`（ffmpeg dev 必需）、`.venv`、`samples/`、`backend/sv/models/bundled/`、`trt-runtime/`（dev 的 TRT 组件安装）、`compare/`、`sidecar.db/.log/.token`。

**Why**: 这些全是可重建产物，但清掉后"直接可用"变"要先构建"——未来会话若不知道会踩空。
**How to apply**: ①本地打安装包（`pnpm dist`）前必须先 `pyinstaller sidecar.spec` 重建 `backend/dist/sidecar`（CI 不受影响，自建）；②要改/重传 TRT 组件包时用 `backend/scripts/build_trt_component.py` 从头重建 staging 与 7z（成品在 runtime-v1 release 上仍在，见 [[trt-component-installer]]）；③u8 包装缓存下次推理自动重建，首次慢几秒属正常。
