---
name: model-broadening-and-trt
description: 2026-08-24 模型扩容(AnimeJaNai/CUGAN 全档)与 TRT 后端的落地事实与坑
metadata:
  node_type: memory
  type: project
  originSessionId: sess_24cfd10c-7f0e-45bd-974f-904d0b99e894
---

2026-08-24 对标 VSET 补差距（未提交时写的状态）：

- **models-v1 资产已上传**：11 个新 onnx（CUGAN denoise0/1/2 ×5 + AnimeJaNai V2/V3 HD ×6），
  registry_json 的 URL/sha256 与之一致，端到端下载+校验+推理实测过。后续补模型照此流程：
  下载→实测通道序→pin sha→上传 release→写 registry。上传走 `git credential fill` 取 token
  （gh CLI 是假包，见 [[super-video-update-flow]]）。
- **AnimeJaNai V2 是 fp16-IO 模型**（输入 tensor(float16)），V3 HD 是 fp32；都为 RGB/0-1、动态
  尺寸。引擎 OnnxSrEngine 已支持原生 fp16 输入（`_in_fp16`）。
- **CUGAN 全系 fp16 转换会坏**（Add_37 ShapeInferenceError），spec.fp16=false + worker 惰性
  转换前必须检查 spec.fp16（2026-08-24 修过一次真机踩坑）。
- **通道序经验测定法**：红绿分屏图喂 color=rgb，输出左红右绿则 RGB；纯色块测试对称性
  抵消不可用。
- **TensorRT 是可选组件**：settings engine=trt → .venv-cuda + tensorrt wheel；引擎层
  TRT 失败自动去 TRT 回退（单测 fake ORT 验证），真机 TRT 未装（1.5GB wheel 没装），
  运行时行为靠回退链保证。TRT 引擎缓存在 .tmp/trt_cache。
- **TRT 下一步状态（2026-08-25，用户主动问过 wheel 是什么，尚未拍板装）**：项目侧
  已全铺好（.venv-cuda 的 onnxruntime-gpu 自带 TensorrtExecutionProvider + 引擎缓存 +
  自动回退），只差 `pip install tensorrt`。增益机理：小网络逐算子调度开销占比大，TRT
  kernel 融合收益最大——在 u8 包装后 37ms/帧 基础上预期再 -30~50%（e2e 冲 30fps+）。
  注意：Blackwell(5080) 需较新 TRT 版本；动态尺寸按"见过的形状"编译，新分辨率首次
  重编译几分钟（有缓存，固定 1080p→4K 无感）；每个模型/分辨率首次编译分钟级。
- **模型来源**：vs-mlrt releases 的 models.vX.7z（852MB，含 CUGAN/RealESRGANv2 全家）；
  AnimeJaNai 在 the-database/mpv-upscale-2x_animejanai releases 的 ModelsOnly.zip。
  AniScale/AniSD/Waifu2x 系未收录（VSET 有，io 约定不同或源不集中）。

**Why:** 这些事实（资产已传、fp16 坑、通道序测法）不写下来，下个会话会重复下载 852MB
或重新踩 CUGAN fp16 崩溃。

**How to apply:** 加新模型先看本条流程；CUGAN/DAT2 类模型永远 fp16:false；改 worker fp16
  逻辑时保留 spec.fp16 守卫。新架构模型若 fp16:false 却仍想确认可转换，跑一次 convert_file
  即知（v0.4.6 起 convert_file 自带落盘前加载验证，坏图不再入库——DAT2 即第二例
  fp16 转换必坏的注意力架构，详见 fp16-conversion-load-validation）。TRT 真跑后
  MangaJaNai 的 CUDA Resize 崩溃见 [[trt-cuda-resize-mangajanai]]。
