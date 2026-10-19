---
name: engine-probe-hardcoded-model-bug
description: v0.4.0 起 CUDA/TRT 探测必败「缺少校验用模型」恒回落 DirectML——换血移走硬编码探测模型连带雷；已修（e638f5d）随 v0.4.5 发布；修复生效后 TRT 真跑暴露 MangaJaNai CUDA 崩溃（后继见 trt-cuda-resize-mangajanai）
metadata:
  node_type: memory
  type: project
  originSessionId: sess_f9e027fd-a96b-402a-bf3c-a90f940f6206
---

2026-09-03 用户装机报「当前后端 DirectML · 组件探测失败，回落 DirectML（缺少校验用模型）」。根因：[[bundled-models-v31-swap]]（commit 47990e1）把 RealESR-AnimeVideo-v3_x4.onnx 移出 bundled，而 `engine_select.py` 的 `_probe_cuda`/`_probe_frozen` 两处**硬编码**该文件名当探测校验模型。触发链=engine 设 trt/cuda 且 TRT 组件已装 → 组件探测找不到校验模型 → select_engine 回落 DirectML。

**影响范围（比文案严重）**：v0.4.0～v0.4.5-preview.1 全部安装版，engine=trt/cuda + 组件已装的用户**任务实际全在 DML 跑**（装了 TRT 白装，哑巴速度损失）。用户此前正常是因为 v0.3.x 安装包还带旧模型。

**修复（commit e638f5d，随下版发布）**：①新增 `_probe_model()` 动态取 `sorted(BUNDLED_DIR.glob("*.onnx"))[0]`（探测只需建真会话跑一帧，任何权重够格，未来换血不再连带坏）；②第二颗潜伏雷同修——`scripts/check_cuda.py` 与 `cli.py ort-check --session` 固定喂 float32 张量，V3.1 是原生 fp16，模型在位也会 type mismatch 误报，改为按会话 `inp.type` 选 dtype（本机 DML+V3.1+64x64 fp16 实测跑通）；③顺手清掉 test_engine_backends 指向已删文件的死常量 BUNDLED；+1 回归 `_probe_model`（命中现存/空目录 None）。

**Why**: 换 bundled 模型集时没人 grep 硬编码文件名引用——探测脚本、bench/calibrate 脚本里这类引用是连带雷高发区。
**How to apply**: 以后动 bundled 集（增/删/换）先 `grep -rn <被移除文件名>` 全仓；探测/自检类脚本喂张量一律按 `sess.get_inputs()[0].type` 定 dtype，勿写死 float32。修复已随 v0.4.5（2026-09-03 转正）发布——v0.4.0～v0.4.5-preview.1 的存量安装版升级 v0.4.5 即恢复 TRT/CUDA。
