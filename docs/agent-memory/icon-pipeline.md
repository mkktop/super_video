---
name: icon-pipeline
description: 应用图标定稿与离线渲染工具链（无 ImageMagick，用项目 Electron + Pillow）；2026-09-29 终稿=M1「律动环」（用户自迭代第 9 批选定）
metadata:
  node_type: memory
  type: project
  originSessionId: sess_0a9ff442-3358-4f5d-a7b9-3cbea66d226f
---

应用图标定稿史：B 画质进化（8-24）→ 霓虹取景框（9-25，被否）→ 极光取景框（9-25 二批选定）→ **M1「律动环」终稿（2026-09-29）**。用户在 [[brand-yuzhen-rainframe]] 品牌化后自己迭代了 design/icons/variants3~10.html 多批画板，最终指定 variants9.html 的 M1：深水暗底（radial #0e3d50→#061a26）+ 56 根径向均衡器柱（青→紫→粉随环流转、高度按正弦律动，音频可视化隐喻）+ 炫彩播放键（iridV 渐变 #22d3ee→#a78bfa→#ff5c8a）。前作极光取景框备份在 `design/icons/rainframe/rainframe-aurora.svg`。

**关键点：** variants9.html 的柱阵是 JS 动态生成的（N=56/r0=120/`h=15+20|sin3t|+7sin(7t+1.3)` 三色插值），不能直接当图标源用；权威源 `design/icons/rainframe/rainframe.svg` 是用 Python 复刻该公式烘焙的**静态 SVG**（512 viewBox）。改柱形/配色要改烘焙脚本重生成，勿手画。

**How to apply:** 出全套三条命令：1) `cd app && node_modules/.bin/electron ../design/icons/rainframe/make_icons.js`（无头 Chromium 渲各尺寸 PNG → design/icons/final/，必须 no-sandbox/disable-gpu，img 用 data: URL 防 canvas 污染）；2) `.venv/Scripts/python.exe design/icons/rainframe/build_assets.py`（Pillow 组装 icon.ico + 标题栏 logo.png + 安装器 header/sidebar 位图；安装器配色=深水系 #061a26 底 + 青→紫→粉霓虹条，方向与 iridV 一致）；3) 重启 dev（窗口/托盘图标启动时读取）。16px 下柱阵糊成辉光环属预期，播放键仍是主体。
