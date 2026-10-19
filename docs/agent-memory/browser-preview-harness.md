---
name: browser-preview-harness
description: 纯浏览器 UI 预览脚手架（pnpm preview + mock），前端样式走查不再需要 Electron/后端
metadata:
  node_type: memory
  type: project
  originSessionId: sess_a21cd062-2734-4355-9385-abc5fb9eb0a2
---

2026-09-04 落地：`cd app && pnpm preview` → http://localhost:5199/preview.html ，渲染层连全 mock 可独立于 Electron/Python 走查样式。构成：`vite.preview.config.ts`（root 指向 src/renderer + SVG 预览图中间件）、`src/renderer/preview.html`、`src/renderer/src/preview/{main,mock}.ts`（替身 window.sv 全部方法 + fetch 拦 /api/* + FakeWebSocket 推 perf/progress 事件，运行中任务进度循环推进）。

**Why:** 本机 [[dev-installed-cannot-coexist]]（安装版常驻，dev 秒退），此前改前端样式无法本机验证；且 <img>/<video> 资源加载不走页面 fetch，预览图必须由 vite 中间件应答。

**How to apply:** 改样式先开 preview 走查再交付；BASE=window.location.origin，JSON 走 fetch 替身、图片走中间件。三个坑：①naive-ui NProgress line 型渐变色只认 `{ stops: [from,to] }` 对象，传数组直接渲染崩整页白屏；②NPopconfirm 的 trigger 槽内 v-if 为空时 VBinder patch 崩（NewTask 预设删除按钮已修，v-if 移到整个 NPopconfirm 上）；③大面积 body 径向渐变+mask+backdrop-filter 在低端 GPU/软件渲染下严重 banding，环境光用小面积 radial 收进卡片内部或线性渐变。

Mock 夹具刻意极简（8 模型/7 任务/4 预设，只为凑齐卡片状态），真实注册表在 backend/sv/models/registry_json/ 有 37 个 manifest——用户已注意到预览里模型偏少；待拍板改进=mock 启动时直接读 registry_json/*.json 灌真数据（与真实注册表同源零维护），替代手工扩充夹具。见 [[user-iterative-ui-polish]]。

**ZCode in-app browser 走查的 rAF 陷阱（2026-09-12 实证）：** IAB 面板被遮挡/不可见时页面内 requestAnimationFrame **完全停跳**（2.5s 不触发一帧）→ Vue Transition 的 enter/leave 类卡死、页面看似空白破版假象；`capabilities.get("visibility").set(true)` 也未必恢复（`document.hidden` 仍 true，渲染时钟 `document.timeline.currentTime` 恒 0）。**切页类走查只能在面板实际可见时做**：`<Transition mode="out-in">` 的 leave 卡死=新页永不挂载（`main` 里旧页挂 `*-leave-active`），且 Vue 的 `nextFrame` 在模块加载期就用 IIFE 捕获了原生 rAF——事后替换 `window.requestAnimationFrame` 无效，补发 `transitionend` 也无效；判定页面是否真切换要看目标页根类是否挂载（KeepAlive 常驻页下 `querySelector('main h1')` 会命中缓存旧页，须查可见节点）。非过渡验证（样式/几何/动画）不受影响：CSSOM 读取、`getAnimations()[0].pause()+currentTime` 手动 seek、getComputedStyle 强制回读都可用（首页扫描线修复 44b8e0e 即以此法证实）。**真实 Electron 窗口无此问题，不是代码 bug**，别据此改组件实现。另：@property 注册的自定义属性若被子元素消费，必须 `inherits: true`——动画挂在父元素上，inherits:false 时子元素永远只拿 initial-value（扫描线自 v0.4.8-preview.2 起没动过的根因）。

**走查手法沉淀：** 切页用 evaluate 点 `.nav-item`（getByRole name 会被「任务/新建任务」子串撞名 strict violation）；每次切页后轮询等 `main.page > div` 类名清掉 enter/leave 且 opacity==1 再截图（异步 chunk 页首切要等更久）；截图视觉审查用 MiniMax understand_image——本地文件路径会超时，用会话产物上传后的远端签名 URL。

**2026-09-29 成功配方（Settings 页走查实证）：** 顺序是关键——**先 reload 页面再操作**：任何一次发生在 rAF 停跳期间的点击都会让 Vue out-in 过渡状态机挂死（nav active 类已切换但新页永不挂载），之后所有点击全部失效、rAF 替身也救不回来，只有 reload 重置。正确序列：reload → 立刻 evaluate 打 rAF 替身（趁应用挂载前）→ evaluate 按 textContent 找 `.nav-item` dispatchEvent 点击 → 轮询目标页根类（如 `.settings-page`）挂载即成。真点（locator.click）的 actionability 检查在渲染停跳时必超时，别用。
