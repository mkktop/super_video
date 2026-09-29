# 雨帧桌面端（app/）

Electron 43 + Vue 3 + TypeScript + Naive UI。三层进程结构：

```
src/
├─ main/index.ts      主进程（窗口/托盘、sidecar 生命周期、自动更新）
├─ preload/index.ts   contextBridge 暴露 window.sv 桥（IPC 面）
└─ renderer/
   ├─ index.html      Electron 窗口入口
   ├─ preview.html    纯浏览器预览入口（不起 Electron/后端）
   └─ src/
      ├─ App.vue      应用骨架 + 全部设计 token（见下）
      ├─ store.ts     reactive 单例数据流（任务/模型/下载进度/WS 落地）
      ├─ api.ts       sidecar HTTP/WS 客户端（自动带 X-SV-Token；ApiError 带状态码）
      ├─ theme.ts     深色/浅色/跟随系统 → 写 <html data-theme>，localStorage 持久化
      ├─ uiFx.ts      纯表现层状态（页面切换方向），不碰 store 数据流
      ├─ pages/       11 个页面（Home/Tasks/NewTask/Models/CompareModels/Trim/
      │               ImageSR/MangaSR/Compare/Perf/Logs/Settings）
      ├─ components/  TaskCard/CompareSlider/CommandPalette/Sidebar/PerfRings 等
      ├─ composables/ 向导页共用逻辑（模型选项/输出设置/编码器/计划门控/更新/TRT）
      └─ preview/     浏览器预览 mock（mock.ts 编造 window.sv + API + WS 事件流）
```

## 命令

```bash
pnpm dev      # 开发模式（electron-vite dev）
pnpm preview  # 纯浏览器 UI 预览 → http://localhost:5199/preview.html（详见根 README）
pnpm test     # vitest 单测（tests/，5 个 spec）
pnpm build    # 双 tsconfig 类型检查（node/web 两份都要）+ electron-vite build
pnpm dist     # build + electron-builder NSIS 安装包 → ../dist-app/
```

## 页面管理：动态组件 + KeepAlive（无 vue-router）

项目**没有用 vue-router / Pinia**（PLAN.md 蓝图提过，实际未采用）：页面切换是
`App.vue` 里动态 `<component :is>` + `<KeepAlive>` + `<Transition>` 的组合，
全局状态是 `store.ts` 的单个 `reactive` 对象。

两个刻意约束（改动页面前先看 `App.vue` 头部注释）：

- **常驻页（NewTask/Trim/ImageSR/MangaSR/CompareModels）必须静态导入**：KeepAlive
  的 include 对首次渲染时还没 resolve 的 async 包装组件不匹配，首切会丢草稿；
- **其余页面走 `defineAsyncComponent` 懒加载**：首屏不解析，首次切入才拉 chunk。

页面切换方向（从下往上 vs 从上往下浮入）由 `uiFx.ts` 按侧栏顺序差决定。

## 设计 token：全部颜色经 `--sv-*` 引用

token 层全在 `App.vue` 的 `<style>` 里：`:root { --sv-bg/--sv-panel/--sv-text/… }`
定义深色（默认），`[data-theme='light']` 用**同名 token 覆盖**整组实现浅色——
`theme.ts` 只负责切换 `data-theme`（模块加载即生效，防首帧深浅闪切）。

约定：

- 颜色一律 `var(--sv-*)`，不写死 hex；`--sv-*-rgb` 三连用于 `rgba(var(--sv-*-rgb), a)`
  派生任意透明度（两主题下都成立）；
- 渐变 stops、SVG 填色同样可以传 `var(--sv-*)`；
- `.sv-card` / `.sv-num` 等工具类已封装常用卡片与数字排版。

## 主进程职责（main/index.ts）

- **sidecar detached 拉起**：UI 崩溃/退出时任务进程不受影响；重启先探测复用
  （校验健康标记与版本一致性），升级残留的旧版 sidecar 占端口时清杀后重拉；
- **本地令牌**：token 写文件 + 随 spawn env 双源注入，renderer 经 `backend:info`
  取得；文件源是「UI 重启换新令牌、复用中的旧 sidecar 实时校验接受」的关键；
- **自动更新**：electron-updater，GitHub / R2 双下载源 + 稳定/预览双通道
  （机制详见根 README「R2 备用下载源」一节）；
- 托盘与「关闭时最小化」、任务栏进度、任务终态系统通知；
- 文件对话框 / shell 打开 / `webUtils.getPathForFile`（Electron 43 的
  `File.path` 已移除，拖拽取路径必须走它）。

## 已知取舍

`webSecurity: false`（`<video>` 直读 file://）是拍板保留项，背景与理由见
`backend/README.md`「已知取舍」，勿顺手"修复"。
