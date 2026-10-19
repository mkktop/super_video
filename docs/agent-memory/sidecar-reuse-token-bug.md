---
name: sidecar-reuse-token-bug
description: Electron 主进程两处 token bug（复用分支不生成 token→401 自锁 + 主进程 fetch
  不带头→关窗确认死代码），2026-09-03 已修复（已随 v0.4.4 发布）
metadata:
  node_type: memory
  type: project
  originSessionId: sess_283f3b77-57bb-42d0-aedf-2ef009b8c4ab
---

v0.2.4 引入 token 鉴权时存在的两个 Electron 主进程 bug，2026-09-03 审查发现并已修复（改动在 app/src/main/index.ts，未提交、随下版发布）：

1. **复用路径 token 缺失（P1）**：`apiToken` 只在「全新拉起」分支生成，两个复用分支直接 return → renderer 拿空 token，sidecar（持旧 token）全 401 → UI 永久初始化失败且自锁（重启仍复用同一 sidecar）。触发 = UI 崩溃/强杀/断电后重启。修法：新增 `issueToken(dataRoot)`（轮换写 token 文件，写失败退读现有文件，都失败返回 false）+ `adoptReuseToken()`，两个复用分支在发任何业务请求前调用。
2. **主进程 fetch 不带 token（P2）**：`confirmClose()`/`hasActiveTasks()` 裸 fetch `/api/tasks` → 打包版必 401，body 是对象 `{"detail":...}`，`.some()` 抛 TypeError → 关窗确认对话框从未生效、旧版空闲 sidecar 永远杀不掉。修法：新增 `apiHeaders()` 帮手，两处 fetch 带头。
3. 顺带对齐：新建路径 `issueToken` 返回 false 时 env 条件注入 SV_TOKEN（空值 sidecar 本就不采信，按契约显式不注入）；README 测试数字 306→352。

**Why:** 修法依赖的 sidecar 侧契约已用隔离 TestClient 六项验证（401 body 形状 / env token / 只写文件轮换即被接受 / env+file 并集 / 错 token 拒 / 删文件 env 仍有效）——sidecar `_expected_tokens` 每请求实时读文件，本就是为「UI 重启换新令牌」设计的，Electron 侧此前从未实现自己那一半。测试盲区根因：Electron 主进程零测试覆盖（同 [[r2-mirror-update-fallback]] 的 isNetworkError 漏判同源）。

**How to apply:** 提交时重点描述自锁场景（UI 崩溃后重启打不开）；存量用户在修复发版前的手动解法 = 任务管理器结束 sidecar.exe。验证门禁：vue-tsc node/web 双绿 + pnpm build 绿（主进程改动按 [[ci-workflows-gates]] 惯例不重跑 pytest，后端零改动）。
