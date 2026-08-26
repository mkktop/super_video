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

# 启动 sidecar 服务（M1，Electron 会自动拉起；手动调试用）
$py cli.py serve --port 8730
```

## 桌面端（app/，M1）

```bash
cd app && pnpm install && pnpm build && npx electron .   # 或 pnpm dev
```

## 代码结构

```
sv/
├─ paths.py            项目路径 / ffmpeg 定位 / 数据目录迁移
├─ pipeline/
│  ├─ probe.py         ffprobe 封装 + 探测缓存（接受 10bit/VFR→CFR 化，拒绝 HDR）
│  ├─ stream.py        核心流式管线（解码→逐帧/批量推理→编码，管道不落盘）
│  ├─ segmented.py     分段流式管线（checkpoint 断点续跑 + 双路分片）
│  ├─ chunked.py       torch 引擎的分块 checkpoint 管线
│  ├─ tile.py          大图分块（重叠切分 + 无缝拼合）
│  └─ trim.py          视频剪切（smart/fast/exact 三模式，可取消）
├─ engines/
│  ├─ base.py          引擎接口 + 分块调度
│  ├─ onnx_engine.py   ONNX 引擎（EP 回退链、u8 图手术、批量）
│  ├─ torch_engine.py  PyTorch 引擎（CUDA 环境，fp16 autocast）
│  ├─ trt_runtime.py   TensorRT 可选组件的发现/激活
│  ├─ u8_wrap.py       uint8 直进直出图手术（前后处理 GPU 化）
│  └─ rife.py          RIFE 补帧（RGB uint8 契约）
├─ server/
│  ├─ app.py           FastAPI 路由 + 本地 token 鉴权 + trim 队列
│  ├─ runner.py        串行任务调度（取消杀树、退出语义、孤儿清杀）
│  ├─ worker.py        任务 worker 子进程（stdout JSON 事件协议）
│  ├─ db.py            SQLite 任务表（WAL）
│  ├─ engine_select.py 解释器/后端选择（CUDA/TRT/DML 探测）
│  ├─ trt_component.py TRT 组件安装/卸载/状态
│  ├─ perf.py          性能采样（CPU/GPU/任务资源）
│  └─ events.py        WS 广播总线（线程安全发布）
├─ models/
│  ├─ registry.py      manifest 注册表
│  ├─ manager.py       下载 / sha256 校验 / 7z 成员提取
│  ├─ fp16.py          fp16 变体转换（原子写）
│  └─ registry_json/   内置模型 manifest
└─ utils/process.py    进程树终止（取消/清理）
scripts/               calibrate_color.py（IO 校准）、build_trt_component.py、bench_*.py（基准）
tests/                 26+ 个测试文件（管线/引擎/服务层/并行/组件/回归）
```

## 模型 IO 约定（校准结论，2026-08-23 实测）

| 模型 | 尺寸输入 | 数值范围 | 通道序 | 分块 |
|---|---|---|---|---|
| realesr-animevideov3 | 动态 | 0-1 归一化 | BGR | 不需要 |
| realesrgan-x4plus | 固定 64x64 | 0-1 归一化 | BGR | tile=64 必须 |

范围与通道序由 `scripts/calibrate_color.py` 的 PSNR 对照实验确定；新模型入库前必须跑一遍校准。

## 已知取舍（2026-08-26 审查拍板，勿顺手"修复"）

- **前端 `webSecurity: false`**：为 `<video>` 直读 file://（Chromium 原生加载器带 Range/moov
  尾部探测）。自定义协议方案有 moov 在尾 MP4 黑屏的历史坑，在 CSP 收敛 + 本地 token 鉴权下
  保持现状（`app/src/main/index.ts` 有同款注释）。
- **ONNX 路径量化用截断**（`astype(uint8)`）而非 round：与 u8 包装图内 Cast 一致是刻意的，
  两路径靠 ≤1/255 逐位 A/B 校验把关；torch 路径用 round，两引擎 ≤1/255 系统性差异不可感知。

## 测试与基准

```bash
$py -m pytest tests/ -q          # 156 项（管线/引擎/服务层/并行/组件/下载器/回归；从 backend 目录跑）
$py scripts/bench.py             # 速度与内存基准表
```
