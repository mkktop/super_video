---
name: titlebar-use-real-app-icon
description: 标题栏等界面品牌位要直接复用桌面图标资产，用户否掉了临时手绘 mark
metadata:
  node_type: memory
  type: feedback
  originSessionId: sess_2a514bfa-ab69-4963-ad2b-7c49c4d2aac1
---

2026-08-27 优化 TitleBar 时我先手绘 SVG mark，又出六款无箭头图形让用户挑，用户反问"为什么不用桌面图标"。

**Why:** 桌面图标是定过稿的品牌资产（[[icon-pipeline]] 的 B 方案：斜切蓝青底＋播放三角＋星芒），界面再自创一套 mark 既突兀又割裂；遇到品牌图形需求先找现有资产，而不是重新设计。

**How to apply:** 渲染进程经 `src/renderer/src/assets/logo.png`（`build/icon.png` 的副本）引用；两处需同步更新，TitleBar.vue 内有注释标注来源。
