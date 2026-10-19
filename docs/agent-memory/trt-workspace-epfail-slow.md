---
name: trt-workspace-epfail-slow
description: TRT workspace 上限不足 → 引擎构建 EP_FAIL 静默回落 CUDA 慢 40~70 倍；4GB→10GB 已修（410e0ae）；providers 显示 TRT ≠ 真跑 TRT
metadata:
  node_type: memory
  type: project
  originSessionId: sess_a3398c1f-1992-48ff-95b5-e12ddab90214
---

2026-09-12 用户实测 realesrgan-x4plus-anime 单张 891×1280（输出 18.2M 像素）图片超分总用时 190.3s（引擎加载 160.3s + 推理 29.15s），追问「为什么这么长」。根因：该全图尺寸下 TRT builder 需求超 `trt_max_workspace_size` 4GB 上限，引擎构建失败（stderr `EP_FAIL: failed to create engine from network`），**ORT 静默把节点回落 CUDA EP 执行——providers 列表仍显示 TensorrtExecutionProvider，程序与用户都无从察觉**；加载大头=TRT 构建失败重试×2（主图+u8 包装图）+ u8 包装 A/B 校验的多次全尺寸 CUDA 慢速推理。数字：CUDA 回落 16.9~29s/张 vs TRT 建成后 0.4s/张（40~70 倍）；修复后首次 ~52s 一次性构建，同尺寸二次任务引擎缓存命中 0.9s 加载 + 0.4s/张。修复 `410e0ae`（v0.5.0 tag 之后，随 [[trt-fp16-nan-black-images]] 125379a 一起待下一版）：workspace 4GB→10GB——只是 builder 选 tactic 的上限、按需分配不实际占满；512 分块 + 4GB 也能建成（0.6s/张，构建仅 17.6s）可作备选。

**Why:** TRT「providers 显示了 ≠ 图真跑在 TRT 上」；视频任务一直没事是分段帧尺寸没大到触发构建失败，18.2M 输出是这台 5080 第一次全图大输出。排障慢任务先查数据根 `.tmp\trt_cache\` 有没有新增 `.engine` 文件——没建就是静默回落了（该次任务时间窗内零新文件即实锤）。

**How to apply:** 大图全图（≥18M 输出像素）再撞 EP_FAIL 时优先想分块而非继续加大 workspace；同尺寸首任务慢是正常摊销（每形状一次性构建）。与 [[trt-fp16-nan-black-images]]（同日另一 TRT 坑：fp16 NaN 黑图）互为姊妹案例，排障环境（.venv-cuda + 组件 DLL）见该条。
