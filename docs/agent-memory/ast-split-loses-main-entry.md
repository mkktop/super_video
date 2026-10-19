---
name: ast-split-loses-main-entry
description: 按函数行号切割 Python 模块时 ast FunctionDef end_lineno 不含模块尾 if __name__
  入口块——worker.py 拆分曾因此全量 11 测试假失败(rc=0 零事件)
metadata:
  node_type: memory
  type: feedback
  originSessionId: sess_475b603b-a100-4625-923a-45d254f6b786
---

2026-09-04 拆 worker.py 时用 ast spans 按函数边界切割,丢了文件尾的 `if __name__ == "__main__"`(argparse+stdout reconfigure+sys.exit(main()))。症状极具迷惑性:worker 子进程拉起后立即退出 rc=0、零事件,任务报「worker 异常退出 (rc=0)」,11 个测试失败且每个等 60s 超时。

**Why:** ast.FunctionDef 的 (lineno, end_lineno) 只覆盖函数体;模块级入口块、模块尾注册代码、`sys.exit` 调用都在函数 span 之外。import 成功+函数可调用让冒烟检查全绿,直到真拉子进程的集成测试才爆。

**How to apply:** 切割/重组 Python 模块后,除了"import ok"冒烟,必须:①对比新旧文件尾部(`tail` 或 grep `__main__`);②跑一个真拉子进程的集成测试(test_server.py 一类)再算数;③恢复入口块时从 `git show HEAD:<path>` 取原文,别凭记忆重写(本例入口块含 stdout GBK reconfigure,丢了会复发 [[windows-locale-stdout-utf8]])。
