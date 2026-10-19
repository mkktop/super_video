---
name: user-iterative-ui-polish
description: 用户逐元素提 UI 美化需求、偏爱炫酷/活数据效果；预览页截图走查是主要迭代回路
metadata:
  node_type: memory
  type: feedback
  originSessionId: sess_a21cd062-2734-4355-9385-abc5fb9eb0a2
---

2026-09-04 UI 升级会话中用户连发四轮逐元素需求：整站美化→显卡卡「炫酷点」→处理器卡「炫酷点」→引擎状态胶囊「显得很丑」，全程开着 pnpm preview 浏览器实时看效果，问一句改一处。

**Why:** 用户审美偏爱显性效果——金属渐变文字、流光扫过、呼吸辉光、实时数据条（接 perf 采样的显存/CPU 占用条）都获正面反馈；对排版混乱和原始技术代号敏感（「就绪 · trt · NVIDIA GeForce RTX 4070 Ti」一行被点名丑）。配合 [[ui-fullscreen-layout-feedback]]（宏观布局铺满）与 [[browser-preview-harness]]（自查工具）。

**How to apply:** 前端美化优先加"活"的可视化（接 perf.latest 的实时条/动效）而非仅调配色；文案不露原始代号（trt→TensorRT 映射、显卡名去 NVIDIA GeForce 前缀取短名）；动效有层级主角（流光只留给被点名的卡，配角用静态渐变避免满屏动）；交付前先在 preview 截图自查；只动被点名元素+强相关邻位（顺手把同排不配套的卡补齐被接受）。

2026-09-12 用户升级为成体系需求书：P0/P1/P2 分级 + 9 条硬约束（不动功能逻辑/零重依赖/性能红线对比页禁 backdrop-filter 与 blur/KeepAlive 监听守卫/tabular-nums/新色必进 token/aria 与焦点环/build 零报错）+ 验收清单，并强调**克制原则**（微庆祝只闪一次、不撒花、不出声；装饰动画包 prefers-reduced-motion）。结构化大需求可一次全量交付，不必逐元素等确认；落地见 [[ui-visual-overhaul-0912]]。
