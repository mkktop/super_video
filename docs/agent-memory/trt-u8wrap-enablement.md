---
name: trt-u8wrap-enablement
description: TRT 下 u8 包装曾全线被拒的根因与已落地修复（尾链 u8 边界化+容差分档），修后 TRT 全模型反超 DML
metadata:
  node_type: memory
  type: project
  originSessionId: sess_e3931df5-502a-4c1c-a3a4-22785930fc75
---

2026-08-27 基准发现的「TRT 不生效」根因与修复（均已改入 dev 工作区，随下次发版带上）：

1. ORT-TRT 导入器禁止 UINT8 中间张量 → 旧包装尾链 `Cast(u8)→Gather(通道反转)` 被
   ModelImporter 拒绝。修复：Gather 反转挪到浮点域、u8 只留图边界（`u8_wrap.py`），
   数学逐位等价。
2. A/B 校验门 ≤1 是 DML 同源口径；TRT 包装跳过尾部 f16 输出量化、常态差 1~3 且
   方向无害（更贴近 fp32 真值）→ `onnx_engine._validate_u8` 容差分档：Tensorrt ≤3，
   其余维持 ≤1。

**Why**: 不修的话 TRT 只对少数天然无 flip 尾链的 fp16 原生模型（AnimeJaNai V3 HD L1/L2）
有收益，其余模型落回 CPU 前后处理瓶颈反而比 DML 慢数倍。
**How to apply**: 复测入口 `backend/scripts/bench_all_models.py --device trt`
（纯推理）/`bench_e2e_models.py`（真管线端到端）；960x540 口径下 v3x4 98.6→6.8ms、
CUGAN x2 33.4→13.4ms 等全线点亮数据在 BENCH.md 第 12/13 节。改 u8_wrap 尾链时
务必同步 `rm -rf .tmp/u8_wrap`（缓存不含生成器版本号）。关联 [[uint8-graph-wrap-speedup]]
[[model-broadening-and-trt]] [[perf-numbers-verify-with-elapsed]]
