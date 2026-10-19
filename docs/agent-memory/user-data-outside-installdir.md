---
name: user-data-outside-installdir
description: 用户数据必须放安装目录外（v0.1.21 教训：electron-builder 更新=旧卸载器清空安装目录）
metadata:
  node_type: memory
  type: feedback
  originSessionId: sess_47ac3143-b4a6-445d-ba79-34e058291244
---

v0.1.20 及之前把 models_store/trt-runtime/data/.tmp 全放安装目录内（ROOT=resources）。
electron-builder NSIS 更新流程 = 先跑旧版卸载器（RMDir 清空整个安装目录）再装新版 →
用户更新后模型权重/TRT 组件全部被删，被迫重新下载 1.5GB+。

**Why:** 我当时验证升级只比对了版本号和任务链路，没验数据存活；"数据放程序目录"在
Electron 自动更新体系里是结构性错误，不是边缘情况。

**How to apply:**
- v0.1.21 起数据在安装目录同级 `super_video_data`（Electron 传 SV_DATA，dev 沿用仓库目录）；
  以后新增任何落盘数据（缓存/注册表/模型）一律进 sv/paths.py 的 DATA_ROOT 族路径，绝不进 ROOT。
- 迁移规则：源存在且目标不存在才搬（不覆盖用户新数据）；单目录失败下次启动重试；
  worker/ort-check 子进程靠继承 sidecar 环境变量拿到 SV_DATA。
- 过渡期事实：0.1.20→0.1.21 升级时旧卸载器仍会清空一次（已在更新说明提醒用户先升级再下载）。
- 验证任何更新链路改动时必须检查数据存活（模型/组件/设置/任务历史），只看版本号不算验过。
