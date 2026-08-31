# super_video backend

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

# 探测媒体信息（含 M0 校验：8bit SDR CFR）
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

其余子命令 `worker` / `ort-check` / `selftest` 为打包链路与自检内部使用，日常开发不需直接调用。

## 桌面端（app/）

```bash
cd app && pnpm install && pnpm build && npx electron .   # 或 pnpm dev
```

## 代码结构

```
sv/
├─ paths.py            项目路径 / ffmpeg 定位 / 数据目录迁移
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
│  ├─ app.py           FastAPI 路由 + 本地 token 鉴权 + 任务/剪切/对比入口
│  ├─ runner.py        串行任务调度（取消杀树、退出语义、孤儿清杀）
│  ├─ worker.py        任务 worker 子进程（stdout JSON 事件协议；视频与图片两类）
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
tests/                 42 个测试文件（管线/引擎/服务层/并行/组件/下载器/图片超分/模型对比/PDF 合并/新模型/回归）
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
| 事件 | WS `/ws` | 进度 / 状态 / 日志 / 下载进度 / TRT 安装进度广播 |

## 模型 IO 约定（manifest `io` 字段；结论均为真机实测）

| 字段 | 取值 | 说明 |
|---|---|---|
| color | `rgb` / `bgr` | 训练通道序（AnimeVideo 系是 BGR 特例，其余均 RGB） |
| range | `0-1` / `0-255` | 数值范围；现役模型全部 0-1 归一化 |
| pad | 整数，或 `{"2":2,"3":4}` 按倍率分档 | 输入边长最小倍数；同族不同倍率需求可不同（CUGAN up2x 只需偶数、up3x 需 4 的倍数——pad 按 3 对齐曾致 45/51/63 高度崩溃） |
| affine | `[a, b]` | 0-1 域入图 `x*a+b`、出图 `(y-b)/a`；CUGAN Pro 动态范围压缩缺它输出直接爆炸，带仿射模型自动跳过 u8 包装 |
| graph_opt | `basic` / `disable` | DML 图优化降档（CUGAN 系 Add 算子全量优化崩溃） |
| batch_hint | 整数 | 批帧建议（动态 batch 模型单 run 多帧） |

通道序用红绿分屏法实测、范围用 0-255 喂入爆炸对照、pad 用 8..64 全尺寸扫描——完整口径与踩坑见根目录 `BENCH.md`（脚本历史参考 `scripts/calibrate_color.py`）。新模型入库流程：核实上游许可 → 逐项实测 IO → 上传 models-v1（平铺引用，`ASSETS.md` 同步维护资产页说明）→ 写 manifest（sha256 + size）→ 真实权重端到端验证。

## 已知取舍（2026-08-26 审查拍板，勿顺手"修复"）

- **前端 `webSecurity: false`**：为 `<video>` 直读 file://（Chromium 原生加载器带 Range/moov
  尾部探测）。自定义协议方案有 moov 在尾 MP4 黑屏的历史坑，在 CSP 收敛 + 本地 token 鉴权下
  保持现状（`app/src/main/index.ts` 有同款注释）。
- **ONNX 路径量化用截断**（`astype(uint8)`）而非 round：与 u8 包装图内 Cast 一致是刻意的，
  两路径靠 ≤1/255 逐位 A/B 校验把关；torch 路径用 round，两引擎 ≤1/255 系统性差异不可感知。

## 测试与基准

```bash
$py -m pytest tests/ -q          # 306 项（管线/引擎/服务层/并行/组件/下载器/图片超分/模型对比/PDF 合并/新模型/回归；从 backend 目录跑）
$py scripts/bench.py             # 速度与内存基准表
```
