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

### ArtCNN 系（2x，Artoriuz，MIT）
单通道亮度 doubler：应用自动做 RGB↔YCbCr 变换（BT.601 全域）并双三次放大
色度，用户无感。全卷积任意边长免对齐。上游 [Artoriuz/ArtCNN](
https://github.com/Artoriuz/ArtCNN) 仓库 ONNX/ 目录官方导出（v1.6.2）。

| 文件 | 定位 |
|---|---|
| ArtCNN_C4F16.onnx | 极速档（权重仅 50KB，老显卡可用） |
| ArtCNN_C4F16_DN.onnx | 极速档 · 降噪（压制源/噪点源） |
| ArtCNN_R8F64.onnx | 画质档（社区实测超紧凑版 CUGAN） |

### RealESRGAN x4plus Anime 6B（xinntao，BSD-3-Clause）
官方动漫图片 4x 专模（6 blocks 轻量版），插画/封面放大。本仓库自官方
release v0.2.2.4 权重导出动态尺寸 ONNX。

| 文件 | 说明 |
|---|---|
| RealESRGAN_x4plus_anime_6B.onnx | 动漫图片 4x（自官方 .pth 导出） |

## 漫画 / 图片超分

### MangaJaNai（黑白漫画专模，the-database，CC BY-NC 4.0）
**许可：CC BY-NC 4.0（署名-非商业）**，上游 [the-database/MangaJaNai](
https://github.com/the-database/MangaJaNai) release 1.0.0 权重（本仓库自 .pth
导出 ONNX，torch-vs-ort 逐位一致）。黑白漫画按**设计源高度**分 7 档专训
（网点频率对位，应用按源高度自动选档，用户无感），专治 JPEG/摩尔纹退化、
还原印刷网点。彩色页请用下面的 IllustrationJaNai。

| 文件 | 说明 |
|---|---|
| 2x_MangaJaNai_1200p … 2048p.onnx（7 个） | 2x，按源高度 1200p~2048p 各一档 |
| 4x_MangaJaNai_1200p … 2048p.onnx（7 个） | 4x，同上七档 |

### IllustrationJaNai（彩色漫画页 / 插画，the-database，CC BY-NC 4.0）
2x 极速档为上游 release 3.0.0 官方 ONNX（fp16 原生）；4x 两档自 release 1.0.0
.pth 导出（DAT2 为 256 定长分块导出）。

| 文件 | 定位 |
|---|---|
| 2x_IllustrationJaNai_V3_SPAN_S.onnx | 2x 极速（官方 fp16 导出） |
| 4x_IllustrationJaNai_V1_ESRGAN.onnx | 4x 均衡（动态尺寸） |
| 4x_IllustrationJaNai_V1_DAT2.onnx | 4x 画质（DAT2 transformer） |

### HAT-L Real GAN（XPixelGroup，Apache-2.0）
真人图片 4x 画质旗舰。窗口注意力网络动态导出会烤死窗口划分常量，故为
**256 定长分块导出**（应用 tile 引擎自动按 256 分块，任意分辨率可用）。
自官方 Google Drive 权重（[XPixelGroup/HAT](https://github.com/XPixelGroup/HAT)）
导出，torch-vs-ort 逐位一致。sharper 为同架构独立训练的锐利风格权重。

| 文件 | 定位 |
|---|---|
| Real_HAT_GAN_SRx4.onnx | 画质旗舰（标准风格） |
| Real_HAT_GAN_sharper.onnx | 画质旗舰 · 锐利 |

### SwinIR-L Real GAN（JingyunLiang，Apache-2.0）
老牌 transformer 画质档，训练集含 Manga109（真人照片与漫画皆可）。同为
256 定长分块导出，上游 [JingyunLiang/SwinIR](https://github.com/JingyunLiang/SwinIR)
release v0.0 权重。

| 文件 | 定位 |
|---|---|
| SwinIR-L_realSR_x4_GAN.onnx | 真人/漫画 4x 画质档 |

### realesr-general-x4v3（+wdn 降噪，xinntao，BSD-3-Clause）
官方 tiny 快档（SRVGG，权重仅 5MB），真人 4x 的速度档。自官方 release
v0.2.5.0 权重导出动态尺寸 ONNX；wdn 为官方降噪权重（与 base 做 dni 插值
用，应用内作独立降噪档提供）。

| 文件 | 定位 |
|---|---|
| realesr-general-x4v3.onnx | 真人 4x 快档 |
| realesr-general-wdn-x4v3.onnx | 真人 4x 快档 · 降噪 |

### SeemoRe B（eduardzamfir，Apache-2.0）
ICML 2024 效率档（MoE，权重仅 2.4MB），一卡覆盖 2x/3x/4x——补通用 3x 空档。
MoE 路由的动态导出有尺寸相关语义偏差，为 **256 定长分块导出**。上游
[eduardzamfir/seemoredetails](https://github.com/eduardzamfir/seemoredetails)
Google Drive 权重。

| 文件 | 定位 |
|---|---|
| SeemoRe_B_X2.onnx / X3 / X4 | 2x / 3x / 4x（3x 为独有空档） |

### 社区精选（OpenModelDB 口碑款，CC BY-NC-SA 4.0）
**许可：CC BY-NC-SA 4.0（署名-非商业-相同方式共享）**，按非商业口径分发。
UltraSharp / AnimeSharp 为 Kim2091 官方 HF 仓库 ONNX 导出（0-1 RGB、动态
尺寸）；Remacri 自 4x_foolhardy_Remacri.pth 导出（4 的倍数对齐由应用自动补边）。

| 文件 | 定位 |
|---|---|
| 4x-UltraSharp.onnx | 社区照片口碑款（锐利干净） |
| 4x-AnimeSharp.onnx | 动漫图片锐利化 |
| 4x-Remacri.onnx | 照片温和增强 |

## 真人 / 通用超分

### Real-ESRGAN x4plus（xinntao / TencentARC，BSD-3-Clause）

| 文件 | 说明 |
|---|---|
| RealESRGAN_x4plus_dyn.onnx | 动态尺寸 ONNX（本仓库自官方权重导出，任意分辨率直跑） |
| RealESRGAN_x4plus.pth | 官方 PyTorch 权重（torch 引擎直跑，需独立 CUDA 环境） |

### DIS 系（2x，Kim2091，Apache-2.0）
超轻量放大+修复架构，原生 fp16 导出。补真人/通用内容的 2x 空档
（720p→1440p、840p→4K 无需硬上 4x）。上游 [Kim2091/DIS](
https://github.com/Kim2091/DIS) 官方 release。

| 文件 | 定位 |
|---|---|
| 2x-DIS_Balanced_Pretrain_fp16.onnx | 均衡档 |
| 2x-DIS_Fast_Pretrain_fp16.onnx | 极速档 |

## 补帧

### RIFE（hzwer，MIT）

| 文件 | 说明 |
|---|---|
| rife_v4.26.onnx | 光流补帧 2x，超分后串联，帧率翻倍 |
