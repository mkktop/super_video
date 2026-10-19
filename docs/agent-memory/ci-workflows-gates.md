---
name: ci-workflows-gates
description: ci.yml=push main 全量 pytest+前端 vitest；release.yml 自带 pytest 门禁+selftest，tag 触发，版本三方一致性校验在前；发版 push 与 tag 并行不等 ci（2026-09-15 起的节奏）
metadata:
  node_type: memory
  type: project
  originSessionId: sess_47ac3143-b4a6-445d-ba79-34e058291244
---

`.github/workflows/ci.yml`：windows-latest + Python 3.14，每次 push main 跑全量 `python -m pytest tests/ -q`，失败给用户发邮件（用户原话"一直给我发邮件报错"）。frontend-check 作业（v0.2.4 加入）：根 tsconfig 只有 files:[] 是空壳，CI 显式跑 node+web 两个子项目（`vue-tsc --noEmit -p tsconfig.node.json / tsconfig.web.json`）→ **v0.5.4 后加 `pnpm test`（vitest 进 CI）** → pnpm build。改 .vue/.ts 后本地验证命令同款两连。

`.github/workflows/release.yml`（tag `v*` 触发）：**v0.2.4 起也跑 pytest 发布门禁**（"未经测试的 commit 不得直接出安装包"，此前 test_worker_entry_parses_shard 红了几个版本发版却一路绿，见 [[windows-locale-stdout-utf8]]）→ PyInstaller sidecar → `sidecar.exe selftest` → vue-tsc + 前端构建 → NSIS → softprops 上传（正文取仓库根 RELEASE_NOTES.md）→ 模型资产同步 models-v1。tag 推送前先过**版本三方一致性校验**：tag / app/package.json / backend/sv/__init__.py 必须同版本，错位会导致 latest.yml 版本不符 → electron-updater 永远报"已是最新"。

**How to apply:** 发版流程 = 改三处版本号 + 重写 RELEASE_NOTES.md（只留当前版本）→ commit → **push main 与 push tag 同批并行（2026-09-15 起不再等 ci.yml 绿——release 自带同样的 pytest 门禁，测试挂两个都红、release 在上传步骤前中止，发不出坏包；串行等 ci 白多 ~9 分钟）** → 盯 release run 到 completed。本地发版前仍建议全量 `pytest tests/ -q` 兜底（约 1~1.5 分钟，v0.2.8 实测 185+1）；CI 无 GPU 会 skip，本机与 CI 总数不同属正常。监控用 git credential + API：`gh` 命令是 npm 假包会炸（见 [[super-video-update-flow]]）；push 遇 `127.0.0.1:7897 连不上` = 本地代理挂了，`git -c http.proxy= -c https.proxy= push` 直连绕过（2026-09-15 实证）。

release.yml 的前端类型检查曾指向根 tsconfig.json（files:[] 空壳，纯空转假门禁），2026-09-15 修为与 ci 相同的 node+web 两个子项目。

frontend-check 作业（v0.2.4 加入）：根 tsconfig 只有 files:[] 是空壳，CI 显式跑 node+web 两个子项目（`vue-tsc --noEmit -p tsconfig.node.json / tsconfig.web.json`）。改 .vue/.ts 后本地验证命令同款两连。

**慢机竞态教训（v0.3.1 首发翻车）**：CI 全量 ~420s vs 本地 ~135s，时序差 3 倍会暴露"测试假设 runner 不会碰自己数据"类竞态——test_resume_api 断言 resume 后恰为 `queued`（慢机断言前已被领走）、test_task_history_cap 断言列表恰 100（前面测试的真实任务还在跑挤进 active 区）。写与 runner 并存的测试时：断言放宽为语义等价的多态（queued/running 皆可）、总数按"预期+active 实际数"比对，或干脆 monkeypatch `db.next_queued` 让 runner 不领任务（新测试的标配做法）。tag 发布失败后重发 = 删远端/本地 tag → 重新打在新 commit → push（未发布过 Release 时这样干净，v0.3.1 实证）。

**v0.3.6 再证**：release 门禁在 `test_notify_action_immediate_no_power` 挂（`_wait` 8s 等不到 queue_done，假 worker 路径与功能改动无关），而**同提交的 ci.yml 全绿**——纯慢 runner 抖动（该次 CI 全量 506s vs 本地 222s）。Release 尚未发布时最干净的补救不是重打 tag，而是 API `POST /actions/runs/{id}/rerun-failed-jobs`（201 受理，重跑通过后照常上传）。判定流程：先查同 commit 的 ci.yml 结论，绿即实锤抖动。

**v0.7.0 三证（2026-09-30）**：release 门禁挂 `test_tasks_search_batch.py::test_batch_cancel_delete_resume`（assert 'running' == 'queued'，与 v0.3.1 的 test_resume_api 同款「慢机断言前 runner 已领走」竞态），同提交 ci.yml 全绿 → rerun-failed-jobs 一次过，正常出包。该竞态候选测试（断言 resume/batch 后恰为 queued 的）迟早都该按上文「语义等价多态断言」改造。
