---
name: trt-fp16-nan-black-images
description: TRT 混合精度对 transformer 超分模型输出全 NaN→纯黑图；已修（125379a：manifest io.trt_fp16:false + 灰图探测降链 + 缓存分家）；v0.5.0 不含、待下一版
metadata:
  node_type: memory
  type: project
  originSessionId: sess_a3398c1f-1992-48ff-95b5-e12ddab90214
---

2026-09-12 用户报 IllustrationJaNai 4x DAT2 图片超分 53 张全纯黑。根因：`_trt_provider_options` 恒开 `trt_fp16_enable`，DAT2（transformer）在 TRT fp16 下输出 100% NaN（3145728/3145728），`np.clip(NaN).astype(uint8)` 截成 0——会话能建、run 不抛、任务「成功」但成片全废。CPU/DML fp32 与 TRT fp32（0.55s/帧，仍远快于 CPU 11.6s）均健康；模型哈希无误。修复 `125379a` 已落 main（2026-09-12，v0.5.0 tag 之后，**下一版才带上**）：

- manifest `io.trt_fp16:false`（DAT2 已声明）；`OnnxSrEngine(trt_fp16=)` 显式参数供降链重建覆盖（仅关 fp16 时才传该 kwarg——测试 fake 显式签名不感知新参）
- TRT 引擎缓存按精度分目录 `trt_cache`/`trt_cache_fp32`：ORT 缓存键不保证区分 trt_fp16_enable，坏 fp16 缓存可能被 fp32 链错误复用；fp16 沿用旧路径存量零失效
- `_load_onnx_engine` 预热从全零帧改灰 128 帧（超分对平坦输入输出≈同色），输出均值 <8 判数值损坏，降链 TRT 关 fp16 → DML/CUDA → CPU；视频/图片/对比共用此链全覆盖

**Why:** 未来 transformer 系模型（SwinIR/HAT/ESPEN 候选）TRT fp16 大概率同病；灰图探测是兜底，新增此类模型仍应主动声明 io.trt_fp16。与 [[fp16-conversion-load-validation]]（fp16 文件转换器产坏图）是两回事。

**How to apply:** 「输出纯黑但任务成功」先查 sr_logs 的 provider（TRT 即此病）；本机复现 TRT 用仓库根 `.venv-cuda`（原生 TRT+CUDA EP）+ `PATH=/d/soft/super_video_data/trt-runtime/dlls:$PATH`（组件 DLL：cp314/ort 1.24.4/TRT 10.16.1.11，nvidia-smi 本机是 RTX 5080）；D:\soft 就是本机装版（用户实测=同一块 GPU）。用户日常 engine=trt+precision=fp16，见 [[d-soft-install-healthy]]。
