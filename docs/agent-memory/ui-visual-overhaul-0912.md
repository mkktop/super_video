---
name: ui-visual-overhaul-0912
description: 2026-09-12 UI 视觉大批次落地：全语义 token 体系+浅色主题+Ctrl+K 命令面板+方向过渡+像素 Hero（已随
  v0.5.0 发布）；纯 UI 状态必须新起文件 uiFx.ts/theme.ts
metadata:
  node_type: memory
  type: project
  originSessionId: sess_a3398c1f-1992-48ff-95b5-e12ddab90214
---

2026-09-12 用户给出成体系 UI 优化需求书（P0 视觉统一/P1 交互增强/P2 特色 + 9 条硬约束 + 验收清单），P0 全部 + P1 全部 + P2-1（像素重构 Hero）/P2-2（浅色主题）已落地，**未 commit、未发版**。pnpm build 零报错、vitest 24 绿、11 页深浅两主题截图走查通过。

**结构（后续改样式必须沿用，勿再散落 hex）：**
- App.vue `:root` 全语义 token：间距 `--sv-space-1..6`、圆角 `--sv-radius-sm/md/lg`、动效 `--sv-ease`(回弹)/`--sv-dur-fast/normal`、状态色柔和底 `--sv-*-bg`、`--sv-*-rgb` 三连（供 rgba(var(--sv-accent-rgb),a) 派生）；浅色用 `[data-theme='light']` 覆盖同组 token。全局工具类 `.sv-card`(面板渐变底+1px 内嵌高光，`.hoverable` 才有悬浮上浮)与 `.sv-num`(tabular-nums+-0.02em)。App.vue 末尾有全局 `prefers-reduced-motion: reduce` kill-switch。
- 硬约束「纯 UI 状态新起文件」的落点：`src/renderer/src/uiFx.ts`（pageDir+navDirection，Sidebar 点击写方向→App.vue `<Transition :name>` 方向感知过渡）、`src/renderer/src/theme.ts`（sv-theme localStorage 三态 dark/light/system，matchMedia 跟随，**模块加载即 apply** 保证首帧前 data-theme 就位）。store/api/composables 一行未动。
- 新组件：CommandPalette.vue（Ctrl+K，**挂 App 根部不在 KeepAlive 内**→无重复监听；keydown 用 capture 且面板开着时吞按键，防穿透到模型对比页数字键/分割线方向键）；EmptyState.vue（film/cube/log/compare 四种 ≤30 行内联 SVG，接入 Tasks/Models/Logs）。
- TaskCard：完成微庆祝=watch 状态 diff（running→done）播一次 tag 弹跳+卡片一次性成功色描边扩散，950ms 后自清。

**设计拍板：** Hero/显卡/处理器/内存卡是「品牌氛围岛」，**两种主题都保持深色画布**（token 单列 `--sv-hero-grad/--sv-gpu-grad/--sv-cpu-grad/--sv-mem-grad` 及岛内固定色，不进浅色映射）——浅色审查确认这样才有视觉焦点不违和。

**技术事实（复用价值高）：**
- naive-ui NProgress 渐变 stops 传 `'var(--sv-accent)'` 字符串照常渲染；SVG stroke/fill/color 全接受 CSS var()——TrendChart/PerfRings 系列色已全 var 化，随主题翻转。
- Home 像素 Hero 用 `@property --scan` 注册 `<percentage>` 自定义属性驱动 clip-path/位移，GPU 合成不触发布局；reduced-motion 退化静态 50% 对比。
- 浅色走查后微调：light `--sv-text-faint=#6d7480`（faint 文字对比度）；TaskCard 排队脊线 `var(--sv-text-faint)` + opacity .55（原 `--sv-border` 在白卡上不可见）。

相关：[[user-iterative-ui-polish]] [[browser-preview-harness]] [[optimization-batch-v048]]
