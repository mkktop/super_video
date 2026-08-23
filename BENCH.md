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
1. **FP16 模型转换**（新证据支持，2026-08-23 追加实验后）：1080p x2 下编码(NVENC≈x264)与批处理(batch 4/8)均不改变速度（5.2~5.3fps 全部持平），190ms/帧几乎全在 DML fp32 推理（显存带宽受限）。转 fp16 减半带宽，DML 对 fp16 优化好，预期最可观的下一步；
2. ~~帧批处理~~（已实测：540p +6%，1080p -9%，仅小分辨率保留 batch_hint=4 + worker 分辨率启发式）；
3. M3 扩散模型走 PyTorch CUDA。

### 帧批处理实测（2026-08-23）

| 场景 | b=1 | b=4 | b=8 |
|---|---|---|---|
| 540p x2 | 19.7 | **20.8** | 20.3 |
| 1080p x2 | **5.26** | 5.20 | 4.77 |

批量与逐帧输出一致性：max diff ≤ 2/255（测试覆盖）。

---

# FP16 模型转换（2026-08-23，RTX 5080 / DML）

fp32 ONNX 转 fp16（IO 保持 fp32，Clip 算子保 fp32——其 DML fp16 实现会崩 0x8007023E）。
合成自然图（渐变+噪声）逐分辨率计时，3 预热 + 10 次取中位：

| 模型 | 输入 | fp32 | fp16 | 加速 | 输出 PSNR |
|---|---|---|---|---|---|
| animevideo-xsx2 | 1920x1080 | 97.6ms | 71.8ms | 1.36x | 75.1dB |
| animevideo-xsx2 | 1280x720 | 42.1ms | 30.9ms | 1.36x | 75.3dB |
| animevideo-xsx2 | 640x360 | 9.6ms | 6.4ms | 1.50x | 75.0dB |
| animevideov3-x4 | 960x540 | 34.4ms | 21.8ms | 1.58x | 74.5dB |
| animevideov3-x4 | 640x360 | 14.3ms | 8.3ms | 1.72x | 74.4dB |
| x4plus（64-tile） | 100x100 整帧 | 39ms | 19ms | **2.05x** | 58.7dB |

PSNR 74~75dB = 数值级一致（远高于 CRF18 编码损失）；x4plus 58.7dB 同样无感知差异。
权重体积精确减半。

**结论：验证了"瓶颈=fp32 显存带宽"的判断，fp16 落地为默认精度**：
- bundled 两模型随仓库分发 fp16 变体；下载模型在下载完成后服务端自动转换；
- worker 惰性兜底（store 旧副本/手动放入的 fp32 首次使用时补转，一次成本）；
- 设置页可切 fp32（个别模型数值异常时回退用）；CUDA 环境缺转换依赖时自动回退。
预期用户体感：1080p x2 约 5.3 → 7+ fps，x4plus 类大模型近翻倍。

端到端冒烟（动漫测试2.mp4 1080p hevc → x2 4K x264，368 帧）：**5.71 fps 完成**（fp32 历史值 5.3，+8%）。
推理 1.36x 只占端到端预算的一部分（hevc 解码 + 4K 编码不变），小分辨率与 x4/x4plus 场景提速占比更大。

---

# M3：torch 引擎 + 分块续跑（2026-08-23）

**扩散模型调研结论（重要，决定 M3 走向）**：FlashVSR 与 SeedVR2-3B 在 Windows 当前不可行——
- FlashVSR：无 ONNX；权重 6.4GiB（safetensors）；依赖需源码编译的 CUDA block-sparse-attention + Wan2.1 VAE
- SeedVR2-3B：12.6GiB fp32 权重 + apex 仅提供 Linux wheel（Windows 需自行编译）
- 结论：扩散引擎以「torch 适配器 + 分块 checkpoint 管线」形式落地，待官方支持 Windows 后接入模型本体

**torch 引擎实测**（Real-ESRGAN x4plus 官方 pth，RTX 5080，torch 2.11 cu128，fp16 autocast，tile 256）：
- 最小 RRDBNet 自实现（无 basicsr 依赖），官方权重 strict 加载通过
- 180x120 → x4：36ms/帧；与 ONNX 版（同权重）输出 PSNR 30.1dB（tiled 边界/精度差异，一致性合格）
- e2e 91 帧 1080p→8K 分块管线：373s 完成，checkpoint 分块续跑逻辑单测验证（预置 done 块 → 从其后继续）

**Real-CUGAN DML 兼容性**：原生 opset13 导出在 DML 默认扩展优化下 Add 算子崩溃（MLOperatorAuthorImpl），
`graph_optimization_level=ORT_ENABLE_BASIC` 即稳定（53ms/帧 @540p→1080p），性能与 DISABLE_ALL 持平。
其 fp16 转换版 DML 加载失败 → manifest 标 fp16=false。

**RIFE v4.26**（vs-mlrt impl=2 七通道版）：t=0 重构 I0 45.7dB；e2e 补帧输出帧率精确 ×2、时长/音轨保持。
