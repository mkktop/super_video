# M0 基准报告（2026-08-23，RTX 5080 16GB / DirectML EP）

## 速度

| 模型 | 输入(24fps) | 输出 | fps | 实时比 | 峰值内存 |
|---|---|---|---|---|---|
| realesr-animevideov3 | 854x480 | 3416x1920 | 9.82 | 0.41x | 3.0GB |
| realesr-animevideov3 | 1280x720 | 5120x2880 | 4.56 | 0.19x | 6.6GB |
| realesrgan-x4plus (tile=64) | 854x480 | 3416x1920 | 0.59 | 0.02x | 2.7GB |

速度与像素数线性（480p→720p 像素×2.25，速度×2.15），符合预期。

## 内存

720p 20s 完整 RSS 曲线：280MB →(前36s爬升)→ 6.65GB →(完全走平,波动<30MB)→ 进程退出回落。
结论：**非泄漏**，为 DML 内存池预热增长；M0 验收（内存不随时长增长）通过。
注意：1080p 源 x4 输出达 7680x4320（单帧 95MB），M1 需要高分辨率自动 tile。

## 结论与后续行动

1. **animevideov3 @ 480p≈10fps 可用**：10 分钟 480p 视频约 40 分钟出片。720p 源建议先降到 480p 语义档或接受 0.19x 实时。
2. **x4plus 固定 64x64 导出版是速度瓶颈**：每帧 180+ 次独立 session.run，Python/调度开销占大头。行动：M1 期间找/自制**动态尺寸**导出版（tile 256 可减少 16 倍调用次数），manifest 换 URL 即可——这正是模型=数据设计的价值。
3. **DirectML 性能天花板明显**：RTX 5080 跑这个速度远低于硬件能力。行动：把 onnxruntime-gpu(CUDA EP) 从 M3 **提前到 M1 可选项**，预期 animevideov3 有数倍提升。
4. 模型 IO 约定已实测校准（见 backend/README.md）：两模型均 0-1 归一化 + BGR；animevideov3 动态尺寸，x4plus 固定 64x64。

## 测试

15/15 通过：tile 无损往返（4 尺寸 × 2 倍率）、管道 IO 往返（音轨保留/分辨率/时长）、两模型真机端到端（320x180→1280x720）。

---

# 真片源质量评估（2026-08-23，samples/ 两段 1080p 动漫）

## 方案

原片(1080p)为 ground truth → lanczos 缩小到 480x270（模拟低清源）→ x4 超分回 1080p → 与原图算 PSNR/SSIM，lanczos 直接放大为基线。

## 客观指标

| 视频 | 方法 | PSNR(dB)↑ | SSIM↑ | 耗时(≈370-590帧) |
|---|---|---|---|---|
| 动漫测试1 | lanczos x4 | 21.54 | 0.916 | 秒级 |
| 动漫测试1 | **animevideov3** | **23.54 (+2.0)** | **0.939** | 19s @31fps |
| 动漫测试2 | lanczos x4 | 24.37 | 0.931 | 秒级 |
| 动漫测试2 | **animevideov3** | **26.41 (+2.0)** | **0.947** | 13s @28fps |
| 动漫测试2 | x4plus | 26.24 | 0.935 | 225s @1.6fps |

## 视觉结论（2x2 对比帧人工核验）

GT > animevideov3 > x4plus > lanczos。animevideov3 线条锐利接近原片、色彩准确、无伪影；x4plus 有轻微边缘过冲和偏暖色偏；lanczos 模糊。

**产品结论：动漫默认模型 animevideov3 无争议（质量高且快 20 倍）；x4plus 留给真人内容，等动态尺寸导出版提速。**

## 1080p→8K 压力测试

动漫测试1 3s 片段，animevideov3 x4 → 7680x4320：**2.07fps 稳定完成，无显存问题**。该模型（动态尺寸、无需 tile）下 16GB 显存可直出 8K；tile 自动分块作为 M1 的保险机制实现。

## 已知小瑕疵（M1 清单）

ffprobe `nb_read_packets` 对部分文件少计 1~2 帧，进度条出现 91/90（cosmetic）；改为与 duration×fps 取 max。

---

# 推理后端对比（2026-08-23，RTX 5080 / 驱动 596.21）

| 场景 | DirectML | CUDA EP (ort-gpu 1.24.4) | 结论 |
|---|---|---|---|
| animevideov3 540p x2 | **18.6 fps** | 16.6 fps | DML +12% |
| animevideov3 1080p x2（出4K） | **5.3 fps** | 4.0 fps | DML +32% |
| x4plus 480p x4（64-tile） | **0.56 fps** | 0.15 fps | DML +270% |

**结论：当前模型库（SRVGG 小模型 + 小 tile 高频 session.run）是延迟/调度瓶颈而非算力瓶颈，DirectML 在 Blackwell 上全面占优。默认后端 = DirectML；CUDA 基础设施（.venv-cuda + 真会话探测 + SV_ENGINE=cuda 强制开关 + /api/engine）保留，供 M3 扩散模型（PyTorch+CUDA）阶段启用。**

后续真正的提速方向（按预期收益）：
1. **帧批处理**：模型导出带动态 batch 轴，一次 session.run 喂 4~8 帧，预计 2~4 倍；
2. 大分辨率输出时 NVENC 硬编（弱 CPU 场景已生效）；
3. M3 扩散模型走 PyTorch CUDA。
