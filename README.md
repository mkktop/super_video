# super_video —— AI 视频超分桌面软件

低分辨率视频 → 高分辨率重建（GAN/光流补帧/PyTorch），Electron + FastAPI sidecar 架构。

## 功能

- **严格串行任务队列**：一次一个视频、每个任务参数独立（模型/倍率/编码/补帧/降噪）；排队任务可拖拽调整顺序
- **断点续跑**：取消/崩溃/重启后点"继续"从上次位置接着跑（GAN 分段 checkpoint，扩散分块 checkpoint）
- **自定义输出分辨率**：原生超分后 lanczos 精确缩放到目标宽高（只缩不放、自动取偶），
  向导自动选覆盖目标的最小原生倍率；"高级选项"可手动调分块大小（tile）
- **视频剪切**（侧栏"视频剪切"）：三种方式——智能（起点帧精确：头部转码+尾部流复制，
  移植自 AIOV smart cut）/ 快速无损（关键帧吸附，秒级）/ 精确转码；片内预览设入出点，
  剪完一键进入超分向导
- **视频对比**：对比页"静帧/视频"双模式——视频模式源/超分双路同步播放、拖动分割线
  擦除对比、可拖进度条（file:// 原生加载，Range/moov 索引位置全兼容，零转封装开销）
- **图片序列输出**：超分结果可按 PNG（无损）/JPG 逐帧导出，`000001.png` 起按帧号
  连续编号；分段全局续编号，断点续跑不重写已完成帧；两条管线（ONNX 分段/torch 分块）均支持
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

# 测试（73 项，另 1 项按环境跳过）
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
设置页"检查更新"发现新版本 → 点"下载更新"（有进度条）→ 点"立即重启"静默安装。
差分下载（blockmap）刻意禁用：实测会把旧版安装包错拼成"新更新"，全量下载 + sha512 校验更稳。

**发新版本**（推送后只需打 tag，其余全自动）：

```bash
git tag v0.1.5
git push origin v0.1.5
# 先编辑仓库根 RELEASE_NOTES.md，按 "## vX.Y.Z" 新增一节（应用内"检查更新"悬浮显示该节内容）
# GitHub Actions 自动: PyInstaller sidecar -> electron-builder -> 上传
# setup.exe / latest.yml 到对应 Release（正文 = RELEASE_NOTES.md），并同步模型资产到 models-v1
```

本地打包（不走 CI）：`backend` 下 `pyinstaller sidecar.spec --noconfirm`，再 `app` 下 `pnpm dist`。

**模型分发**：只内置小模型（AnimeVideo x2/x4 约 7MB，随仓库 `backend/sv/models/bundled/`
分发，安装包瘦身且开箱即用）；x4plus 等大模型按需从 models-v1 下载。模型市场全部下载源
统一为本仓库 `models-v1` Release（CUGAN/RIFE/pth 一次性人工上传，其余由发布流水线自动
同步），不依赖第三方直链。x4plus ONNX 由 `backend/scripts/export_onnx_x4plus.py` 从官方
权重导出（导出非字节级确定，故以仓库 models-v1 上的资产为准）。

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
| 无独显机器 | ✅ 代码级验证：DML 初始化失败回落 CPU 有单测模拟（test_target_res.py），CPU EP 端到端出片实测；真机无独显环境待外场抽检 |
| 睡眠唤醒 | ✅ 进程树挂起/恢复 6s 模拟实测通过（backend/scripts/sim_sleep_wake.py，冻结后续跑、产物完整）；真实睡眠待外场 |
| 跨版本升级 | ✅ v0.1.7 资产完整性校验（setup.exe 的 sha512/size 与 latest.yml 一致，即 updater 下载后校验必过）；Release 正文分节与 sliceNotes 切片对 0.1.3~0.1.7 全档验证（0.1.3→0.1.7 展示 0.1.7 节） |
