---
name: v0-2-4-review-fixes
description: 2026-08-26 全面审查修复（v0.2.4）完成内容与两项拍板取舍，勿重复提出
metadata:
  node_type: memory
  type: project
  originSessionId: sess_e3931df5-502a-4c1c-a3a4-22785930fc75
---

2026-08-26 对 super_video 做了全面代码审查并修复全部问题（版本 bump 到 0.2.4，未打 tag）。全量测试 156 passed / 1 skipped（仓库根与 backend 目录均绿）。修复清单：RIFE 通道序（RGB 契约+32 对齐）、batch 尾批丢帧+段帧数校验、fp16 原子写、localhost token 鉴权（SV_TOKEN env + .tmp/sidecar.token 双源）、sidecar 复用优先于 reap、initStore 容错、release pytest 门禁+版本三方校验、依赖上下界 pin、测试体系 SV_DB 模块隔离、下载器失败路径测试、双路并行最小 E2E（含取消连坐）。

**两项取舍已经用户拍板「保持现状」，代码注释与 backend/README「已知取舍」小节有记录，勿再作为问题提出：**
1. 前端 `webSecurity: false`（file:// 直读视频；自定义协议有 moov 黑屏历史坑）
2. ONNX 路径 astype 截断 vs torch round 的 ≤1/255 量化差异（截断与 u8 包装图内 Cast 一致是刻意的）

**Why:** 避免未来会话重复审查时把已拍板的取舍再翻出来，或误改有 A/B 校验依赖的量化行为。

**How to apply:** 涉及 webSecurity/自定义协议或引擎量化行为时，先看 backend/README.md「已知取舍」与对应代码注释；测试新增注意 `-m sv.server.worker` 子进程需要 PYTHONPATH=backend（test_parallel.par_task 有对齐 runner env 的写法）。
