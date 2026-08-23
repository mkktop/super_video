# super_video backend (M0)

视频超分核心管线：ffmpeg 流式管道 + ONNX Runtime 推理 + CLI。M0 范围见根目录 `PLAN.md`。

## 环境

```bash
cd D:\work\super_video
python -m venv .venv            # 已就绪则跳过
.venv\Scripts\python -m pip install -r backend\requirements.txt
```

ffmpeg/ffprobe 不入版本库，首次搭建时下载（BtbN 静态构建，含 ffprobe）：

```bash
curl -L -o ffmpeg.zip https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip
unzip ffmpeg.zip -d ffmpeg_tmp && cp ffmpeg_tmp/ffmpeg-master-latest-win64-gpl/bin/*.exe ../bin/ && rm -rf ffmpeg_tmp ffmpeg.zip
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
```

## 代码结构

```
sv/
├─ paths.py            项目路径 / ffmpeg 定位
├─ pipeline/
│  ├─ probe.py         ffprobe 封装 + M0 输入校验（10bit/HDR/VFR 拒绝）
│  ├─ stream.py        核心流式管线（解码→逐帧推理→编码，管道不落盘）
│  └─ tile.py          大图分块（重叠切分 + 无缝拼合）
├─ engines/
│  ├─ base.py          引擎接口 + 分块调度
│  └─ onnx_engine.py   ONNX 引擎（EP 回退、固定尺寸模型、0-1/0-255 范围）
├─ models/
│  ├─ registry.py      manifest 注册表
│  ├─ manager.py       下载 / sha256 校验
│  └─ registry_json/   内置模型 manifest
└─ utils/process.py    进程树终止（取消/清理）
scripts/               calibrate_color.py（IO 校准）、bench.py（基准）
tests/                 tile / stream / e2e 三层测试
```

## 模型 IO 约定（校准结论，2026-08-23 实测）

| 模型 | 尺寸输入 | 数值范围 | 通道序 | 分块 |
|---|---|---|---|---|
| realesr-animevideov3 | 动态 | 0-1 归一化 | BGR | 不需要 |
| realesrgan-x4plus | 固定 64x64 | 0-1 归一化 | BGR | tile=64 必须 |

范围与通道序由 `scripts/calibrate_color.py` 的 PSNR 对照实验确定；新模型入库前必须跑一遍校准。

## 测试与基准

```bash
$py -m pytest tests/ -q          # 15 项（tile/管道往返/真模型端到端）
$py scripts/bench.py             # 速度与内存基准表
```
