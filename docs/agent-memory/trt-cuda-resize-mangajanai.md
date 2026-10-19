---
name: trt-cuda-resize-mangajanai
description: TRT 真启用后 MangaJaNai 必崩（MxNet Resize 在 CUDA EP fast_divmod
  断言缺陷）——worker 崩溃自动降链 trt_cpu/cpu，已随 v0.4.7 发布；根修机会（ONNX 图改写）挂起；
  cuda 设置映射 ort_device=auto 的坑
metadata:
  node_type: memory
  type: project
  originSessionId: sess_f9e027fd-a96b-402a-bf3c-a90f940f6206
---

2026-09-03 v0.4.6 修好 TRT 探测后用户实测立刻爆雷：**MangaJaNai（漫画模型）任务必败** `RUNTIME_EXCEPTION ... fast_divmod.h:50 d_ >= 1 was false`。根因链：MangaJaNai 是 MxNet 导出（节点名 `/model/model.5/Resize` 风格），网络首层 Resize 到专训高度；该类 Resize 在 **CUDA EP 内核有断言缺陷**（DML/CPU 实现正常——所以探测坏的那些版本反而"没事"）；TRT 编译不了的节点回退 CUDA 执行，warmup 第一帧即崩。装版 DB 查失败任务确认 model_id=mangajanai。

**修复（commit da261f3，已随 v0.4.7 发布，2026-09-04）**：崩溃自动降链重试，不靠用户手动切后端。①`OnnxSrEngine` 新 device=`"trt_cpu"`——TRT 链剔除 CUDAExecutionProvider（主图仍 TRT 编译、坏节点落 CPU，Resize 廉价速度几乎无损；u8 包装会话的链构造同步支持）；②worker `_load_onnx_engine` 重试环新增分支（range 3→4）：非 OOM 且特征命中（**RUNTIME_EXCEPTION 且含 providers/cuda 源路径**，只认精确特征防误伤驱动/初始化问题）→ engine=trt 换 trt_cpu、engine=cuda 退 cpu 保出片，log 说明；降链后条件天然只触发一次。测试 +3（trt_cpu 链剔除 CUDA / trt 换链 calls==[trt,trt_cpu] / cuda 退 cpu）。存量 0.4.6 临时解法=设置切 DirectML（0.4.7 起自动降链无需手动）。

**根修机会挂起（2026-09-04 向用户交底过，未排期）**：给 MangaJaNai 的 ONNX 做图改写（把 Resize 换成 CUDA 友好的等价算子，需逐位校验兜底）可消掉降链——待漫画路径用户反馈量再决定投入；fp16 转换器缺陷同理只能等上游（见 [[fp16-conversion-load-validation]]）。修复策略分层原则：我们自己的层根修，第三方组件（ORT 内核/转换器/驱动）层优雅降级，**降级必须带可见日志**（「已切换为 TensorRT+CPU 混合执行」等），不静默变慢。

**Why**: GPU 后端"能不能建会话"和"跑起来内核崩不崩"是两回事——探测只覆盖前者；MxNet 导出的 Resize 是 CUDA EP 已知坑族。
**How to apply**: 关键映射事实——**settings engine=cuda 映射的 ort_device 是 "auto"**（worker 只显式映射 trt/cpu，auto 走 _PROVIDER_ORDER 自动含 CUDA），判定降链要用 `eng_setting` 而非 ort_device；存量版本临时解法=设置切 DirectML。另：修好"恒回退"类闸门后积压的下游坑会在用户手里当天爆出（本例即 [[engine-probe-hardcoded-model-bug]] 的后继），GPU 类修复发布前先在装版把常用模型各试跑一轮。
