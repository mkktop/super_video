---
name: queue-reorder-and-resume
description: 队列拖拽排序与 GAN 断点续跑的实现位置与关键约定
metadata:
  node_type: memory
  type: project
  originSessionId: sess_464de919-6a51-497e-9a75-eb908ebc5338
---

2026-08-24 新增两个功能：①队列拖拽排序 ②ONNX(GAN) 任务断点续跑。

- 排序：tasks 表新增 queue_order 列（迁移回填 created_at）；`POST /api/tasks/reorder` 只重排 queued 子集；list_tasks 顺序 = running → queued(按 queue_order) → 其余按 created_at DESC（前端不再 reverse）。
- 续跑：ONNX 任务统一走 `backend/sv/pipeline/segmented.py` 分段管线（每段 = StreamPipeline 子运行：`-ss` 输入 seek + `-frames:v` 限帧 + 无音轨编码），checkpoint 在 `.tmp/segmented/<task_id>/checkpoint.json`（`{"done":[起始帧索引]}`，原子写），全部段完成后 concat + 源音轨合成最终文件（-c:v copy）。分段数自适应 8 段（60~600 帧/段）。
- `POST /api/tasks/{id}/resume`：仅 failed/canceled 可续；无工作目录但有进度 → 409"续跑数据已不存在"（从未开始的进度=0 允许从头重跑）；torch 任务无目录直接从头（解码幂等）。
- 图片系任务（image/manga，2026-09-24 起）不走分段目录：断点=逐页产物 mtime≥created_at，resume 端点对其豁免输出删除与 segmented 守卫，详见 [[manga-sr-standalone]]。
- 已知取舍：VFR 源续跑段边界 ±1-2 帧；补帧任务段间末帧复制补足 2N。
- 测试：test_segmented.py（含续跑跳过用 CountingNearest2x 验证推理帧数）+ test_server.py 的 reorder/resume 状态机。

**Why:** 用户要求对标 Video2X 类工具补齐这两个体验短板；续跑机制与 torch 的 chunked.py 同构。

**How to apply:** 改续跑先看 segmented.py 与 chunked.py 的一致性；改排序注意 next_queued 与 list_tasks 都依赖 queue_order；前端拖拽逻辑在 Tasks.vue（原生 HTML5 DnD，无第三方库）。
