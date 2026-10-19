---
name: cugan-dml-leak-resolved
description: CUGAN×DML 逐帧显存泄漏(0x887A0006 设备摘除)已定论并落地产品围栏：DML 禁包装+主线程内联+worker 拒绝，TRT/CPU 可用
metadata:
  node_type: memory
  type: project
  originSessionId: sess_e3931df5-502a-4c1c-a3a4-22785930fc75
---

2026-08-27 深夜定论（曾立案 cugan-pipeline-crash-open，现已解决）：

**根因**：Real-CUGAN 权重在 DirectML 下逐帧显存泄漏，泄漏量∝输出像素——
1080p→4K 第 8 帧（BASIC 优化）/~30 帧（ENABLE_ALL）必触发 0x887A0006
设备摘除，全 GPU 会话连坐挂死/段错误；540p 阈值更高但终会爆。与线程/包装/
预热/COM/会话选项/内容全无关；CPU 与 DML 输出还语义级分歧（maxdiff 0.76）
使跨 EP 校验不成立；CUDA EP 同样挂。其他模型全部正常。TRT 下 CUGAN 健康
（端到端 14.9fps）。

**产品围栏（已落地，182 pytest 全绿）**：real-cugan.json u8_wrap=false +
onnx_engine CUGAN×DML 自判禁包装/main_thread_only 主线程内联 +
worker DML/CUDA 建 CUGAN 任务直接拒绝并提示走 TRT/CPU。

**2026-09-03 补充**：worker 报错曾引导「显式切换到 CPU 后端」，但 settings
白名单/设置页只有 auto|cuda|trt|directml——死路。v0.4.4 批次闭环：engine
白名单加 cpu，engine_select/worker（ort_device="cpu"→CPUExecutionProvider）/
设置页 radio/backendText 全链路支持，报错文案从此可达。

**Why**: DML 默认引擎下用户点 CUGAN 任务会跑到一半进程崩溃（发布版同样中招，
v0.2.6 即带此病），必须让"不可用"变成"明确不可用"而不是"跑一半炸"。
**How to apply**: 复测入口 bench_e2e_models.py（DML 应 skip、TRT 应 ok）；
未来若 DML 驱动/ORT 大版本升级可移除围栏，判定条件=1080p 连续 40+ 帧稳定。
BENCH.md §13 有完整证据链。关联 [[model-broadening-and-trt]] [[trt-u8wrap-enablement]]
