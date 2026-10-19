---
name: gitbash-python-toolchain-pitfalls
description: Git Bash 里调 Windows Python/ffprobe 的连环坑：/tmp 路径错位、glob 方括号、全角冒号文件名传参炸
metadata:
  node_type: memory
  type: feedback
  originSessionId: sess_f65c667e-d92b-4888-91d1-62f949bea1b5
---

2026-09-18 排障时连环踩坑，三连修才跑通：

1. **`cp` 到 Git Bash `/tmp` 后，Windows 原生 Python 打开 `/tmp/xxx` 实际落在 `C:\tmp\`**——sqlite3.connect 还会静默新建空库，症状是"库是空的/没有表"，其实是读错了文件。**Why**：MSYS 的 /tmp 映射到 AppData\Temp，Windows Python 不认识 POSIX 路径。**How to apply**：Bash 和 Python 之间交换文件用双方都认的 Windows 路径（如 D:\work\.tmp.db）。
2. **glob 模式里 `[Sakurato]` 被当字符类**（只匹配单字符）→ IndexError。用 `os.listdir` + 子串过滤代替 glob 处理含方括号的文件名。
3. **文件名含全角冒号"："直接在 Bash 里传给 exe 报 "Illegal byte sequence"**（cp936 编码链）。用 Python subprocess 传参数列表可正确处理 Unicode。

另外：ffprobe 不在 PATH，仓库自带在 `D:\work\super_video\bin\ffprobe.exe`（SV_FFPROBE 可覆盖）。
