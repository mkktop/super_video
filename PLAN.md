# super_video 施工蓝图（M0 → M4）

> 视频超分桌面软件：Electron + Vue 前端，Python sidecar 后端，多模型可插拔。
>
> **状态注记（2026-08）**：本文档是 M0→M4 时期的施工蓝图，属历史文档——
> 实际落地与蓝图有多处偏离：Python 已用 3.14；M3 的"扩散引擎"方向经
> 实验放弃（BENCH.md 记录），转向 TensorRT 可选组件与双路并行；测试数与
> 版本细节以 README / backend/README 为准。新功能请以现状代码为准绳，
> 勿以本文档为唯一依据。

---

## 0. 项目概述

- **产品定位**：Windows 桌面视频超分工具（架构保持跨平台可能），多模型引擎（快速 GAN → 高质量扩散），按内容类型（动漫/真人）推荐模型，预设降低使用门槛。
- **对标**：VSET（Electron+VapourSynth，进程失控/模型锁死 GAN）、Video2X（界面糙/音频弱）、Topaz（收费）。
- **核心竞争力**：① 模型全档位覆盖（含扩散）② 任务进程可控可恢复 ③ 应用内模型市场 ④ 现代中文 UI + 预设。

## 1. 技术栈（开工时锁定版本）

| 层 | 选型 | 说明 |
|---|---|---|
| 前端壳 | Electron（当前稳定版）+ electron-vite | 参照 VSET / ComfyUI Desktop 先例 |
| UI | Vue 3 + TypeScript + Pinia + Vue Router | |
| 组件库 | Naive UI | TS 原生、暗色主题友好、中文文档 |
| 后端 | Python 3.11 + FastAPI + uvicorn | sidecar 常驻进程 |
| GAN 推理 | onnxruntime-directml | DirectML EP 全显卡兼容（N/A/I），CPU EP 兜底 |
| N 卡加速（可选） | onnxruntime-gpu（CUDA EP） | M3 引入，运行时按显卡加载，避免与 directml 包冲突 |
| 扩散推理 | PyTorch（CUDA 构建） | M3，作为可选组件单独下载（体积大） |
| 视频 | FFmpeg/FFprobe 静态构建（捆绑分发） | 全部走 subprocess 管道，不用 wrapper 库 |
| 队列 | SQLite（stdlib sqlite3） | 任务持久化，无外部服务依赖 |
| 进程管理 | psutil | 进程树查询与终止 |
| 打包 | electron-builder（NSIS）+ PyInstaller（onefolder） | M4 |

**依赖纪律**：后端保持最小依赖（上述清单外新增需理由）；前端按需引入。

## 2. 系统架构

### 2.1 进程模型

```
Electron 主进程
 ├─ 渲染进程（Vue UI）
 └─ 拉起并守护 ──► python sidecar（FastAPI, 常驻）
                    ├─ 任务调度器（读 SQLite 队列，串行取任务）
                    ├─ Worker 子进程（每个任务一个，psutil 管理进程树）
                    │    ├─ ffmpeg 解码 ──rawvideo rgb24 管道──► ONNX/PyTorch 推理
                    │    │                                          │
                    │    └─ ffmpeg 编码 ◄──rawvideo 管道────────────┘
                    │       （第二输入取源视频音轨，-c:a copy）
                    └─ 模型管理器（下载/sha256 校验/清单）
```

**原则**：
- 推理永远在 Worker 子进程，**UI 崩溃任务不丢，任务卡死一键杀树**；
- GPU 任务**串行**执行（多任务并发只会互相拖慢），队列排队；
- sidecar 与 Electron 解耦：`backend/cli.py` 保持独立可用（开发期 + 未来服务端复用）。

### 2.2 通信协议（localhost）

HTTP（FastAPI）：
```
POST /api/tasks                创建任务 {input, output, model_id, params}
GET  /api/tasks                任务列表（含队列位置）
POST /api/tasks/{id}/cancel    取消（杀 Worker 进程树，状态→canceled）
DELETE /api/tasks/{id}         删除记录
GET  /api/tasks/{id}/preview   取最新预览帧 jpg
GET  /api/models               模型清单（含安装状态/适配性）
POST /api/models/{id}/download 下载并校验
GET  /api/hardware             GPU/显存/CPU 检测结果
GET  /api/settings  PUT /api/settings
GET  /api/logs?task_id=        日志
WS   /ws                       事件推送（见下）
```

WebSocket 事件（sidecar → UI，500ms 节流）：
```json
{"type":"progress","task_id":"…","frames":1234,"total":45678,"fps":4.2,"eta_sec":5011,"stage":"inference"}
{"type":"task_status","task_id":"…","status":"running|done|failed|canceled","error":null}
{"type":"model_download","model_id":"…","progress":0.62}
{"type":"log","task_id":"…","line":"…"}
```

### 2.3 GAN 引擎流式管线（核心路径）

```
ffmpeg -i input -f rawvideo -pix_fmt rgb24 -sws_flags lanczos - ▼
  [Python Worker: 逐帧 read W*H*3 → BGR→模型输入→推理→输出帧 RGB] ▼
ffmpeg -y -f rawvideo -pix_fmt rgb24 -s {W*s}x{H*s} -framerate {fps} -i -
       -i input -map 0:v -map 1:a? -map 1:s? -c:v {编码器} -crf {q}
       -c:a copy -c:s copy? -movflags +faststart output
```

- **全程流式不落盘**，内存占用与视频长度无关；
- 总帧数预先 ffprobe（`-count_packets` 快速估算），进度=已处理帧/总帧；
- 大图按模型 `tile_hint` 切重叠 tile（overlap 默认 16px）；
- 预览帧：Worker 每 5s 写一张 `preview.jpg` 到任务临时目录，UI 轮询展示。

### 2.4 扩散引擎分块管线（M3）

- 解码整段为帧序列（临时目录，PNG 8bit）；
- 按块（32 帧滑窗，重叠 4 帧）推理，输出帧落盘；
- **checkpoint 文件记录已完成块**，中断重跑跳过已完成块（这是扩散引擎独有优势）；
- 完成后 ffmpeg 图片序列编码 + 音轨合成，清理临时目录。
- 临时目录磁盘占用预估展示给用户（帧数 × 输出分辨率 × ~1.5MB）。

## 3. 数据模型

### 3.1 SQLite 任务表

```sql
CREATE TABLE tasks (
  id TEXT PRIMARY KEY,            -- uuid
  created_at REAL, input_path TEXT, output_path TEXT,
  model_id TEXT, params TEXT,     -- json: scale, encoder, quality, tile, denoise...
  status TEXT,                    -- queued|running|done|failed|canceled
  progress_frames INTEGER DEFAULT 0, total_frames INTEGER DEFAULT 0,
  fps REAL, eta_sec REAL, checkpoint TEXT,  -- 扩散引擎块进度 json
  error TEXT, log_tail TEXT, preview_path TEXT
);
```

### 3.2 模型 manifest（`models/registry/*.json`，用户自定义模型放用户目录）

```json
{
  "id": "realesr-animevideov3",
  "name": "AnimeVideo v3",
  "vendor": "TencentARC", "license": "BSD-3-Clause",
  "engine": "onnx",               // onnx | torch
  "family": "realesrgan",
  "scale": [2, 3, 4],
  "content": ["anime"],
  "temporal": false,
  "speed": "fast",                // fast | balanced | slow
  "vram_gb": 2,
  "tile_hint": 0,                 // 0=不需分块
  "description": "动漫视频专用，速度最快，近实时",
  "files": [{ "url": "…onnx", "sha256": "…", "size_mb": 17 }],
  "mirror_urls": ["…"]            // 国内镜像（M4 启用）
}
```

首期注册表：`realesr-animevideov3`（x2/x3/x4）、`realesrgan-x4plus`、`realesrgan-x2plus`、M3 增 `real-cugan`、`rife-v4`、扩散模型（FlashVSR 优先，SeedVR2-3B 视显存）。ONNX 权重来源：vs-mlrt-external-models 与各官方 release，入库前统一校验。

### 3.3 设置（JSON 文件）

临时目录、默认输出目录、编码默认值、引擎后端偏好（auto/cuda/directml/cpu）、日志级别。

## 4. 里程碑施工

### M0（第 1–2 周）：后端 CLI 核心管线

**目标**：不写一行 UI，先在真机验证质量、速度、内存三条硬指标。

施工分解：
1. `backend/` 工程骨架 + 依赖锁定（requirements.txt + venv 脚本）；ffmpeg 静态构建放入 `bin/`。
2. `pipeline/probe.py`：ffprobe 封装 → 输入信息（分辨率/时长/帧率/编码/bit depth/音轨/字幕/VFR 检测）。
3. `pipeline/stream.py`：双 ffmpeg 子进程 + 逐帧读写循环（asyncio），帧计数、限速、优雅取消（杀进程树）、错误透传（ffmpeg stderr 尾部入日志）。
4. `pipeline/tile.py`：重叠 tile 切分/拼合（单元测试重点：任意尺寸往返无损）。
5. `engines/base.py` 定义引擎接口；`engines/onnx_engine.py` 实现加载（EP 顺序 cuda→dml→cpu 自动回退）与推理。
6. `models/` 注册表 + 下载校验工具（CLI 手动触发）。
7. `cli.py`：`sv run <input> -m <model> -s 4 -o <out> [--crf 18] [--tile 256]`，rich 进度条 + fps + ETA。
8. 测试：`tests/` ①tile 往返 ②管道 IO 往返（ffmpeg→python→ffmpeg 无推理）③端到端：脚本生成 10s 合成视频 → 超分 → 校验输出分辨率/时长±1帧/音轨存在/无音画漂移。

**验收标准**：
- 1080p 10s 动漫片段 animevideov3 x4 跑通，音轨保留；
- 处理 5 分钟视频内存曲线平稳（无随时长增长）；
- 产出本机速度基准表（两模型 × 720p/1080p × x2/x4 的 fps）——此表决定后续 UI 文案与推荐档位；
- 明确限制并记录：SDR 8bit + CFR（检出 HDR/VFR/10bit 时拒绝并提示）。

### M1（第 3–4 周）：sidecar 服务 + Electron 壳跑通端到端

施工分解：
1. `server/app.py` FastAPI + WS；`server/queue.py` SQLite 队列（启动时 running→queued 恢复）；`server/runner.py` 调度器（串行取任务 → spawn Worker 进程 → 回收）。
2. Worker 进程管理：psutil 杀树、崩溃分类（显存不足/解码失败/用户取消），错误写回任务表。
3. Electron 工程（electron-vite + Vue3 + Naive UI 暗色）：主进程拉起/守护 sidecar（崩溃自动重启，端口冲突检测）。
4. UI 最小集：任务列表页（状态徽章/进度条）+ 新任务表单（文件选择、模型下拉、倍数、输出路径）+ 日志抽屉。
5. 端到端联调：UI 提交 → 队列 → WS 进度 → 完成通知。

**验收标准**：
- 连续提交 3 个任务自动排队串行执行；
- 任务运行中杀掉 Electron 重开，任务仍在跑/队列仍在（sidecar 独立存活）；
- 取消任务 1 秒内进程树清空（任务管理器验证无残留 ffmpeg/python）；
- 人为杀 Worker，任务标记 failed 且错误信息可读，下一个任务正常启动。

### M2（第 5–6 周）：产品化——模型市场 / 预设 / 完整向导

施工分解：
1. 模型管理器：下载（断点续传）、sha256 校验、删除、并发下载 1 个；模型目录管理（内置 registry + 用户自定义目录）。
2. 硬件检测：GPU 型号/显存/CPU（`GET /api/hardware`），模型适配性计算（显存不足灰显 + 原因 tooltip）。
3. 预设系统：`动漫快速 / 动漫高清 / 真人修复` 一键档（模型+倍数+编码组合，预设存 manifest 化的 JSON）。
4. 新任务向导完整版（三步式，见 UI 设计 5.2）。
5. 硬件编码器：检测 NVENC，编码器选项出现 `H.264 (硬编)`，压制速度大幅提升。
6. 预览对比：完成后首帧 before/after 滑动对比条；处理中实时预览帧轮询。
7. VFR/10bit/HDR 支持评估落地（VFR：ffmpeg `-fps` 时基传递方案；10bit：管线升级 rgb24→rgb48 或 yuv444p16 中转，按 M0 记录的实际情况决定）。

**验收标准**：全新用户路径 ≤ 60 秒完成"装软件→下模型→出第一个片"（模型下载除外）；显存不够的模型不可选且说明原因。

### M3（第 7–9 周）：扩散引擎 + 补帧 + 全模型矩阵

施工分解：
1. PyTorch 引擎适配器（`engines/torch_engine.py`），PyTorch CUDA 作为**可选组件**（安装器选项 / 应用内下载，不装不影响 GAN 路径）。
2. 接入 **FlashVSR**（优先，轻量）+ **SeedVR2-3B**（按用户显存决定是否提供）；分块滑窗 + checkpoint 续跑（3.4 节管线）。
3. `real-cugan`（ONNX）与 `rife-v4`（ONNX 补帧，作为第二流水线阶段：超分→补帧）。
4. 流水线组合引擎：SR →（可选）补帧，进度分阶段展示。
5. 显存自适应：块大小/分辨率自动降档策略 + 失败重试（半块重算）。

**验收标准**：扩散引擎 10s 片断跑通且中断重跑跳过已完成块；显存不足时自动降档或给出明确指引；补帧后帧率翻倍且音画同步。

### M4（第 10–11 周）：打包分发

施工分解：
1. PyInstaller（onefolder）打 sidecar，**本地全量回归**（引擎各跑一个任务）。
2. electron-builder NSIS 安装包：图标/签名占位/安装协议/静默参数；捆绑 ffmpeg + onnxruntime-directml + 首发两模型；PyTorch 与其余模型走应用内下载。
3. electron-updater 自动更新（GitHub Releases + 国内镜像 URL 备胎）。
4. 模型下载镜像源（ghproxy 类镜像 + 自备 CDN 可选）。
5. 自定义模型导入 UI（选 ONNX → 表单生成 manifest → 校验能跑 → 入库）。
6. 灾难场景测试：无显卡机器、路径含中文/空格、睡眠唤醒中断、双开实例检测。
7. 体积优化：安装包目标 < 400MB（不含 PyTorch 与模型）。

**验收标准**：干净 Windows 机器（含无独显机器）安装即用；升级流程验证；崩溃日志可收集（入口在设置页导出）。

## 5. UI 设计规范

### 5.1 设计原则与视觉

- **暗色为唯一主题**（视频工具惯例，长时间盯不累）；
- 配色 token：背景 `#141517` / 面板 `#1E2023` / 边框 `#2A2D31` / 主文字 `#E8EAED` / 次文字 `#9AA0A6` / 主色 `#4F8CFF` / 成功 `#34D399` / 警告 `#FBBF24` / 危险 `#F87171`；
- 圆角 8px，卡片阴影轻，中文字体栈 `system-ui, "Microsoft YaHei"`，日志/数据用等宽；
- 状态徽章色：排队=灰 / 运行=主色脉冲 / 完成=绿 / 失败=红 / 取消=黄。

### 5.2 页面与线框

**① 主页（任务中心）**——启动即达

```
┌──────────────────────────────────────────────────────────────┐
│ ⬆ super_video        任务        模型市场        设置    [硬件●RTX] │
├──────────────┬───────────────────────────────────────────────┤
│ 任务列表      │  ┌─────────────────────────────────────────┐  │
│              │  │ ▶ 新建任务（拖入视频到此处）                  │  │
│ ● 片头.mp4   │  └─────────────────────────────────────────┘  │
│   63% 运行中  │  当前任务                                     │
│ ○ ep02.mkv   │  ┌─────────────────────────────────────────┐  │
│   排队 #2     │  │ 片头.mp4  →  片头_4K.mp4    AnimeVideo v3 4x │  │
│ ✓ demo.mp4   │  │ ████████████░░░░░░░░░░  63%  4.2 fps      │  │
│              │  │ 12,470 / 19,800 帧   剩余 28 分钟          │  │
│              │  │ [预览对比]  [日志 ▾]  [取消]               │  │
│              │  └─────────────────────────────────────────┘  │
└──────────────┴───────────────────────────────────────────────┘
```

**② 新任务向导（三步）**

- 顶部预设条：`⚡ 动漫快速` `🎬 动漫高清` `✨ 真人修复`（一键填满后续所有选项）
- Step1 输入：拖拽/选择文件 → 信息卡（原始分辨率/时长/帧率/编码，输出预估 `1920×1080 → 3840×2160`）
- Step2 模型：Tab（动漫/真人/全部）× 卡片网格；每卡=名称、速度档图标（⚡/⚖️/🐢）、显存需求、一句话描述；不适配卡片灰显+原因
- Step3 输出：倍数（模型支持的亮，其余灰）、编码器（H.264/H.265/硬编）+ 质量滑条（推荐档预选）、输出路径（默认 `输入目录/文件名_4K.mp4`）
- 底栏：`← 上一步   创建任务（加入队列）`

**③ 模型市场**：已安装/可下载分组；每行=图标+名称+大小+速度/显存标签+状态（✓ 已安装 / 下载中 62% / [下载] / [删除]）；自定义模型入口（右上角"导入自定义模型"）。

**④ 设置**：硬件信息卡（GPU/显存/后端当前选择）；引擎后端（自动/NVIDIA CUDA/DirectML/CPU）；临时目录与磁盘余量显示；默认输出与编码偏好；日志查看器（全局+按任务）；关于/检查更新。

### 5.3 交互细节清单

- 拖拽文件到窗口任意位置直接开向导；支持多选批量入队；
- 完成通知（系统级 + 应用内）；完成后卡片直接提供"打开所在文件夹"；
- 取消需二次确认（含"不再提醒"）；删除已完成任务可选同时删产物；
- 所有 ffmpeg/推理日志可展开查看（排障刚需，学习 VSET 的透明化输出）；
- 空状态文案引导（"还没有模型？去模型市场看看"一键跳转）。

## 6. 测试策略

- **单元**：tile 切拼往返、帧 IO 字节精确、manifest 校验、队列状态机；
- **集成**：管道无推理往返、每引擎 10s 端到端、取消/杀进程残留检查（枚举进程名断言）；
- **基准**：`bench/` 脚本输出速度表（M0 建立后每里程碑回归）；
- **人肉清单**：中文路径/网络盘路径/超长视频/无显卡/睡眠唤醒——M4 前过一遍并留档。

## 7. 风险与预案

| 风险 | 等级 | 预案 |
|---|---|---|
| onnxruntime 双包冲突（cuda/directml 同名包） | 高 | M0-M2 只用 directml 单包；M3 起 CUDA 版放独立目录运行时选择加载 |
| 10bit/HDR/VFR 边角（VapourSynth 社区趟过的坑） | 高 | M0 明确只收 SDR8bit+CFR 并检测拒绝；M2 专项扩展，测试用例先行 |
| PyTorch 打包体积/兼容性 | 中 | 扩散引擎=可选组件按需下载，主安装包不背 |
| 模型下载慢（国内 GitHub） | 中 | M4 镜像源；安装包首发捆绑 2 个 GAN 模型保证开箱即用 |
| 显存溢出（扩散） | 中 | 块自动降档 + 失败重试；UI 显存过滤前置 |
| ffmpeg 进程残留 | 中 | psutil 杀树 + job object 思路兜底；M1 验收专项 |

## 8. 时间线总览

| 里程碑 | 周 | 出口物 |
|---|---|---|
| M0 | 1–2 | CLI + 两模型 + 速度基准表 + 硬限制清单 |
| M1 | 3–4 | sidecar + Electron 壳，端到端可用，进程可控 |
| M2 | 5–6 | 模型市场/预设/完整向导/预览对比，产品脸面成型 |
| M3 | 7–9 | 扩散引擎 + Real-CUGAN + RIFE 补帧 + checkpoint 续跑 |
| M4 | 10–11 | 安装包/自动更新/自定义模型/镜像，可发布 |

> 施工顺序即本文章节顺序；每完成一个里程碑，先过验收标准再前进，基准表回归一次。
