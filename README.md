<div align="center">

<img src="design/icons/final/icon@256.png" width="120" alt="雨帧图标" />

# 雨帧（RainFrame）

**视频、图片与漫画的本地 AI 超分工作台**

[![最新版本](https://img.shields.io/github/v/release/mkktop/super_video)](https://github.com/mkktop/super_video/releases)
[![CI](https://img.shields.io/github/actions/workflow/status/mkktop/super_video/ci.yml)](https://github.com/mkktop/super_video/actions/workflows/ci.yml)
![平台](https://img.shields.io/badge/platform-Windows%2010%2F11-0078d6)
![Python](https://img.shields.io/badge/python-3.14-3776ab)
![Electron](https://img.shields.io/badge/electron-43-47848f)

[下载安装](https://github.com/mkktop/super_video/releases) · [快速上手](#快速上手) · [AI 接入](#ai-接入mcp) · [常见问题](#常见问题) · [开发](#开发)

</div>

雨帧面向 Windows，可放大视频与图片、批量处理整本漫画，并提供模型对比、补帧、视频剪切和图片去水印工具。选择素材、模型和输出方式后，即可加入处理队列。

媒体处理在本机完成，应用本身不会上传源视频或图片。更新检查、模型下载与可选加速组件需要联网；接入外部 AI 客户端后，其收到的信息和预览还受该客户端的数据处理方式影响。

本文描述当前源码。安装包的具体功能以对应 [Release](https://github.com/mkktop/super_video/releases) 为准；尚未发布的改动见 [更新说明](RELEASE_NOTES.md)。

## 安装

1. 从 [Releases](https://github.com/mkktop/super_video/releases) 下载 `RainFrame_<版本>_setup.exe`。
2. 按安装向导完成安装，再启动雨帧。安装版自带运行环境，无需另装 Python 或 Node.js。
3. 首页可查看硬件与引擎状态。需要其他模型时，在「模型库」下载，也可在创建任务后由程序下载缺失权重。

当前随包提供 AnimeJaNai V3.1 HD 的四个 x2 权重：极速 / 均衡 × 标准 / 锐利。其他模型按需下载。NVIDIA 用户可在「设置 → 处理」安装可选 TensorRT 加速组件；不安装也可使用基础处理功能。

### 数据与更新

模型、加速组件、设置、任务历史和缓存通常存放在安装目录同级的 `super_video_data/`；该位置不可写时，使用用户目录下的数据路径。升级与卸载默认保留这些数据。备份或迁移时，请一并保留数据目录；任务续跑与对比还依赖原素材和已有处理产物。

在「设置 → 系统」检查、下载并安装更新。更新下载源支持自动、R2 优先或仅 GitHub。模型主要从 ModelScope 下载，GitHub 为备用源；模型下载进行 SHA-256 校验，应用更新进行 SHA-512 校验。

## 快速上手

### 视频超分

1. 点击「新建任务」，导入一个或多个视频。
2. 选择模型。单个视频分析完成后会显示「素材推荐」，可查看依据并一键应用；推荐基于采样与规则，仍建议先检查效果。
3. 设置放大倍数或目标分辨率、编码器和输出位置。自定义分辨率会先按模型原生倍率放大，再缩放到目标尺寸。
4. 先试跑片头 20 秒，查看画质和速度；确认后将全片加入队列。
5. 在「任务队列」查看进度。排队任务可拖动排序；视频任务可取消，并利用已完成分段继续处理。

默认动漫模型为 **AnimeJaNai V3.1 HD Balanced Sharp（均衡·锐利）x2**。Sharp 是锐化风格选择，速度档也不代表画质排名；纹理、噪点与运动稳定性应结合自己的素材检查。

### 图片与漫画

- **图片超分**：选择一张或多张图片，设置模型、倍率与 PNG/JPG 输出。多张图片合并成一个任务，逐张处理；可同时生成 PDF，单张结果仍保留。
- **漫画超分**：选择整本漫画文件夹或单页图片。文件夹模式保留卷、话子目录，可生成按页序排列的 PDF；彩色与黑白页可使用不同模型，分批处理可降低显存占用。
- **图片去水印**：先用预览确认区域和处理效果，再批量处理文件夹或选中的图片。支持固定区域、样本智能定位，以及填白、白底/黑底自动识别和局部修补。结果另存 PNG，保留原图与尺寸；局部修补可能留下模糊，匹配不可靠的图片会跳过。

PDF 保留输出图片的数据，不进行二次有损压缩。去水印工具在 CPU 上运行，无需下载模型；其作业独立于超分队列，支持停止，但不提供超分任务的断点续跑。

### 不确定选哪个模型

打开「模型对比」，选择同一张图片或一段视频，勾选 2～6 个候选模型实测。视频片段最长 20 秒，候选模型需支持共同倍率。通过分割线查看细节、切换模型比较效果，再将选择用于完整任务。

## 功能一览

| 功能 | 用途 |
|---|---|
| 任务队列 | 视频、图片、漫画超分顺序处理；查看进度、取消、重试和视频分段续跑 |
| 素材推荐与预设 | 依据视频采样建议模型、倍率与预处理；保存自己的任务参数 |
| 模型库 | 按用途浏览模型、下载权重、查看兼容性，导入自定义 ONNX 模型 |
| 视频剪切 | 智能、快速无损、精确转码三种方式；片段可继续超分或对比模型 |
| 结果对比 | 源片与结果同步播放、分割线与放大镜、静帧样本、导出 PNG/GIF 对比图 |
| 预处理与补帧 | 隔行视频反交错、渐变区域去色带、RIFE 帧率倍增 |
| 输出设置 | 原生倍率或自定义分辨率；视频或 PNG/JPG 帧序列；输出命名模板 |
| 队列自动化 | 按时段或电脑空闲状态开始任务；完成后通知、关机或休眠，电源动作有取消倒计时 |
| 性能监控与运行日志 | 查看资源占用、处理速度与日志；可开启任务性能日志排查耗时 |
| AI 接入 | 通过 MCP 让支持该协议的客户端探测素材、创建任务、预览结果并查询进度 |

### 格式与限制

- 视频输入支持 SDR 8/10bit；可变帧率输入会转为固定帧率处理。**HDR 暂不支持**，请先转换为 SDR。
- 视频输出支持 H.264、H.265、AV1 软件编码，以及设备支持的 NVENC/AMF 硬件编码；容器可选 MP4/MKV/MOV。可用选项取决于硬件与素材。
- 支持保留多音轨与章节；字幕和字体的保留方式受容器限制。MKV 更适合保留原字幕与附件，MP4 文本字幕会转换格式。
- 视频可选择「烧录进画面」或「烧录并保留原字幕轨」，导入 SRT/ASS/SSA 或选择视频内文本字幕轨；支持同名文件、语言/标题的批量匹配、时间调整和画面预览。字幕在超分、补帧和最终缩放后绘制，与视频一次编码完成。ASS/SSA 保留原样式，PGS/VobSub 等位图字幕暂不支持烧录。
- 图片输入支持 PNG、JPG、WebP、BMP、TIFF；输出为 PNG 或 JPG，支持 EXIF 方向转正。
- 默认输出会避让同名文件。开启「超分完成后删除源文件」会永久删除成功任务的源文件，也会影响后续对比。
- 超分可以重建细节，也可能产生伪影。当前视频超分模型主要逐帧处理，不保证运动画面没有闪烁。

## 模型选择

模型库按主用途分类。下面列出常见选择，具体倍率、显存要求、许可证和可用后端以当前模型库及 [注册表](backend/sv/models/registry_json/) 为准。

| 素材 / 目标 | 常见模型 | 说明 |
|---|---|---|
| 动漫视频 | AnimeJaNai V3.1 HD、AnimeVideo、Ani4K v2 | V3.1 四档随包提供；其他权重按需下载 |
| 动漫重建与降噪 | Real-CUGAN / Pro、ArtCNN | CUGAN 系仅支持 TensorRT / CPU；降噪档位随模型和倍率变化 |
| 黑白漫画 | MangaJaNai | 默认黑白漫画模型，按源高度选择权重 |
| 彩色漫画与插画 | IllustrationJaNai | 默认彩漫模型为 4x DAT2；另有 2x SPAN 和 4x ESRGAN 权重 |
| 照片与通用修复 | Real-ESRGAN x4plus、HAT、SwinIR、DIS | 风格与计算成本不同，建议先对比局部效果 |
| 轻量通用放大 | RealESR-General、SeemoRe | 适合计算成本优先的场景 |
| 社区风格模型 | UltraSharp、AnimeSharp、Remacri | 适用风格不同，按素材选择 |
| 视频补帧 | RIFE | 提升帧率；需检查运动伪影，不用于提高空间分辨率 |
| 自有模型 | 自定义 ONNX | 导入时需要正确填写倍率与输入输出约定 |

不同模型权重有各自的许可，部分含非商业限制。逐模型说明见 [模型资产文档](backend/sv/models/ASSETS.md)。

## 性能

处理速度取决于模型、输入尺寸、倍率、分块、编码器、显卡和引擎缓存。TensorRT 和双路并行并非对所有模型都更快；FP16 通常更省显存，但效果与兼容性因模型而异。TensorRT 首次构建引擎可能需要数分钟。

以下仅为 **2026-08 历史实测**，设备为 RTX 5080 16GB / Ryzen 7 9800X3D，模型为 AnimeJaNai V3 HD L2，输入 1080p、输出 4K：

| 测试场景 | 记录的速度 | 测试口径 |
|---|---|---|
| DirectML FP16 优化链路 | 约 20 fps | 历史默认链路测试 |
| TensorRT 热缓存 | 51.5 fps | 短片段测试；首次构建时间另计 |
| TensorRT 单路 + 软件 H.264 | 42.0 fps | 5459 帧长片段，耗时 130.0 秒 |
| TensorRT 双路 + NVENC H.264 | 77.5 fps | 同一长片段，耗时 70.4 秒；同时改变并行与编码配置 |

短片段与长片段的数据不能直接作为统一加速比。上述结果也不代表当前默认模型、其他显卡或安装包的速度。完整过程与后续实验见 [BENCH.md](BENCH.md)。

## AI 接入（MCP）

1. 保持雨帧运行，打开侧栏「AI 接入」。
2. 确认「允许 AI 客户端接入」已开启。
3. 复制对应客户端的配置片段，按 Claude Desktop、Cursor、ZCode 等客户端的配置方式写入；其他客户端需支持 MCP stdio。
4. 重启或重新加载客户端配置，调用 `rf_status` 验证连接。

页面生成本机实际路径，通常无需手动填写端口。接入后可通过对话探测素材、下载模型、创建视频/图片/漫画任务、查询诊断和进度，也可使用模型对比、视频剪切与去水印预览和批处理。

例如：“先探测这个视频，说明推荐模型和倍率，再处理一段让我检查效果。”

超分任务进入同一处理队列；对比、剪切与去水印使用独立作业。MCP 不提供删除模型、删除任务或修改应用设置的工具。详细参数与工具说明见 [后端 MCP 文档](backend/README.md#mcp-接入ai-客户端调用超分和去水印)。

## 常见问题

**没有 NVIDIA 显卡能用吗？**

可以使用 DirectML 或 CPU。DirectML 面向支持 DirectX 12 的 NVIDIA、AMD、Intel 显卡，实际兼容性仍取决于驱动、设备与模型；CPU 处理大型模型会比较慢。

**显存不足或速度偏慢怎么办？**

先换轻量模型、调小分块，并关闭双路并行。硬件编码可减轻 CPU 负担，但并不一定加快所有任务；在「性能监控」和「运行日志」检查瓶颈，必要时开启任务性能日志。

**模型或加速组件下载失败怎么办？**

先检查网络与代理，再重试。模型下载支持断点续传与校验；TensorRT 组件可重试安装。应用更新的备用源在「设置 → 系统」选择，模型下载代理也在该分类设置。

**取消后还能继续吗？**

视频超分可利用已完成分段续跑，尚未完成的分段可能需要重新处理。请保留源文件和任务产物。剪切、模型对比与去水印作业没有同样的续跑机制。

**为什么对比入口不可用？**

确认任务已生成可用结果，且源文件和输出仍存在。删除或移动源文件会影响对比；自动删除源文件的设置也有同样影响。

**升级、卸载或迁移后模型还在吗？**

升级与卸载默认保留安装目录外的数据。换安装位置或换电脑时，需要迁移原数据目录和素材；输出文件保存在所选输出位置，不一定在数据目录内。

**启动或更新遇到拦截怎么办？**

安装包尚未签名，系统可能显示未知发布者。请核对下载来源与具体提示；无法启动时保留错误信息，通过手动安装对应 Release 或检查本地服务日志排查。

## 开发

### 环境与准备

Windows 10/11、Python 3.14、Node.js 22、pnpm 11.5.2（与当前 CI 一致）。将 FFmpeg 8.1 的 `ffmpeg.exe` 与 `ffprobe.exe` 放在仓库 `bin/`；CI 的获取方式见 [ci.yml](.github/workflows/ci.yml)。内置权重位于 `backend/sv/models/bundled/`，源码环境也需要这些文件。

以下 PowerShell 命令均从仓库根目录开始：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
pnpm --dir app install --frozen-lockfile

# 启动桌面开发程序；Electron 自动启动或复用本地后端
pnpm --dir app dev
```

运行前请退出安装版雨帧，避免单实例机制切回已有窗口。开发数据默认位于仓库根目录，包含 `models_store/`、`data/` 和 `.tmp/`。CUDA / PyTorch 开发需要独立环境；基础依赖不会自动提供所有推理后端。

### 浏览器预览与后端调试

```powershell
# 纯 UI 预览，无需 Electron 或 Python 后端
pnpm --dir app preview
# 打开 http://localhost:5199/preview.html

# 单独启动后端，便于调试 HTTP API
.\.venv\Scripts\python.exe backend\cli.py serve --port 8730
```

UI 预览使用 mock 数据和占位图，刷新后还原，不能用于验证真实处理效果，也不会进入安装包。CLI、API 和 MCP 的更多用法见 [backend/README.md](backend/README.md)。

### 验证与打包

```powershell
# 后端测试；依赖 GPU 或真实权重的项目可能按环境跳过
.\.venv\Scripts\python.exe -m pytest backend\tests -q

# 前端测试、类型检查与构建
pnpm --dir app test
pnpm --dir app build

# 打包：先构建 sidecar，再生成 NSIS 安装包
.\.venv\Scripts\python.exe -m pip install pyinstaller
Push-Location backend
try {
    ..\.venv\Scripts\pyinstaller.exe sidecar.spec --noconfirm
} finally {
    Pop-Location
}
pnpm --dir app dist
```

安装包输出至 `dist-app/`。发布前还应验证真实模型与打包产物；模型验收脚本为 `backend/scripts/release_model_audit.py`，报告脚本为 `backend/scripts/release_model_report.py`。源码测试通过不等于安装包在目标设备上通过。

发布使用稳定版 `vX.Y.Z` tag，须同步 `app/package.json`、`backend/sv/__init__.py` 与更新说明。自动测试、打包和资产同步流程见 [release.yml](.github/workflows/release.yml)；R2 / ModelScope 同步所需配置见对应工作流。

## 架构与文档

桌面端采用 Electron + Vue 3 + TypeScript；通过 HTTP / WebSocket 连接本地 FastAPI sidecar。后端管理 SQLite 任务队列，调用 FFmpeg 与 ONNX Runtime / 可选 PyTorch 引擎处理素材；MCP bridge 连接运行中的本地服务。

- [桌面端文档](app/README.md)：进程结构、页面管理与设计 token。
- [后端文档](backend/README.md)：CLI、API、MCP、任务事件与模型输入输出约定。
- [基准记录](BENCH.md)：性能与画质实验方法、数据及历史问题。
- [设计资产](design/README.md)：图标、界面设计与资源索引。
- [更新说明](RELEASE_NOTES.md)：当前版本与未发布改动；历史版本见 Releases。
- [项目规划](PLAN.md)：历史里程碑，阅读时注意其状态注记。

## 许可

本软件遵循仓库 [LICENSE](LICENSE)，免费安装、编译与使用的授权范围及商业转售、再分发限制以该文件为准。第三方组件和模型权重遵循各自的许可，部分模型仅限非商业使用；详见 [模型资产说明](backend/sv/models/ASSETS.md)。
