---
name: ui-fullscreen-layout-feedback
description: 用户按全屏宏观布局评判 UI；限宽居中=「挤在中间」差评；改动要铺满宽度且一眼可见
metadata:
  node_type: memory
  type: feedback
  originSessionId: sess_47ac3143-b4a6-445d-ba79-34e058291244
---

对设置页做了一轮内容级微调（后端状态行/彩色标签/两列网格/未保存提示）后，用户评价"我看没啥变化啊"，真正不满是 `max-width:860px + margin auto`——全屏时内容挤在中间、两侧大片空白，要求"美化"。

**Why:** 用户以全屏宽度的视觉冲击判断"改没改"；文案/标签级微调不 register，布局级（铺满、多列、明显结构变化）才算数。

**How to apply:** 页面容器一律 `width:100%` + 响应式网格铺满（设置页用 `repeat(auto-fit, minmax(460px,1fr))`，中宽两列/超宽三列/窄窗单列），不要给内容列 max-width+居中；交付 UI 改动前先想象 1920 全屏下的观感，微调+布局混合改动要在汇报里把布局变化说清楚。
