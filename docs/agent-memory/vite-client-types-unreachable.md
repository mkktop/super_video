---
name: vite-client-types-unreachable
description: app 顶层 node_modules 无 vite 包，vite/client 类型引用全仓库失效；静态图片导入的声明必须放独立 assets.d.ts
metadata:
  node_type: memory
  type: project
  originSessionId: sess_17413170-0490-4de1-9726-9d9dfa7ed7aa
---

pnpm 虚拟存储布局下 `app/node_modules/vite` 顶层链接不存在（vite 只在 .pnpm 子依赖里），env.d.ts 的 `/// <reference types="vite/client" />` 解析失败（TS2688 实证）→ 所有 vite 资产通配声明全仓库不生效。2026-08-27 用户在 TitleBar 引入首个静态图片导入时 TS2307 暴露。

**Why:** 更绕的是：在 env.d.ts 里补 `declare module '*.png'` 无效（env.d.ts 同时含 triple-slash 失败引用 + declare global，混合形态下通配声明未进编译图，机制未深究）；但放进独立的纯脚本声明文件立即生效。

**How to apply:** 已落地 `app/src/renderer/src/assets.d.ts`（含 *.png），全量 vue-tsc 恢复绿。今后新增其它资产类型（svg/woff 等）静态导入报 TS2307 时，往该文件追加对应 declare 即可；或根治性方案=package.json 显式 devDep `vite`（待用户拍板）。排查此类问题先删 app/*.tsbuildinfo 防缓存迷惑。
