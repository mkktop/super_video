# super_video —— AI 视频超分桌面软件

低分辨率视频 → 高分辨率重建（GAN/光流补帧/PyTorch），Electron + FastAPI sidecar 架构。

## 功能

- **严格串行任务队列**：一次一个视频、每个任务参数独立（模型/倍率/编码/补帧/降噪）
- **多模型**：AnimeVideo（动漫，快）、Real-CUGAN（动漫降噪）、Real-ESRGAN x4plus（真人）、
  x4plus PyTorch 版（分块断点续跑）、RIFE v4.26 补帧（帧率×2）；自定义 ONNX 导入
- **推理**：DirectML 默认（全显卡兼容）+ FP16（提速 1.36~2.05x）；CUDA/PyTorch 组件可选
- **输入兼容**：8/10bit、VFR（自动 CFR 化）；HDR 暂不支持（明确报错）
- **产物**：H.264/H.265 软编或 NVENC 硬编，音轨无损保留，前后同帧对比

## 开发运行

```bash
# 后端（Python 3.14）
python -m venv .venv && .venv/Scripts/pip install -r backend/requirements.txt
# ffmpeg/ffprobe 放到 bin/（BtbN n8.1 正式版，见 PLAN.md 风险表）

# 前端 + 桌面壳
cd app && pnpm install && pnpm build && npx electron .

# 测试（47 项）
cd backend && ../.venv/Scripts/python.exe -m pytest tests/ -q
```

## 打包发布（M4）

```bash
# 1) sidecar（PyInstaller onedir，155MB）
cd backend && ../.venv/Scripts/pyinstaller.exe sidecar.spec --noconfirm

# 2) 安装包（electron-builder NSIS，产物 dist-app/super_video_0.1.0_setup.exe，252MB）
cd app && pnpm dist
```

安装布局：`resources/{sidecar,bin}`；数据目录（models_store/.tmp）也在 resources 下（per-user 安装，可写）。
打包版 worker 通过 `sidecar.exe worker <task_id>` 复用入口拉起（无解释器依赖）。

### 发布与自动更新（GitHub Releases）

自动更新已接线 electron-updater（源：`github.com/mkktop/super_video`，公共仓库无需 token）。
设置页有"检查更新"，打包版启动 10 秒后也会静默检查；下载完成弹窗提示重启。

**发新版本**（仓库推送后）：

```bash
cd backend && ../.venv/Scripts/pyinstaller.exe sidecar.spec --noconfirm   # 先重打 sidecar
cd app && pnpm dist                                                        # 再打安装包
```

到 GitHub → Releases → 新建 Release（tag = 版本号，如 v0.1.1），上传 `dist-app/` 下的三个文件：
`super_video_0.1.1_setup.exe`、`super_video_0.1.1_setup.exe.blockmap`、`latest.yml`。
已装用户即会收到更新提示（blockmap 支持增量下载）。

**模型资产**（首次发布需做一次）：新建 Release tag `models-v1`，上传
`models_store/realesrgan-x4plus/RealESRGAN_x4plus_dyn.onnx`（由
`backend/scripts/export_onnx_x4plus.py` 从官方权重确定性导出，sha256 见 manifest）。
此后 x4plus 的下载源即为本仓库 GitHub Release。其余模型已直接使用各官方 GitHub Release。

## 里程碑

- M0 ✅ 管线/引擎/注册表（BENCH.md：IO 校准方法论、真片源质量评估）
- M1 ✅ sidecar+SQLite 队列+Electron 壳（串行队列、进程隔离、UI 崩溃任务不丢）
- M2 ✅ 模型市场/预设向导/设置页/对比预览
- M3 ✅ FP16 默认精度、Real-CUGAN、RIFE 补帧、VFR/10bit、显存降档、
  torch 引擎+分块 checkpoint 续跑（扩散模型 Windows 不可行结论见 BENCH.md）
- M4 ✅ PyInstaller+NSIS 安装包（252MB）、镜像 fallback、自定义模型导入、
  单实例、日志导出；自动更新待发布源

## 灾难场景清单（M4 验收）

| 场景 | 状态 |
|---|---|
| 中文/空格路径 | ✅ 全程中文样本实测（samples/动漫测试*.mp4） |
| 双开实例 | ✅ 单实例锁实测（二开聚焦已有窗口；sidecar 端口复用机制天然兼容） |
| UI 崩溃/退出 | ✅ sidecar detached，任务不丢（M1 实测），重启自动复用 |
| 无独显机器 | ⚠️ 未在本机实测；引擎 provider 链 DML→CPU 自动回落，理论可用（慢） |
| 睡眠唤醒 | ⚠️ 未实测；sidecar/worker 独立进程，唤醒后继续，架构上安全 |
