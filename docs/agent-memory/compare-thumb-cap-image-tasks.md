---
name: compare-thumb-cap-image-tasks
description: 图片任务静帧对比曾喂 960p 预览缩略图致分割线两侧无差异；已改 file:// 全分辨率首对（49040c4）
metadata:
  node_type: memory
  type: project
  originSessionId: sess_a3398c1f-1992-48ff-95b5-e12ddab90214
---

2026-09-12 用户报「静帧对比两张图根本没重叠比较」+ 前一日「成片显得不清晰、单独打开格外清晰」。像素法医（截图块与 previews 文件模板匹配 + 接缝相关）证明两层**叠合是对齐的**；真病因：Compare.vue 任务对比页给滑块的是 `previews/{id}*.jpg` 960px 缩略图对——源 891 与成片 3564 都压进同一张 668×960，高频细节被下采样抹平，拖分割线无差异。

**Why:** 缩略图口径下「1:1 像素舞台」名存实亡（1:1 只是缩略图像素）；CompareModels 页用 compare 资产（src_still 原图 + still/<mid> 全分辨率 PNG）所以正常——同机两页表现不同即指纹。

**How to apply:** done 态图片任务取 `params.images` 首对 → `mediaSrc()` file:// 直读原文件+成品（无 out 字段/mock 或未完成回落缩略图）。排障工具箱：接缝两侧低相关在漫画高频内容上属正常（同图相邻列也 0.2~0.4），别据此误判错位；模板匹配用文件级缩略图多尺度搜索可定位渲染层喂的是什么资产。
