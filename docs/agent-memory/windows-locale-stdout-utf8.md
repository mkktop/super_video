---
name: windows-locale-stdout-utf8
description: 中文 print 在非中文 Windows(cp1252)管道必炸、stdout 全空；入口统一 reconfigure；测试注入
  PYTHONIOENCODING=cp1252 回归
metadata:
  node_type: memory
  type: project
  originSessionId: sess_47ac3143-b4a6-445d-ba79-34e058291244
---

v0.2.0 后 CI `test_worker_entry_parses_shard` stdout 全空失败：GitHub 英文 Windows runner 管道编码 cp1252，`python -m sv.server.worker` 直跑入口 emit 中文错误行（"任务 … 不存在"）直接 UnicodeEncodeError，一行都写不出；本机中文系统 GBK 编码得了中文所以永远复现不了（"本机绿 CI 红"先怀疑编码）。runner 真实链路 spawn 时带 `PYTHONIOENCODING=utf-8`，线上功能从未受影响——只有测试/直跑/调试场景暴露。

**修法（2026-08-25 已落地）**：worker `__main__` 入口加 `sys.stdout/stderr.reconfigure(encoding="utf-8", errors="replace")`（cli.main 同款，frozen GBK 坑见 [[trt-component-installer]]）；测试 env 注入 `PYTHONIOENCODING=cp1252` 主动回归，任何机器都能验证兜底。

**How to apply:** 任何直接 print 中文的进程入口（frozen exe、`-m` 直跑）都要 reconfigure；复现命令 `PYTHONIOENCODING=cp1252 python -m sv.server.worker <不存在的id>`——修复前 stdout 空 + rc=1，修复后输出 UTF-8 JSON 行 rc=2。
