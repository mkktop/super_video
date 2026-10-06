# 雨帧 backend（RainFrame）

视频超分核心管线：ffmpeg 流式管道 + ONNX Runtime / PyTorch / TensorRT 推理 + FastAPI sidecar + CLI。里程碑范围见根目录 `PLAN.md`。

## 环境

```bash
cd D:\work\super_video
python -m venv .venv            # 已就绪则跳过
.venv\Scripts\python -m pip install -r backend\requirements.txt
```

ffmpeg/ffprobe 不入版本库，首次搭建时下载（**BtbN 8.1 正式版**，勿用 master 版——master 的 NVENC 要求驱动 ≥610，正式版兼容性好）：

```bash
curl -L -o ffmpeg.zip https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n8.1-latest-win64-gpl-8.1.zip
unzip ffmpeg.zip -d ffmpeg_tmp && cp ffmpeg_tmp/ffmpeg-n8.1-latest-win64-gpl-8.1/bin/*.exe ../bin/ && rm -rf ffmpeg_tmp ffmpeg.zip
```

模型权重在 `models_store/`（同样不入库，用 `cli.py models download` 获取）。

## CLI 用法

```bash
cd backend
py=../.venv/Scripts/python.exe

# 探测媒体信息（SDR 8/10bit，可变帧率处理时转固定帧率）
$py cli.py probe ../samples/xxx.mp4

# 生成合成测试视频
$py cli.py gen ../.tmp/test.mp4 -w 854 -h 480 --duration 10

# 模型管理
$py cli.py models list
$py cli.py models download realesr-animevideov3

# 超分（默认输出到输入同目录 *_4x.mp4）
$py cli.py run ../.tmp/test.mp4 -m realesr-animevideov3
$py cli.py run xxx.mp4 -m realesrgan-x4plus --tile 64 --crf 17

# 启动 sidecar 服务（Electron 会自动拉起；手动调试用）
$py cli.py serve --port 8730
```

其余子命令 `worker` / `ort-check` / `selftest` 为打包链路与自检内部使用，日常开发不需直接调用；`mcp` 见下节。

## MCP 接入（AI 客户端调用超分和去水印）

`sv/mcp_server.py` 是一个 stdio MCP server，把运行中 sidecar 的能力暴露给 Claude Desktop / ZCode / Cursor 等 AI 客户端——对话式完成「探测 → 选模型 → 建任务 → 轮询进度」。

**前置条件：雨帧须在运行**。bridge 只代理不拉起（自拉 headless sidecar 会与 Electron 版形成双 runner 抢 GPU 队列与 SQLite）。发现逻辑与 Electron 主进程同款：扫描 127.0.0.1:8730-8739、`/api/health` 健康标记校验、版本一致者优先；鉴权令牌自动从 SV_TOKEN / 数据根 token 文件候选逐个试探（frozen 态从 exe 位置复刻 `resolveDataRoot` 候选序列），UI 重启轮换令牌时自动重读重试一次。

**准入总闸**：settings `mcp_enabled`（默认开）——sidecar 中间件对 bridge UA（`rainframe-mcp/*`）在关闭时返回 403，文案自解释由 bridge 转告；`/api/health` 豁免（bridge 靠它发现 sidecar 才能报出「去开闸」而非表现为连不上）；普通 UI 流量不受闸门影响（仅 bridge UA 才读设置文件）。侧栏「AI 接入」页（日志下方）提供状态、开关与三段预填本机路径的客户端配置片段（`backend:info` 的 `mcpCommand`：安装版 `sidecar.exe mcp`、dev 为仓库 venv python 跑 `cli.py mcp`），另附「其他支持 MCP 的客户端」指令块——跨客户端无统一深链标准，通用路径是把接入指令（命令行+验证步骤+两种报错指引）发给 AI，由它写自己宿主的配置并自行验证。

客户端配置示例（stdio；下例适用于使用 mcpServers 字段的客户端，其他客户端按各自格式配置）：

```jsonc
{
  "mcpServers": {
    "rainframe": {
      "command": "C:\\...\\RainFrame\\resources\\sidecar\\sidecar.exe",  // 安装版
      "args": ["mcp"]
    }
  }
}
// 开发版请复制「AI 接入」页生成的配置：command 为 venv python，args 含 cli.py 的绝对路径与 "mcp"。
```

工具面 23 个（`rf_` 前缀）：

| 功能 | 工具 |
|---|---|
| 状态、媒体探测、模型 | `rf_status`、`rf_probe`、`rf_models`、`rf_model_download` |
| 超分任务 | `rf_task_create`、`rf_tasks`、`rf_task`、`rf_task_cancel`、`rf_task_resume`、`rf_scan_folder` |
| 诊断与结果预览 | `rf_diagnostics`、`rf_task_preview` |
| 图片去水印 | `rf_watermark_preview`、`rf_watermark_batch`、`rf_watermark_job`、`rf_watermark_cancel` |
| 模型对比 | `rf_compare_create`、`rf_compare_job`、`rf_compare_cancel`、`rf_compare_preview` |
| 视频剪切 | `rf_trim_create`、`rf_trim_job`、`rf_trim_cancel` |

超分任务创建立即返回任务 id，客户端轮询 `rf_task` 直到终态。整夹超分使用 `input_folder`，bridge 自动设置 `folder_src`，保留章节目录结构；显式 `extra_params.folder_src` 优先。`rf_tasks` 默认每页 30 条，`limit` 为 1~100，使用 `next_offset` 继续读取，保持 status/q 不变；列表变化时 offset 分页位置可能变化。

工具在请求后端前校验声明的参数类型、枚举、范围和必填项，不进行类型转换；例如 `overwrite="false"` 会返回协议错误 -32602，不会创建任务。编码枚举与后端共用常量，软件 H.265 为 `h265`，硬件编码需对应硬件能力；分块 `tile` 越小越省显存。

`rf_diagnostics` 返回队列闸门、最近性能样本及日志尾；可选 task_id 附带该任务详情和性能日志尾。log_lines 默认 80、上限 200，perf_limit 默认 20、上限 60，单行日志限制 2000 字符。性能日志 API 的可选 `n` 参数从文件尾读取，上限 500 行、最多读取 1 MiB；不传 n 保持原有完整日志接口。

`rf_task_preview` 返回源图/结果的 MCP 原生 PNG，最长边 1200 像素；提供 sample_index 则读取同时间的静帧，初次可能返回 building，需要稍后重试，unsupported 时省略索引回落普通预览。模型对比使用 `rf_compare_create` 创建 2~6 个不同模型的短片段或图片对比（共同支持的倍率，torch 暂不参与），查询用 `rf_compare_job`，结果图用 `rf_compare_preview`；视频索引需小于 still_count，单片段最长 20 秒。视频剪切用 `rf_trim_create`，查询用 `rf_trim_job`，完成后的 output 可作为超分输入。对比、剪切和去水印作业 id 均与超分任务分开使用；各自的 cancel 工具遵循后端取消粒度，不提供断点续跑。

去水印工具复用界面同一套后端：`mode=fixed|smart`、`removal=auto|repair|white`、右下角区域 `mask`（`unit=px|percent`，`width/height/right/bottom`）、`sample={path,mask}` 和 `threshold` 均可传入。默认 `fixed+auto`；smart 必须提供纯色页边上的同款水印样本，fixed+repair 可选样本以按文字形状生成遮罩，没样本时修补整个框。repair 可能留下模糊，不可靠的智能匹配仍会跳过。

先调用 `rf_watermark_preview` 检查：默认返回定位/清除方式等文字信息及原图、处理后两张 MCP 原生 PNG 图像；`include_images=false` 只返回信息，不把 base64 塞入文本。再用相同参数调用 `rf_watermark_batch`，传 `paths` 或 `folder`（未传 paths 时自动递归扫描，清单不经过 AI 上下文）；可指定 `output_dir` 作为输出父目录。结果保留原图、尺寸和相对目录结构，另存 PNG，同名目录自动避让。

去水印作业独立于超分队列：用返回的 `id` 作为 `job_id` 调用 `rf_watermark_job`，轮询到 `done/cancelled`，并检查 `succeeded/skipped/failed`。用 `rf_watermark_cancel` 请求停止，当前图片处理完后退出，已有结果保留；此 id 不能传给 `rf_task`，也不支持 `rf_task_resume`。错误最多回前 20 条，并返回总数；智能定位或增强清除的完整记录见结果目录中的 `watermark-report.json`。

协议层使用 Python stdlib 实现（initialize / tools/list / tools/call / ping 的逐行 JSON-RPC），图像预览惰性使用项目已有 Pillow，不引入官方 mcp SDK。PyInstaller 经 cli.py 惰性 import 自动发现。安全边界：不暴露 delete_model / remove_task / reorder / settings 写等端点；输出纪律：任务列表每页最多 100 条、扫描预览最多 20 文件、images 清单超过 20 条只回计数+样例，图片最长边 1200 像素，单次原始预览/日志读取上限 32 MiB。

## 桌面端（app/）

```bash
cd app && pnpm install && pnpm build && npx electron .   # 或 pnpm dev
```

## 代码结构

```
sv/
├─ paths.py            项目路径 / ffmpeg 定位 / 数据目录迁移
├─ mcp_server.py       MCP stdio bridge（AI 客户端接入：发现/鉴权/协议层零依赖手写）
├─ pdfmerge.py         批量图片 → 单份 PDF 无损封装（Flate+PNG 预测器 / JPEG 直嵌，零依赖手写 PDF 对象）
├─ pipeline/
│  ├─ probe.py         ffprobe 封装 + 探测缓存（接受 10bit/VFR→CFR 化，拒绝 HDR）
│  ├─ stream.py        核心流式管线（解码→逐帧/批量推理→编码，管道不落盘）
│  ├─ segmented.py     分段流式管线（checkpoint 断点续跑 + 双路分片）
│  ├─ chunked.py       torch 引擎的分块 checkpoint 管线
│  ├─ tile.py          大图分块（重叠切分 + 无缝拼合）
│  └─ trim.py          视频剪切（smart/fast/exact 三模式，可取消）
├─ engines/
│  ├─ base.py          引擎接口 + 分块调度
│  ├─ onnx_engine.py   ONNX 引擎（EP 回退链、u8 图手术、批量、io 仿射/pad 分档）
│  ├─ torch_engine.py  PyTorch 引擎（CUDA 环境，fp16 autocast）
│  ├─ trt_runtime.py   TensorRT 可选组件的发现/激活
│  ├─ nvidia_dlls.py   NVIDIA DLL 目录注册（CUDA/TRT 运行库定位）
│  ├─ u8_wrap.py       uint8 直进直出图手术（前后处理 GPU 化）
│  └─ rife.py          RIFE 补帧（RGB uint8 契约）
├─ server/
│  ├─ app.py           FastAPI 装配（lifespan/token 鉴权/WS）；路由在 routes/ 按域拆分
│  ├─ routes/          system / models / tasks / trim / compare 五域路由
│  ├─ state.py         进程级单例（bus/runner/perf/下载锁/硬件缓存）
│  ├─ consts.py        任务与预设校验共用常量
│  ├─ gpu_lease.py     GPU 租约：队列任务与对比/剪切作业显存互斥（辅助作业插队）
│  ├─ runner.py        串行任务调度（取消杀树、退出语义、孤儿清杀）
│  ├─ worker.py        任务 worker 进程入口（视频路径+双路编排）
│  ├─ worker_common.py worker 事件输出 + 性能日志
│  ├─ worker_engine.py onnx 引擎装配（fp16/TRT 激活/降档重试）
│  ├─ worker_image.py  图片作业（单张/批量）
│  ├─ db.py            SQLite 任务表（WAL）
│  ├─ settings.py      应用设置持久化与校验（含默认输出目录）
│  ├─ hardware.py      GPU/CPU 硬件检测
│  ├─ compare.py       模型对比作业（独立线程：切段/静帧 × 多模型，不占任务队列）
│  ├─ task_stills.py   任务对比页多帧静帧（懒构建缓存：源/输出同时间戳成对抽帧）
│  ├─ engine_select.py 解释器/后端选择（CUDA/TRT/DML 探测）
│  ├─ trt_component.py TRT 组件安装/卸载/状态
│  ├─ perf.py          性能采样（CPU/GPU/任务资源）
│  └─ events.py        WS 广播总线（线程安全发布）
├─ models/
│  ├─ registry.py      manifest 注册表
│  ├─ manager.py       下载 / sha256 校验 / 7z 成员提取
│  ├─ fp16.py          fp16 变体转换（原子写）
│  ├─ ASSETS.md        models-v1 资产页说明（Release body 的事实源）
│  └─ registry_json/   内置模型 manifest
└─ utils/process.py    进程树终止（取消/清理）
scripts/               calibrate_color.py（IO 校准）、convert_fp16.py / export_onnx_x4plus.py、build_trt_component.py、bench_*.py（基准）
tests/                 测试文件（管线/引擎/服务层/并行/组件/下载器/图片超分/模型对比/PDF 合并/新模型/GPU 租约/DB 迁移/回归/MCP bridge）
```

## HTTP API 一览

sidecar 仅监听 localhost（本地 token 鉴权），完整定义见 `server/app.py`：

| 分组 | 端点 | 说明 |
|---|---|---|
| 系统 | GET `/api/health` `/api/engine` `/api/hardware` `/api/perf/history` `/api/presets` | 版本 / 引擎后端选择 / GPU·CPU 检测 / 性能采样 / 输出预设 |
| 设置 | GET · PUT `/api/settings` | fp16/tile/双路并行/默认输出目录等 |
| 探测 | POST `/api/probe` · GET `/api/log-tail` | 媒体信息（带探测缓存）/ 日志尾部 |
| 模型 | GET `/api/models` · POST `/api/models/{id}/download` · DELETE `/api/models/{id}` · POST `/api/models/import` | 清单与适配性 / 下载（sha256）/ 删除 / 自定义导入 |
| 任务 | POST `/api/tasks` · GET `/api/tasks` · GET `/api/stats` · POST `/api/tasks/reorder` · `{id}/cancel` · `{id}/resume` · DELETE `{id}` · GET `{id}/preview` | 视频（单个或 inputs 批量）与图片（inputs 清单合并一任务）创建；串行队列 / 断点续跑 / 结果预览 |
| 剪切 | POST `/api/trim` · GET `/api/trim/{job_id}` · POST `…/cancel` | smart / fast / exact 三模式后台作业 |
| 对比 | POST `/api/compare` · GET `/api/compare/{job_id}` · POST `…/cancel` · GET `…/asset/{key:path}` | 多模型对比作业（素材切割 × 各模型处理）/ 白名单资产读取 |
| TRT | GET `/api/trt-component` · POST `…/install` · DELETE | TensorRT 组件状态 / 安装 / 卸载 |
| 事件 | WS `/ws` | 见下「WS 事件契约」 |

## WS 事件契约（`/ws`）

连接带本地令牌（`ws://127.0.0.1:<port>/ws?token=…`，与 HTTP 同源鉴权）。每条事件是
一个 JSON dict，`type` 取值如下（发布点：`server/events.py` 总线；任务 worker 事件由
`runner.py` 转发并附加 `task_id`；后台线程一律走 `publish_threadsafe`）：

| type | 载荷（除 type 外的字段） | 说明 |
|---|---|---|
| `task_status` | `task_id`，`status`（queued/running/done/failed/canceled），失败附 `error` | 任务状态变更的唯一权威信号（创建/入队/开跑/终态/取消） |
| `started` | `task_id`，`total_frames`，`output` | worker 开跑（runner 转发） |
| `progress` | `task_id`，`frames`，`total`，`fps`，`eta_sec` | worker 进度（转发同时落库） |
| `log` | `task_id`，`line` | 任务日志行（一行一条） |
| `task_deleted` | `task_id` | 任务被删除 |
| `recovered` | `count` | sidecar 启动时恢复的孤儿任务数 |
| `queue_gate` | `active`，`reason` | 队列处理时机门控（立即/时段/空闲）的启停与原因 |
| `queue_done` | `action`，`grace_s` | 队列完成后动作（通知/关机/休眠）进入反悔倒计时 |
| `queue_done_fired` | `action`，`ok` | 倒计时结束动作实际执行 |
| `queue_done_canceled` | — | 反悔窗口内被用户取消 |
| `model_download` | `model_id` + `progress`（0~1）/ `source`（modelscope\|github）/ `done` / `failed` | 下载进度（先发一条 `progress:0` 让 UI 立刻出进度条）、实际命中的下载源、完成/失败 |
| `trim` | `job_id`，`state`（当前仅 canceled） | 剪切作业只有取消走 WS；进度与结果轮询 `GET /api/trim/{job_id}` |
| `compare` | `id`，`status`（running/canceled/failed/entry_done/entry_failed…），运行中附 `model_id` | 模型对比作业状态机（每个候选模型开始/完成/失败各一条） |
| `compare_log` | `id`，`line` | 对比作业日志行 |
| `trt_component` | `phase`（download/extract/done/error）；download 附 `file`/`done`/`total`/`source`，extract 附 `file`，error 附 `error` | TensorRT 组件安装进度 |
| `perf` | `t`，`cpu`，`mem_pct`，`mem_used_gb`，`gpus`，`task` | 性能采样推送（与 `GET /api/perf/history` 同构） |

worker 的 `done`/`failed`/`canceled` 终态事件不直接上 WS——runner 消化后统一发
`task_status`。前端消费方式见 `app/src/renderer/src/store.ts`。

## 模型 IO 约定（manifest `io` 字段；结论均为真机实测）

| 字段 | 取值 | 说明 |
|---|---|---|
| color | `rgb` / `bgr` | 训练通道序（AnimeVideo 系是 BGR 特例，其余均 RGB） |
| range | `0-1` / `0-255` | 数值范围；现役模型全部 0-1 归一化 |
| pad | 整数，或 `{"2":2,"3":4}` 按倍率分档 | 输入边长最小倍数；同族不同倍率需求可不同（CUGAN up2x 只需偶数、up3x 需 4 的倍数——pad 按 3 对齐曾致 45/51/63 高度崩溃） |
| affine | `[a, b]` | 0-1 域入图 `x*a+b`、出图 `(y-b)/a`；CUGAN Pro 动态范围压缩缺它输出直接爆炸，带仿射模型自动跳过 u8 包装 |
| graph_opt | `basic` / `disable` | DML 图优化降档（CUGAN 系 Add 算子全量优化崩溃） |
| batch_hint | 整数 | 批帧建议（动态 batch 模型单 run 多帧） |

通道序用红绿分屏法实测、范围用 0-255 喂入爆炸对照、pad 用 8..64 全尺寸扫描——完整口径与踩坑见根目录 `BENCH.md`（脚本历史参考 `scripts/calibrate_color.py`）。新模型入库流程：核实上游许可 → 逐项实测 IO → 上传 models-v1（平铺引用，`ASSETS.md` 同步维护资产页说明）→ 写 manifest（sha256 + size；`url` 指 ModelScope 主源、`mirror_urls` 列 models-v1 备用，发版时自动对账同步镜像）→ 真实权重端到端验证。

## 已知取舍（2026-08-26 审查拍板，勿顺手"修复"）

- **前端 `webSecurity: false`**：为 `<video>` 直读 file://（Chromium 原生加载器带 Range/moov
  尾部探测）。自定义协议方案有 moov 在尾 MP4 黑屏的历史坑，在 CSP 收敛 + 本地 token 鉴权下
  保持现状（`app/src/main/index.ts` 有同款注释）。
- **ONNX 路径量化用截断**（`astype(uint8)`）而非 round：与 u8 包装图内 Cast 一致是刻意的，
  两路径靠 ≤1/255 逐位 A/B 校验把关；torch 路径用 round，两引擎 ≤1/255 系统性差异不可感知。

## 测试与基准

```bash
$py -m pytest tests/ -q          # 管线/引擎等测试（管线/引擎/服务层/并行/组件/下载器/图片超分/模型对比/PDF 合并/新模型/回归/MCP bridge；从 backend 目录跑；无 GPU/部分模型缺失时按机器跳过）
cd ../app && pnpm test           # 前端 vitest（CI 同跑：ci.yml 后端 pytest + 前端类型检查/单测/构建）
$py scripts/bench.py             # 速度与内存基准表
```

MCP 默认模型：动漫 `animejanai-v31-hd-balanced-sharp`（x2）；黑白漫画 `mangajanai`（默认 x2，权重按源高度自适应）；彩漫 `illustrationjanai-4x-dat2`（x4）。`rf_task_create` 可省略 `model_id`，通过 `content_type=anime/manga_bw/manga_color` 选默认；省略类型时 `kind=manga` 按黑白漫画，其余按动漫。显式模型和倍率优先，真人素材继续通过 `rf_probe` 选择。`rf_probe` 的图片推荐依据黑白/彩色检测，不区分摄影与彩漫，照片请手动选通用模型。

模型清单新增 `category`（主用途）、`version`（具体权重版本）、`temporal`（是否使用跨帧信息）。主用途分为 `anime_video` 动漫视频、`anime_restore` 动漫重建/降噪、`manga_bw` 黑白漫画、`illustration` 彩漫/插画、`photo_restore` 照片/通用修复、`general_upscale` 轻量通用放大、`interp` 补帧。原有 `content` 内容标签与 `scenes` 任务场景保持独立；`rf_models` 返回相同字段。速度档只描述计算取向，不是画质排名；Sharp 为风格选择，逐帧超分不保证无闪烁。IllustrationJaNai 当前权重版本：2x 为 V3 SPAN S，4x 为 V1 ESRGAN，4x DAT2 为 V1 DAT2。

分类说明依据：[AnimeJaNai](https://github.com/the-database/mpv-AnimeJaNai)、[MangaJaNai](https://github.com/the-database/MangaJaNai)、[ArtCNN](https://github.com/Artoriuz/ArtCNN)、[Real-ESRGAN 模型库](https://github.com/xinntao/Real-ESRGAN/blob/master/docs/model_zoo.md)、[HAT](https://github.com/XPixelGroup/HAT)、[SwinIR](https://github.com/JingyunLiang/SwinIR)、[DIS](https://github.com/Kim2091/DIS)、[SeemoRe](https://github.com/eduardzamfir/seemoredetails)。这些分类描述用途，不代表统一素材实测后的质量排名。
