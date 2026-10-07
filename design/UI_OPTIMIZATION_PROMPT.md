# super_video 前端 UI/UX 优化任务提示词

> 本文档是交给执行 AI 的完整任务说明。你（执行 AI）将在 `D:\work\super_video` 仓库中工作，
> 目标是把这套已经"能用且不难看"的深色 UI，打磨成一套**有记忆点、有质感、交互顺滑**的桌面软件界面。
> 请通读全文后再动手，严格按"硬约束"执行，按 P0 → P1 → P2 的顺序逐项实施。

---

## 0. 项目背景（先读，避免瞎改）

**super_video** 是一款 Windows 桌面端 AI 视频超分辨率软件（Electron 43 + Vue 3 + TypeScript + naive-ui 深色主题），
前端代码在 `app/src/renderer/src/`，后端是 FastAPI sidecar（**不在本次范围内，禁止改动**）。

页面与组件清单（全部单文件组件，样式多为 scoped）：

| 文件 | 作用 |
|---|---|
| `App.vue` | 根组件。**全局设计 token（`:root` 下的 `--sv-*` CSS 变量）+ 全局样式都在这里** |
| `components/TitleBar.vue` | 自定义标题栏（40px，拖动区、logo、版本、更新提示、窗口控制） |
| `components/Sidebar.vue` | 左侧导航（196px，10 个页面入口 + 徽标 + 底部迷你性能指示） |
| `components/TaskCard.vue` | 任务卡片（状态脊线、进度条、预览图、操作按钮） |
| `components/CompareSlider.vue` / `VideoCompare.vue` | 分割线擦除对比组件 |
| `components/PerfRings.vue` / `TrendChart.vue` | 性能圆环 / 趋势图（SVG 手绘，非图表库） |
| `pages/Home.vue` | 首页（Hero、运行状态、四宫格统计、硬件信息） |
| `pages/NewTask.vue`（1182 行）| 新建任务三步向导（最复杂的表单页） |
| `pages/Tasks.vue` | 任务队列（批量选择、拖拽排序） |
| `pages/Models.vue` | 模型市场 |
| `pages/Trim.vue` / `ImageSR.vue` / `CompareModels.vue` | 剪切 / 图片超分 / 模型对比（**KeepAlive 常驻页**） |
| `pages/Compare.vue` | 全页对比（无侧栏全屏布局，`page-full`） |
| `pages/Perf.vue` / `Logs.vue` / `Settings.vue` | 性能 / 日志 / 设置 |

**现状设计基调**（已具备，不要推倒重来）：
- 深色单主题：底色 `#0e1014`，面板 `#181b21`，主色 `#4F8CFF`，品牌渐变 `linear-gradient(135deg, #4f8cff, #8b5cf6)`
- 已有设计 token 体系（`--sv-bg` / `--sv-panel` / `--sv-accent` / `--sv-grad` 等），但**存量页面里还有大量硬编码 hex**，迁移未完成
- 已有细节：主按钮渐变柔光、任务卡状态脊线、进度条流光、侧栏激活项左侧光条、连接状态呼吸点、骨架屏 shimmer

**验证方式**（重要）：仓库支持纯浏览器预览，无需起后端：
```bash
cd app && pnpm preview   # → http://localhost:5199/preview.html
```
预览入口带 mock 数据（`src/renderer/src/preview/mock.ts`），刷新即还原。每完成一项改动都用它走查样式。

---

## 1. 硬约束（违反任何一条视为不合格）

1. **不改任何功能逻辑**：只动 template 的结构/样式与纯表现层代码。`store.ts` / `api.ts` / composables 里的数据流、WS 事件、轮询逻辑一行不碰；确需新增纯 UI 状态（如主题开关）时新起文件。
2. **不引入重型依赖**：不加 UI 框架、不加动画库（GSAP/framer-motion 等）、不加图表库。全部用 CSS transition/animation + 原生 SVG/Canvas 实现。允许新增零依赖的小工具函数。
3. **保留 naive-ui**：视觉改版通过 `App.vue` 的 `themeOverrides`、全局 CSS 与组件级 scoped 样式覆盖实现，不换组件库。
4. **性能红线**：视频对比页（Compare/VideoCompare）与任务运行中页面**禁止**使用 `backdrop-filter`、大面积 `filter: blur()`、多层 box-shadow 叠加动画——这些页面可能同时解码两路视频，GPU 预算先给视频。装饰性动画统一用 `transform` + `opacity`（GPU 合成层友好），并全部包在 `@media (prefers-reduced-motion: no-preference)` 里。
5. **KeepAlive 页面注意**：`NewTask / Trim / ImageSR / CompareModels` 是常驻页，新增的全局事件监听必须配 `onActivated/onDeactivated` 守卫，避免重复绑定。
6. **中文字体与排版**：保持 `system-ui, 'Segoe UI', 'Microsoft YaHei', sans-serif`；数字用 `font-variant-numeric: tabular-nums`（倒计时、帧率等跳变数字必须等宽，防止布局抖动）。
7. **所有新颜色进 token**：新增颜色一律在 `App.vue` 的 `:root` 加 `--sv-*` 变量再引用，禁止再写散落 hex。浅色主题变量用 `[data-theme='light']` 选择器覆盖同一组 token。
8. **可访问性**：交互元素保留 `:focus-visible` 焦点环；正文对比度 ≥ 4.5:1；所有纯装饰元素加 `aria-hidden="true"`。
9. 改动完成后跑 `cd app && pnpm build` 必须零报错；`pnpm preview` 逐页走查无样式破损。

---

## 2. P0 —— 视觉统一与质感提升（先做，收益最大）

### P0-1 设计 token 补全与硬编码颜色清零
- 把 `:root` token 扩展为完整语义层：`--sv-bg / --sv-panel / --sv-panel-2 / --sv-panel-deep / --sv-border / --sv-text / --sv-text-dim / --sv-text-faint / --sv-accent / --sv-accent-2 / --sv-success / --sv-warning / --sv-danger` 之外，补充：
  - 间距刻度 `--sv-space-1..6`（4/8/12/16/24/32px）
  - 圆角刻度 `--sv-radius-sm/md/lg`（8/12/16px）
  - 动效曲线 `--sv-ease: cubic-bezier(0.22, 1, 0.36, 1)`（回弹感）与 `--sv-dur-fast/normal`（120ms/200ms）
  - 状态色柔和背景 `--sv-success-bg` 等（10% 透明度版本，供 tag/徽标底色）
- 全局搜索 `#[0-9a-fA-F]{3,8}` 逐个替换为 token（`rgba(255,255,255,x)` 这类通用半透明可保留）。完成后各页面 scoped 样式里不应再出现状态色 hex。

### P0-2 背景氛围层升级（"暗夜工作室"质感）
现状 body 只是一条纵向渐变，偏平。改为三层叠加（全部伪元素，`pointer-events:none`，固定在 `.app` 层级而非 body，避免滚动重绘）：
1. 底层：现有纵向渐变保留；
2. 中层：左上角 `radial-gradient` 极淡主色辉光（`rgba(79,140,255,0.05)`）+ 右下角极淡紫色辉光（`rgba(139,92,246,0.04)`），**静态，不做动画**（性能红线）；
3. 顶层：2% 透明度的噪点纹理（内联 SVG `feTurbulence` data-URI，平铺 128×128），消除暗色大面积色带的 banding。
效果目标：界面像"深夜剪映/达芬奇"的工作台，有纵深但不喧宾夺主。

### P0-3 卡片体系统一
现在 `Home` 的 `.card`、`TaskCard`、`Models` 的模型卡各自为政。统一为：
- 面板卡：`var(--sv-panel)` 底 + `1px var(--sv-border-soft)` 描边 + `--sv-radius` 圆角；
- 悬浮态：`translateY(-2px)` + 描边提亮到 `rgba(255,255,255,0.12)` + 阴影 `0 10px 30px rgba(0,0,0,0.35)`，过渡 180ms `var(--sv-ease)`；
- 卡片顶部加 1px 内嵌高光（`box-shadow: inset 0 1px 0 rgba(255,255,255,0.05)`），制造"磨砂玻璃叠在深色上"的层次。
落地位置：`App.vue` 定义 `.sv-card` 工具类，各页面卡片逐步套用（旧的局部卡片样式删除）。

### P0-4 字体层级与数字排版
- 建立字号阶梯：页面大标题 22px/600、区块标题 15px/600（现状 `sec-title`）、正文 14px、辅助 12.5px；全部页面统一（现在各页 h2/stat 字号有出入）。
- 所有统计大数字（首页四宫格、fps、ETA）加 `letter-spacing: -0.02em` + tabular-nums；统计数字从 24px 提到 28px，颜色用 `--sv-text` 纯白，标签用 `--sv-text-faint`。

---

## 3. P1 —— 交互体验增强

### P1-1 页面切换过渡升级
现状是整体淡入+7px 上浮。升级为**方向感知过渡**：侧栏导航从上往下点（索引增大）时新页从下方 12px 浮入，反向则从上方 -12px 浮入（在 Sidebar 点击时把方向写进 `ui` 状态，App.vue 的 Transition 按方向动态切换 `name`）。时长 180ms/140ms，`var(--sv-ease)`。这是低成本但"高级感"最明显的改动之一。

### P1-2 任务卡运行态"活起来"
`TaskCard.vue` 运行中状态增强：
- 状态脊线从静态渐变改为**缓慢流动的渐变**（`background-size: 100% 300%` + `background-position` 位移动画，3s 循环）；
- 进度条的流光（现有 `fill-sheen`）保留，同时在卡片右上角加一颗 8px 脉动圆点（与侧栏连接点同款 halo 动画）；
- fps/ETA 数字变化时用 150ms 的 `opacity` 淡入（Vue `<Transition mode="out-in">` 包数字，key 绑值），消除跳变生硬感；
- 排队任务卡片左侧加拖拽手柄图标（⋮⋮，六点位），hover 才显示，暗示可拖拽排序。

### P1-3 空状态插画系统
给所有空列表/空数据场景做统一空状态组件 `components/EmptyState.vue`（新建），纯内联 SVG 插画（线条风，描边 `--sv-text-faint`，一个主色点缀），覆盖：
- 任务页无任务：虚线胶片框 + "把视频拖进来，或点新建任务" + 主按钮；
- 模型市场全装完/筛选无结果：立方体插画；
- 日志页无日志、对比页未选任务等。
要求插画 ≤ 30 行 SVG，禁止引入图片资源。

### P1-4 骨架屏全覆盖
现有 `.sv-skeleton` 只在 NewTask 用到。扩展到：首页统计卡（stats 未返回时 4 个骨架卡）、任务列表（3 条骨架卡）、模型市场（网格骨架）、硬件信息卡。骨架结构要与真实布局等高，避免加载完成时的布局跳动（CLS）。

### P1-5 全局快捷键 + 命令面板（Ctrl/Cmd + K）
新建 `components/CommandPalette.vue`：
- `Ctrl+K` 唤起居中浮层（仿 VS Code/Linear），输入模糊匹配：页面跳转（"任务"、"设置"…）、高频动作（"新建任务"、"打开模型对比"、"打开输出目录"）、最近 5 个任务（选中直接打开对比或定位文件夹）；
- 上下键选择、Enter 执行、Esc 关闭；浮层带 8px 圆角 + 深阴影 + 150ms 缩放淡入（`scale(0.98)→1`）；
- 同时在设置页列出全部快捷键对照表。
数据源全部来自现有 store，不新增后端接口。

### P1-6 窗口控制与标题栏细节
- 标题栏窗口控制按钮 hover 效果对齐 Windows 惯例（关闭按钮 hover 变 `#E81123` 红底白图标，其余 hover `rgba(255,255,255,0.08)`），加 100ms 过渡；
- "有可更新版本"提示按钮加轻微呼吸光（`box-shadow` 2.5s 循环），现状太安静容易忽略；
- 标题栏 logo 在启动后做一次 600ms 的微光扫过动画（仅一次，`animation-iteration-count: 1`），作为"软件醒了"的仪式感。

### P1-7 任务完成微庆祝
任务从 running → done 时（监听 store 中任务状态变化，diff 触发）：
- 屏幕右上角 toast 已是现状，**额外**在对应任务卡的状态 tag 上播一次 400ms 的 `scale(1→1.15→1)` 弹跳 + 完成瞬间卡片边缘闪过一圈成功色描边（`@keyframes` 一次性的 `box-shadow` 扩散）；
- 首页四宫格对应统计数字同步做一次数字滚动补间（200ms，requestAnimationFrame 从旧值插值到新值）。
克制原则：只闪一次，不循环、不撒花、不出声音。

---

## 4. P2 —— 别具一格的特色体验（做出记忆点）

> 以下每项都是"让用户截图发群"级别的差异化设计，按兴趣挑做，不必全做；做了就必须做精。

### P2-1 "像素重构"启动页 / 首页 Hero（主打记忆点）
首页 Hero 右侧现有静态"480p→4K"示意。升级为**会呼吸的像素重构动画**：
- 一个 96×96 的 CSS grid 像素画（用 `box-shadow` 多色点阵或 256 个 div 二选一，选性能更好的），左侧 1/3 保持马赛克模糊，右侧 2/3 清晰锐利，中间一条 2px 品牌渐变"扫描线"以 4s 周期从左向右扫过——扫过之处像素从模糊块"重构"为清晰细节；
- 全程 `transform` 实现（扫描线 `translateX` + 清晰层 `clip-path: inset(0 X% 0 0)` 联动），GPU 合成，不触发布局；
- `prefers-reduced-motion` 时退化为静态 50% 对比图。
这是软件核心价值的视觉隐喻：**AI 逐帧把低清重建成高清**，放在第一眼位置。

### P2-2 浅色主题支持
- 在 `App.vue` 增加 `[data-theme='light']` 下整套 token 的浅色映射（底色 `#f5f6f8`、面板 `#ffffff`、文字 `#1c2333`、主色保持 `#4F8CFF`、阴影变浅），naive-ui 的 `darkTheme` 相应切换为 `null`（light）；
- 设置页加"外观：深色 / 浅色 / 跟随系统"三态选择，持久化到 localStorage（key `sv-theme`），跟随系统用 `matchMedia('(prefers-color-scheme: light)')` 监听；
- 切换时给 `html` 加 200ms 的 `background-color/color` transition，避免生硬闪切；
- 走查所有页面，消灭浅色下的"白字白底"盲区（这正是 P0-1 清零硬编码 hex 的价值所在）。

### P2-3 队列时间轴视图（任务页增强）
任务页现有列表视图之外，加一个**横向时间轴模式**切换（仅在有 ≥2 个历史任务时显示入口）：
- 一条横向滚动的 SVG 时间轴，每个任务是一个节点圆点（状态着色），按完成时间排布；运行中的节点带脉动光环；
- 节点 hover 浮出迷你卡片（文件名、模型、用时、fps），点击跳到对应任务卡；
- 纯 SVG 实现，节点 ≤ 50 个（超出只画最近 50 个并提示）；
- 价值：把"我这段时间都用它干了什么"变成一条可视化的生产力轨迹。

### P2-4 性能页"仪表盘化"
`Perf.vue` + `PerfRings.vue` 升级：
- CPU/GPU/显存/内存四枚圆环改为**同心仪表盘**布局或 2×2 大圆环阵列，环上用渐变色（低负载蓝绿 → 高负载琥珀 → 红色），数值在圆心大字号显示；
- 每枚环带 60 点历史迷你 sparkline（SVG polyline，现有 TrendChart 的数据源复用）；
- 运行任务时整个性能页背景叠加极淡的主色呼吸（`opacity` 2s 循环，≤0.03 透明度），"机器在发力"的感知。

### P2-5 模型对比页"擂台"包装
`CompareModels.vue` 的视觉包装：候选模型卡从普通列表改为**对战卡片**——当前选中模型居中带主色光晕描边，其余灰一档；切换模型时旧卡片 150ms 缩小淡出、新卡片从 0.96 放大淡入；顶部加"2/6 号选手"式的位置指示。让"选模型"像选秀打擂，而不是填表。

### P2-6 音效与触感（可选，默认关）
设置页加"界面音效"开关（默认关闭）：任务完成一声短促的"叮"、按钮点击极轻的"嗒"。用 Web Audio API 合成（两个正弦波音符即可），**禁止引入音频文件**；音量 ≤ 0.15。做好了是高级感，做吵了是灾难，所以默认关。

---

## 5. 验收清单（逐项自检后再交付）

- [ ] `pnpm build` 零报错，`pnpm preview` 全部 11 个页面走查无破版
- [ ] 全局 scoped 样式中状态色 hex 已清零（主色/成功/警告/失败只经 token 引用）
- [ ] 所有动画在 `prefers-reduced-motion: reduce` 下静止
- [ ] 对比页（Compare/VideoCompare）无 `backdrop-filter` / `filter: blur`
- [ ] 浅色主题（若做 P2-2）下逐页无对比度事故
- [ ] Ctrl+K 面板（若做 P1-5）在 KeepAlive 页反复切换后无重复监听
- [ ] 数字跳动处（fps/ETA/统计）无布局抖动
- [ ] 功能回归：新建任务全流程、任务取消/续跑/删除、拖拽排序、对比页播放——全部照旧可用

## 6. 执行建议

- **分批提交**：P0 一批、P1 每 2~3 项一批、P2 每项独立批，每批跑一遍构建+预览再走下一批。
- 改任何文件前先完整读一遍该文件（本项目注释密度高，很多"为什么这么做"写在注释里，例如 naive Progress 渐变色只认 `{stops}` 对象形态、KeepAlive include 的异步组件坑——这些注释里的坑不要踩第二遍）。
- 遇到"设计稿想法"与现有注释冲突时，以注释说明的功能约束为准，视觉上换等价方案。
