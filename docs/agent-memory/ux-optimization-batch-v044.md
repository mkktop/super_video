---
name: ux-optimization-batch-v044
description: 2026-09-03 五轮优化批次（R1a→R5）已提交并随 v0.4.5-preview.1 发布到预览通道；关键实现约定与剩余待办
metadata:
  node_type: memory
  type: project
  originSessionId: sess_f9e027fd-a96b-402a-bf3c-a90f940f6206
---

2026-09-03 大批次（先全面调研后按序实施，19 文件改动 + 1 新测试文件）。**已提交并发布**：功能 commit `140d973` + 发布材料 commit `48675fb`，tag **v0.4.5-preview.1** 已推送（预览通道首发，正式版转正等 v0.4.5）。版本三方（package.json/sv__init__/tag）= 0.4.5-preview.1。

**R1 数据安全**：删除二次确认（TaskCard 任务卡/Models 删除权重/NewTask 预设 ×，均弹确认）；输出覆盖预检——`POST /api/tasks` 与 `/api/trim` 新增 `overwrite` 字段，显式 output 撞「已存在文件」或「排队/运行中任务将写入的路径」→ 409，前端弹「覆盖并继续」确认后带 overwrite=true 重交（App.vue 补了 NDialogProvider；api.ts 加带 status 的 ApiError 类）；自动命名把活动任务输出也计入避让（`_active_output_keys` 播种，堵 A 没跑完 B 抢同名；NewTask defaultOutputName 前端镜像同口径含 _2 序号）；settings engine 白名单加 `cpu` 全链路（engine_select/worker/Settings UI，SV_ENGINE=cpu 亦生效，见 [[cugan-dml-leak-resolved]]）。

**R2 任务搜索+批量**：`GET /api/tasks?q=`（路径/输出/模型 LIKE，搜索时历史上限放宽 300）；`POST /api/tasks/batch`（action=cancel|delete|resume，内部逐个复用单任务端点校验，部分成功语义返回 failed 明细）；Tasks.vue 搜索+多选批量操作条。**关键约定：搜索结果显示在页面本地 searchList，绝不写入 store.tasks**——侧栏任务徽标/首页依赖全量列表，污染会让计数缩水；WS 更新按 id 就地合入 searchList。

**R3 复用对比基建的接线**：CompareModels 单模型可跑（原 ≥2）+「总览」grid 视图（源+各模型同帧并排，点卡进分割线）；NewTask 新增「▶ 先试跑 20 秒」（吸底操作条，跳 mcompare 并预填 pendingCompare+pendingModel+pendingScale，consumePendingCompare 已扩展消费模型预选）；ImageSR 缩略图解码失败兜底。图片任务结果对比链路核实本就通（worker 图片路径也产 preview，TaskCard 全页对比可用）。

**R4 视觉轮**：App.vue 建全局 `--sv-*` CSS token 层（:root），仅 Sidebar/框架迁移，**存量页面 scoped hex 未迁（待后续轮次）**；页面切换淡入动画（v-if 链改 `<component :is>` 动态页——KeepAlive 内 v-if 链外层 Transition 感知不到；Transition 包 KeepAlive、**不能加 :key**（会破坏 include 名字匹配缓存））；通用 .sv-skeleton shimmer + NewTask probe 卡骨架。

**R5**：probe 响应透出 bit_depth/vfr/field_order，NewTask 信息卡加黄色媒体属性标签（隔行建议反交错等）。

**Why**: 会话对项目做完整体评估后用户圈定的五轮优先级清单，全部按序落地后用户拍板发预览通道。
**How to apply**: 验证基线=后端全量 pytest 361 passed+2 skipped（新 test_tasks_search_batch.py 5 例）、vue-tsc node/web 双绿、pnpm build 绿；写新 API 测试若用固定输出路径须带 overwrite:true（覆盖预检使二次运行 409，test_server/test_trim/新测试 sb_out rmtree 均按此适配）；剩余待办=存量 scoped hex 迁 token、Home/模型市场骨架屏、模型市场多权重卡体验（见 [[model-marketplace-multiweight-cards]]）。

**v0.4.5 已于 2026-09-03 转正发布**（tag v0.4.5，commit 8ca920b）：preview.1 全部内容 + 两个后续修复（多权重下载进度条 ceb0c32、CUDA/TRT 探测必败 e638f5d）+ 用户的 45b1dc1（引擎失败堆栈落 sidecar.log）都在本版；R2 latest.yml 随转正恢复正式版覆盖。**同日又发 v0.4.6**（tag，commit 3f42cf8）：收 v0.4.5 后的四笔修复——DAT2 fp16（291fb80）、对比成片 CSP 黑屏（8d13571）、图片对比资产 key（73655d0），加转正时并入的进度条/探测，RELEASE_NOTES 记五修复；本批（R1~R5）至此全部在正式版用户手里。
