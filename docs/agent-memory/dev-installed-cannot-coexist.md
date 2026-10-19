---
name: dev-installed-cannot-coexist
description: dev 与安装版不能同时跑——userData 共用触发单实例锁，dev 秒退 exit 0 零输出；安装版进程名 super_video.exe（v0.5.6 起 RainFrame.exe）
metadata:
  node_type: memory
  type: project
  originSessionId: sess_47ac3143-b4a6-445d-ba79-34e058291244
---

`pnpm --dir app dev` 与正式安装版**互斥**：两者 userData 同为 %APPDATA%\super_video，
`app.requestSingleInstanceLock()` 拿不到锁→`app.quit()`。症状极具迷惑性：electron-vite
打出 "starting electron app..." 后**秒退、exit 0、零输出**（加 ELECTRON_ENABLE_LOGGING 也无输出）。

**Why:** 安装版进程名是 `super_video.exe`（electron-builder 按 productName 改名；2026-09-23 品牌化后 productName=雨帧 + executableName=RainFrame，**自 v0.5.6 起 exe 改叫 RainFrame.exe**，v0.5.5 及以前仍是 super_video.exe——排查时两个名字都查，见 [[brand-yuzhen-rainframe]]），`tasklist | grep electron`
查不到它，极易误判"没有实例在跑"；2026-08-25 为看 dev 设置页排查了三轮才定位。同日还发现：
8730 上的"孤儿 sidecar"可能是运行中安装版的子进程（父进程 super_video.exe），杀了它用户
安装版界面会断连；而安装版 renderer 会轮询重连 8730 上任何 sidecar（连 dev 拉起的都能连上），
用户屏幕上出现的窗口未必是你起的那个。

**How to apply:**
- 起 dev 前先 `tasklist | grep super_video`，有则告知用户后 `taskkill //IM super_video.exe //T //F`（会关掉用户正在用的安装版，说明清楚）。
- sidecar 身份用命令行区分：dev=项目 `.venv\Scripts\python.exe … cli.py serve`；安装版=`…\resources\sidecar\sidecar.exe serve`。Git Bash 会吃 PowerShell 的 `$_`，用 `Get-CimInstance Win32_Process -Filter` 形式查 CommandLine。
- 动 8730 端口占用前先查监听进程的路径/父进程，确认真是孤儿再杀。
- 安装版参照见 [[d-soft-install-healthy]]；curl 探测须 --noproxy 见 [[local-proxy-hijacks-localhost]]。
