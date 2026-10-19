---
name: git-bash-commit-msg-utf8
description: 本机 Git Bash 提交中文信息两种坑——heredoc 偶发被存成 GBK 乱码（用 Python 写 UTF-8 临时文件 +
  git commit -F）；console 显示乱码≠提交坏了（xxdd 验字节再动手）
metadata:
  node_type: memory
  type: feedback
  originSessionId: sess_f9e027fd-a96b-402a-bf3c-a90f940f6206
---

本机（win32 + Git Bash）用 `git commit -F - <<'EOF'` heredoc 提交中文信息，**偶发**把消息按 GBK 存进提交对象（同会话多次提交只坏一次，机制未查明）——那次 `852ca49` 主题成了 `淇痉鍥剧墖...`。

**Why**: 乱码有两类且处置完全相反：提交对象真坏（GBK 字节）需 amend 修复；对象是合法 UTF-8 只是 console 用错码页渲染——后者 amend/force-push 是瞎折腾。
**How to apply**: ①稳妥提交中文消息：Python `io.open(tmp,'w',encoding='utf-8',newline='\n').write(msg)` 写临时文件 + `git commit -F <tmp>`，绕开 heredoc 编码劫持；②看到 `git log` 中文乱码先 `git log -1 --format=%s | head -c 30 | xxd` 验字节——UTF-8 中文是 e4/e5/e6/e7/e8/e9 开头的三字节序列（「修」=e4bfae），合法就别动；③真坏了才 amend -F 修 + `push --force-with-lease`（仅限自己刚推、无人消费的提交）。
