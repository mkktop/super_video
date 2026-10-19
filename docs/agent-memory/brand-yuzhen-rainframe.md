---
name: brand-yuzhen-rainframe
description: 2026-09-23 品牌化定名雨帧（英文名 RainFrame）——显示层全量换名，内部标识符刻意保留；exe 自下版起叫 RainFrame.exe
metadata:
  node_type: memory
  type: project
  originSessionId: sess_cc526dd7-9fcc-47a4-9f09-c73923d17fd4
---

2026-09-23 用户定品牌名「雨帧」，英文名 **RainFrame**。已落地（随下一版发布生效）：

- 显示层全换：窗口标题/托盘 tooltip/TitleBar/设置页关于卡/日志导出默认文件名（雨帧_日志.txt）/分享卡标题/队列完成关机提示/FastAPI title/CLI description/README/backend README/ASSETS.md/ModelScope README 生成模板（sync_modelscope.py）
- 构建产物（app/package.json build 段）：`productName=雨帧`、新增 `executableName=RainFrame`（exe 从 super_video.exe 变 **RainFrame.exe**）、安装包名 `RainFrame_${version}_setup.exe`；release.yml 的 R2 上传键已同步
- 图标 9-29 终稿「律动环」（用户自迭代 variants3~10 批次后指定 variants9.html 的 M1，柱阵已烘焙成静态 SVG），产出链见 [[icon-pipeline]]；安装器 header/sidebar 位图同日换血（深水底+青→紫→粉霓虹升柱+雨帧字标）改名

**刻意保留的内部标识符（勿顺手"补改"）**：npm name `super-video-app`（Electron userData 与单实例锁来源，改了锁会失效）、appId `com.supervideo.app`（升级链身份，改了等于新应用）、GitHub repo `mkktop/super_video`、R2 域 super-video.kaikun.top 与桶 `super-video`、数据目录 `super_video_data`（存量用户数据+[[d-soft-install-healthy]] 实测机全靠它保命）、ModelScope `mengkaikun/super-video-models`、下载 UA `super-video/*`、pyinstaller `sidecar` 名。

**Why:** 数据目录/npm name/appId/repo 关乎存量用户升级与数据存活，改名收益纯 cosmetic、风险全在链路；exe 与安装包名用 ASCII RainFrame 而非中文，是避开命令行/下载 URL/R2 键的编码坑。

**How to apply:** 新增用户可见文案用「雨帧」（需 ASCII 处用 RainFrame）；在代码里看到 super_video 字样先分辨是内部标识符还是漏改的显示名；发下一版时 RELEASE_NOTES 应提一句更名（安装包文件名变了，README 已注明旧名）；进程名排查按 [[dev-installed-cannot-coexist]] 新旧名都查。
