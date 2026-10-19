---
name: docs-completeness-2026-09
description: 2026-09-25 文档体系补齐——LICENSE 是自写非商用协议、前端无 router/Pinia 等文档化事实
metadata:
  node_type: memory
  type: project
  originSessionId: sess_1cfbab7e-e1aa-471a-aa24-047408ba25bb
---

2026-09-25 应用户要求补齐项目文档缺口，新增/修订六处：

- **仓库根 LICENSE**：自写的中文非商用协议（免费使用、禁未经授权商业转售/再分发），与安装器 `app/build/license_zh.rtf` EULA 条款刻意一致。**勿替换成 MIT/Apache 等标准开源协议**——项目非商用（见 [[vset-model-gap-and-tier1]]），且部分模型 CC BY-NC。
- **app/README.md**（新建）：前端开发文档。关键事实：前端**没有 vue-router/Pinia**（PLAN.md 蓝图提过但未采用）——页面切换是 App.vue 动态 `<component :is>` + KeepAlive + Transition，状态是 store.ts 单个 reactive；常驻页必须静态导入（async 包装组件 KeepAlive include 不匹配坑）。设计 token 全在 App.vue `:root --sv-*` + `[data-theme='light']` 同名覆盖。
- **backend/README.md**：补 WS 事件契约表（16 种 type 的载荷与时机）；测试数字从 306 校正到 445（--collect-only 口径），文件数 50→54，补前端 `pnpm test`。
- **根 README**：测试节补前端命令、相关文档节补 app/backend/design/LICENSE 链接。
- **design/README.md**（新建）：图标三条命令管线（rainframe.svg → make_icons.js 无头 Electron → final/ PNG → build_assets.py Pillow → app/build 成品；installer/make_assets.py 出安装器 bmp+license_zh.rtf）。现役源是 rainframe/，B-evolution.svg 是被否的上一代。

**Why:** 此前这些知识散在会话记忆与代码注释里；文档化后新会话/协作者可直接读到。
**How to apply:** 涉及许可证、前端结构、WS 契约、图标再生成时先读对应文档，别按通用开源项目惯例行事。
