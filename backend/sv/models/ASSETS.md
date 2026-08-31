# 模型资产（models-v1）

Super Video 应用内「模型市场」的全部下载源。应用按需从本页下载权重并逐文件
sha256 校验，正常使用**无需手动下载**；如需手动获取，保持文件名不变放入应用
数据目录 `models_store/<模型id>/` 文件夹即可跳过下载。

资产与应用版本解耦：同一资产被多个应用版本复用，不在应用版本 Release 里重复
分发。每个文件的 size + sha256 在应用注册表（`backend/sv/models/registry_json/`）
中登记，可用于人工核验。

## 动漫超分

### AnimeJaNai 系（2x，the-database）
上游发布包（mpv-AnimeJaNai）自由分发。

| 文件 | 定位 |
|---|---|
| 2x_AnimeJaNai_V2_SuperUltraCompact_100k.onnx | V2 极速档 |
| 2x_AnimeJaNai_V2_UltraCompact_30k.onnx | V2 均衡档 |
| 2x_AnimeJaNai_V2_Compact_36k.onnx | V2 画质档 |
| 2x_AnimeJaNai_HD_V3_SuperUltraCompact.onnx | V3 HD 极速档 |
| 2x_AnimeJaNai_HD_V3_UltraCompact.onnx | V3 HD 均衡档 |
| 2x_AnimeJaNai_HD_V3_Compact.onnx | V3 HD 画质档 |

#### V3.1（2026 新一代，SPAN 架构，fp16 原生）
Performance ≈ V3 极速档速度、画质反超上一代极速/均衡；Balanced ≈ V3 均衡档
速度、画质反超上一代画质档。Sharp1 为同架构独立训练的锐利风格权重（非后处理）。

| 文件 | 定位 |
|---|---|
| 2x_AnimeJaNai_HD_V3.1_Performance_SPANF3_b5f48_unshuffle_fp16.onnx | 极速档 |
| 2x_AnimeJaNai_HD_V3.1Sharp1_Performance_SPANF3_b5f48_unshuffle_fp16.onnx | 极速档·锐利 |
| 2x_AnimeJaNai_HD_V3.1_Balanced_SPANF3_b8f64_unshuffle_fp16.onnx | 均衡档 |
| 2x_AnimeJaNai_HD_V3.1Sharp1_Balanced_SPANF3_b8f64_unshuffle_fp16.onnx | 均衡档·锐利 |

### Ani4K v2（2x，Sirosky）
**许可：CC BY-NC 4.0（署名-非商业）**。原作者 [Sirosky / Upscale-Hub](
https://github.com/Sirosky/Upscale-Hub)，此处为原文件未修改再分发，仅限非商业
用途；商用需自行联系原作者获取授权。

| 文件 | 定位 |
|---|---|
| 2x_Ani4Kv2_G6i2_Compact_107500_fp32.onnx | 画质档 |
| 2x_Ani4Kv2_G6i2_UltraCompact_105K_fp32.onnx | 极速档 |

### Real-CUGAN 系（bilibili，Apache-2.0）
动漫超分 + 降噪。`latest` 为标准版，`pro` 为官方 Pro 版（画质更好、速度更慢，
应用内自动处理其动态范围前后处理）。

| 文件 | 说明 |
|---|---|
| up2x-latest-conservative / no-denoise / denoise1x / denoise2x / denoise3x.onnx | 标准版 2x 全降噪档 |
| up3x-latest-conservative / no-denoise / denoise3x.onnx | 标准版 3x |
| up4x-latest-conservative / no-denoise / denoise3x.onnx | 标准版 4x |
| pro-conservative / pro-no-denoise3x / pro-denoise3x-up2x.onnx | Pro 版 2x |
| pro-conservative / pro-no-denoise3x / pro-denoise3x-up3x.onnx | Pro 版 3x |

### AnimeVideo（RealESRGAN-animevideo，TencentARC，BSD-3-Clause）
为视频内容训练的动漫专用模型，成片稳定不易闪烁。

| 文件 | 定位 |
|---|---|
| RealESR-AnimeVideo-v3_x4.onnx | 4x（480p/720p 老动画升清） |

（2x 档 RealESRGANv2-animevideo-xsx2 随应用安装包分发，不在此页。）

## 真人 / 通用超分

### Real-ESRGAN x4plus（xinntao / TencentARC，BSD-3-Clause）

| 文件 | 说明 |
|---|---|
| RealESRGAN_x4plus_dyn.onnx | 动态尺寸 ONNX（本仓库自官方权重导出，任意分辨率直跑） |
| RealESRGAN_x4plus.pth | 官方 PyTorch 权重（torch 引擎直跑，需独立 CUDA 环境） |

## 补帧

### RIFE（hzwer，MIT）

| 文件 | 说明 |
|---|---|
| rife_v4.26.onnx | 光流补帧 2x，超分后串联，帧率翻倍 |
