---
name: only-own-tests-scope
description: 全量 pytest 有失败时只保证自己新增的测试绿,不追用户 WIP 里的既有失败
metadata:
  node_type: memory
  type: feedback
  originSessionId: sess_47ac3143-b4a6-445d-ba79-34e058291244
---

2026-08-24 加性能监控功能时,全量 pytest 出现多个与本次改动无关的失败(用户未提交的 test_engine_backends.py / test_server.py 波动,失败集每次运行不同,疑似 SV_DB 环境变量在测试模块 collection 期互相覆盖的既有问题)。用户明确说"你不用管其他的,只管你写的"。

**Why:** 工作区常有大量未提交 WIP(新测试文件、改动的管线代码),失败归属不清;修别人的 WIP 测试既越界又可能和用户正在进行的工作冲突。

**How to apply:** 跑全量前先单独跑自己的测试文件;全量失败时先做归属判断(git status 看文件是否用户 WIP),是别人的就报告事实并继续自己范围(用户说"修一下"后再动)。给测试文件设 SV_DB 时注意:模块级 `os.environ` 赋值会被后收集的模块覆盖(collection 期全跑一遍),**运行期 fixture 才可靠**——autouse fixture 里改 env + `db.init_db()` + teardown 还原(2026-08-25 test_encode_opts CI 修复即此模式:直调路由函数不经 lifespan,必须自建 schema;本机绿 CI 红多半是 .tmp 里遗留旧库文件掩盖了缺 schema)。另:裸 `except X as e` 里 e 就是异常本身,别写 `e.value`(pytest.raises 上下文才用 e.value)。参见 [[super-video-project-goal]]。
