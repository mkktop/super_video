---
name: dml-bigimage-silent-garbage
description: DML 大尺寸全图静默出垃圾图（fp16 灰雾/fp32 近黑，不报错）；彩色探针+512分块降链已修（16c9447）
metadata:
  node_type: memory
  type: project
  originSessionId: sess_a3398c1f-1992-48ff-95b5-e12ddab90214
---

2026-09-12 用户实测 realesrgan-x4plus-anime 1500×1078 在 DML（后端=自动）出整幅灰雾（值域 92~132、std 1.3）。本机复现扩展：**与精度无关**（fp16 转换件→灰雾、fp32 原件→近黑 max=4）、**尺寸相关**（480×360 全健康）、**秒级安静返回不报错**。512 分块即救（2.1s 且比全图更快）。fp16.py 的 CPU 建会话校验只拦类型不一致（[[fp16-conversion-load-validation]] 的 DAT2 案），数值损坏从未有校验层。修复 `16c9447`：`_load_onnx_engine` 探针升级为确定性彩色渐变帧（均值<8 拦黑图、std<6 拦色彩塌缩），垃圾降链 512 分块→fp32 原件→关 TRT fp16→auto→cpu。

**Why:** 「输出灰/黑但任务成功」在 2026-09-12 一天连爆三案（TRT fp16 NaN 黑图、TRT workspace 慢、DML 大图垃圾），共性=引擎会话能建、run 不抛、垃圾静默落盘——任何新后端/新模型都可能再犯，探针层是通用防线。

**How to apply:** 排障灰图先看 sr_logs 的 provider+精度+尺寸组合，本地三步复现法（小帧 vs 真实尺寸、fp16 vs fp32、分块 vs 全图）见 16c9447 提交信息；姊妹案 [[trt-fp16-nan-black-images]]、[[trt-workspace-epfail-slow]]。
