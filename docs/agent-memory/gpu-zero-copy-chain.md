---
name: gpu-zero-copy-chain
description: GPU 直连零拷贝链（NVDEC→显存推理→NVENC）已合 main；组件化被用户拍板暂缓
metadata:
  node_type: memory
  type: project
  originSessionId: sess_fec8e0d4-a267-43f6-abba-70e3b229b0d9
---

GPU 直连零拷贝链于 2026-10-18 合入 main（2a2f904 + 审查修复 b1418cd）：10bit+NVENC+无特殊参数的任务帧数据全程驻显存，实测 45.8→64.9fps（+42%，5070 Ti 机器跑的 A/B）。标准链零改动（segmented/settings 的大 diff 是排版重排，-w 后 4+3 行）。

**用户已拍板（2026-10-18）：PyNvVideoCodec/cuda-bindings 组件化"先算了"**——不进基础安装包也不并入 TRT 组件，别主动再提；重提前提是用户主动问起直连链发货。两个包合计 wheel ~33MB，届时归属建议是扩进 [[trt-component-installer]]。

未写进代码的遗留项（审查发现，用户知情未修）：GPU 路径 `used_precision` 虚标（回显请求值非实测）；`av1_nvenc` 在 stream_gpu 放行但 gpu_codec 不认；`gpu_zero_copy` 无 UI 开关。

本机状态：`.venv-cuda`（Python 3.14.4）**没装** PyNvVideoCodec/cuda-bindings，本机直连链不激活（静默回标准链）；要本地复现需 `pip install PyNvVideoCodec==2.2.3 cuda-bindings`。

产品行为注意：直连链强制 bf=0（无 B 帧），同画质码率明显升高（3 分钟 342MB vs 标准链 150MB@cq18）——用户问"直连链产物为什么大"时先想到这个。
