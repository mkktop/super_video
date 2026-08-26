<div align="center">

<img src="design/icons/final/icon@256.png" width="120" alt="super_video 图标" />

# super_video

**AI 视频超分辨率桌面软件 —— 低清视频一键变高清**

[![最新版本](https://img.shields.io/github/v/release/mkktop/super_video)](https://github.com/mkktop/super_video/releases)
[![CI](https://img.shields.io/github/actions/workflow/status/mkktop/super_video/ci.yml)](https://github.com/mkktop/super_video/actions/workflows/ci.yml)
![平台](https://img.shields.io/badge/platform-Windows%2010%2F11-0078d6)
![Python](https://img.shields.io/badge/python-3.14-3776ab)
![Electron](https://img.shields.io/badge/electron-43-47848f)

</div>

## 简介

super_video 是一款面向 Windows 的 AI 视频超分辨率桌面软件：通过 GAN 超分模型将低分辨率视频重建为高分辨率画面，并内置补帧、剪切、对比等完整工作流。软件采用 Electron + FastAPI sidecar 架构，全部推理在本机完成，视频数据不出设备。

内置模型开箱即用，无需配置环境；NVIDIA 用户可在设置页一键安装 TensorRT 加速组件，配合双路并行与硬件编码，4K 超分可达到实时处理级别（详见[性能](#性能)）。

## 特性

- **向导式新建任务**：选择视频 → 选择模型 → 设置输出，几步完成；任务参数完全独立（模型/倍率/编码/补帧/降噪）
- **严格串行任务队列**：一次处理一个视频，排队任务可拖拽调整顺序，资源占用可预期
- **断点续跑**：取消、崩溃或重启后点击"继续"，从上次位置接着跑（分段 checkpoint），已完成的帧不会重写
- **自定义输出分辨率**：原生超分后 lanczos 精确缩放到目标宽高（只缩不放、自动取偶），向导自动选择覆盖目标的最小原生倍率
- **多模型支持**：动漫（AnimeVideo / AnimeJaNai / Real-CUGAN）、真人（Real-ESRGAN x4plus）、补帧（RIFE），并支持导入自定义 ONNX 模型（含 fp16）
- **多后端推理**：DirectML 默认（全显卡兼容）、CUDA、TensorRT（引擎缓存 + 自动回退）、PyTorch；FP16 默认精度（提速 1.36~2.05x）
- **双路并行**：两个进程分段同时处理，配合硬件编码器吞吐最高提升约 84%
- **视频剪切**：智能（起点帧精确）/ 快速无损（关键帧吸附，秒级）/ 精确转码三种方式，剪完一键进入超分向导
- **视频对比**：源片与超分结果双路同步播放，拖动分割线擦除对比，进度条全程可拖
- **图片序列导出**：结果按帧导出 PNG（无损）/JPG，断点续跑不重写已完成帧
- **输入兼容**：8/10bit、VFR（自动转 CFR）；HDR 暂不支持（明确报错，不会静默出错片）
- **输出规格**：H.264/H.265/AV1（SVT）软编或 NVENC/AMF 硬编；MP4/MKV/MOV 容器；音轨保留或指定 AAC/FLAC；MKV 字幕轨原样保留，MP4 文本字幕自动转 mov_text

## 性能

以下数据均为真机实测（RTX 5080 16GB / Ryzen 7 9800X3D / 1080p→4K，AnimeJaNai V3 HD L2），完整方法论见 [BENCH.md](BENCH.md)。

| 处理链路 | 端到端 fps | 说明 |
|---|---|---|
| DirectML（默认，fp16 + 图手术 + 流水线重叠） | ~20 | 基线 |
| TensorRT 加速组件（引擎热缓存） | **51.5** | ≈2.5x；冷启动含引擎构建为 43.2 |
| 双路并行 + NVENC 硬编 | **77.5** | 相对单路硬编（42.0）+84% |

单项加速手段的实测效果：

- **TensorRT**：单帧推理 36.7ms → 13.9ms（2.6x），引擎缓存首任务构建数十秒、之后秒级加载
- **FP16 精度**：推理提速 1.36~2.05x（权重体积精确减半，输出数值级一致）
- **uint8 图手术 + 流水线重叠**：5.65 → 20.2 fps（3.6x，CPU 前后处理不再阻塞 GPU）
- **双路并行**：两个进程各处理一半分段，GPU 利用率从 ~50% 升至饱和（65% avg / 99% max）

画质收益（原片 1080p 降采样至 480p 后 x4 超分，与原片对比；AnimeVideo v3 相对 lanczos 直接放大基线）：

| 指标 | lanczos 基线 | super_video | 提升 |
|---|---|---|---|
| PSNR | 21.54 dB | **23.54 dB** | +2.0 dB |
| SSIM | 0.916 | **0.939** | +0.023 |

说明：双路并行仅配合硬件编码器有收益（软编场景 CPU 已饱和，实测无增益）；短于约 4800 帧的视频自动不启用并行（双引擎加载成本大于收益）。16GB 显存实测可直出 8K 分辨率（7680x4320）无显存问题。

## 安装与使用

### 安装

从 [Releases](https://github.com/mkktop/super_video/releases) 下载 `super_video_0.2.4_setup.exe`（基础包约 240MB），双击安装，无需管理员权限。

模型、TensorRT 组件、设置与任务历史统一存放在安装目录同级的 `super_video_data/` 目录——**升级或重装不会丢失任何数据**。应用内置自动更新：设置页检查更新 → 下载（带进度条）→ 重启静默安装，全程 sha512 校验。

### 快速上手

1. **新建任务**：在首页选择视频文件，进入超分向导
2. **选择模型**：内置 AnimeVideo x2/x4 开箱即用；更多模型在"模型市场"按需下载，下载完成即自动接入
3. **设置输出**：选择目标分辨率（自动取覆盖目标的原生倍率，可精确指定宽高）、编码器与容器
4. **开始处理**：任务进入串行队列，运行卡实时显示帧率与预计剩余时间；随时可取消，之后点"继续"从断点接着跑

### 可选加速（设置页）

- **TensorRT 加速组件**：NVIDIA 显卡（2018 年及之后架构）一键安装，约 1.5GB 按需下载，推理速度最高约 2.5 倍；基础安装包体积不变
- **双路并行**：两个进程分段同时处理，配合硬件编码器效果最佳；显存占用约增加一倍，可按需开关
- **精度**：FP16 默认启用，个别模型数值异常时可切回 FP32

### 附加工具

- **视频剪切**：三种剪切方式，片内预览设入出点，剪完一键进入超分向导
- **视频对比**：静帧/视频双模式，源片与超分结果同步播放、分割线擦除对比
- **图片序列导出**：PNG/JPG 逐帧导出，按帧号连续编号

## 模型支持

| 模型 | 倍率 | 适用内容 | 说明 |
|---|---|---|---|
| AnimeVideo v3 / xs x2 | x2 / x4 | 动漫 | 内置，开箱即用，速度最快 |
| AnimeJaNai V2 / V3 HD | x2 | 动漫 | L1 极速 ~ L3 画质三档可选 |
| Real-CUGAN | x2 / x3 / x4 | 动漫 | 降噪档位：不降噪 / 1 / 2 / 3+保守 |
| Real-ESRGAN x4plus | x4 | 真人 / 通用 | ONNX 与 PyTorch 双版本（PyTorch 版支持分块断点续跑） |
| RIFE v4.26 | 补帧 x2 | 通用 | 帧率倍增，提升画面流畅度 |
| 自定义 ONNX | 任意 | 通用 | 导入自有模型，支持 fp16 本体 |

内置小模型随安装包分发，其余模型全部从本项目 GitHub Release 下载（sha256 校验，不依赖第三方直链）。

## 开发

### 环境要求

- Windows 10/11，Python 3.14，Node.js 与 pnpm
- ffmpeg/ffprobe（BtbN 8.1 构建）放入 `bin/`

### 从源码运行

```bash
# 1) 后端 sidecar
python -m venv .venv
.venv/Scripts/pip install -r backend/requirements.txt

# 2) 前端 + 桌面壳（Vue 3 + Electron 43）
cd app && pnpm install && pnpm build && npx electron .
```

开发模式下数据目录为仓库根目录（打包版才使用安装目录外的 `super_video_data`）。

### 测试

```bash
cd backend && ../.venv/Scripts/python.exe -m pytest tests/ -q
# 156 通过 + 1 跳过（无 GPU/部分模型缺失时按机器跳过）
```

### 打包发布

```bash
# 1) sidecar（PyInstaller onedir）
cd backend && ../.venv/Scripts/pyinstaller.exe sidecar.spec --noconfirm

# 2) 安装包（electron-builder NSIS）
cd app && pnpm dist
```

发新版本：更新仓库根 `RELEASE_NOTES.md`（应用内"检查更新"会展示其内容）→ 推送后打 tag → GitHub Actions 自动完成 sidecar 打包、安装包构建、Release 上传与模型资产同步。

## 架构

```
┌──────────────────────────────────────┐
│  Electron 桌面端（Vue 3 + TypeScript） │  界面 / 任务管理 / 设置 / 自动更新
└─────────────────┬────────────────────┘
                  │ HTTP + WebSocket
┌─────────────────▼────────────────────┐
│  FastAPI sidecar（PyInstaller 冻结）  │  SQLite 串行任务队列 / 分段 checkpoint
└─────────────────┬────────────────────┘
                  │
          ┌───────┴────────┐
          │  推理引擎       │  ONNX Runtime（DirectML / CUDA / TensorRT）+ PyTorch
          │  FFmpeg 编解码  │  x264 / x265 / SVT-AV1 软编 + NVENC / AMF 硬编
          └────────────────┘
```

## 相关文档

- [BENCH.md](BENCH.md) — 基准测试方法论与全部实测数据（IO 校准、质量评估、各加速手段 A/B）
- [PLAN.md](PLAN.md) — 项目规划与里程碑
- [RELEASE_NOTES.md](RELEASE_NOTES.md) — 版本更新说明
