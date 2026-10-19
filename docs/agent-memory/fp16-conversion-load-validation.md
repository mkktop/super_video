---
name: fp16-conversion-load-validation
description: fp16 转换器对注意力架构产出坏图（CUGAN/DAT2 两例）——v0.4.6 起 convert_file 落盘前 CPU
  会话验证，坏图自动回退 fp32；DAT2 manifest fp16:false
metadata:
  node_type: memory
  type: project
  originSessionId: sess_f9e027fd-a96b-402a-bf3c-a90f940f6206
---

2026-09-03 用户装版实锤：IllustrationJaNai 4x DAT2 任务必败「引擎加载失败 Type Error: Cast 节点类型不匹配」（`/layers.../attn/.../Cast_18`）。根因：onnxconverter fp16 转换器对 DAT2 注意力结构（attn 内 Cast 链）产出**类型不一致的图**——序列化不报错、加载才炸；convert_file 无验证直接原子落盘，model_file 只要 `_fp16` 文件存在就选中 → 该模型所有任务加载必败且**不自愈**（重下也会再转出同样的坏图）。本机 ModelScope 拉 fp32 原件完整复现（fp32 原件正常、转换产物 CPU 会话加载即报同款错）。

**修复（commit 291fb80，随 v0.4.6 发布）双层**：①通用防线——convert_file 落盘前用 CPU 会话建图验证（建图即可暴露类型错误，无需推理），坏图删产物抛异常，ensure_fp16_file 既有 except 兜底回退 fp32——以后任何模型的坏转换都不可能再入库；②定向——manifest `illustrationjanai-4x-dat2` fp16:false（第二例 fp16 转换必坏的注意力架构，第一例 CUGAN 见 [[model-broadening-and-trt]]）。存量机器的坏 `_fp16.onnx` 因 spec.fp16=false 被 model_file/worker 双闸绕开，**升级即自愈无需手工清理**（残留 31MB 磁盘无害）。

**Why**: 转换器产坏图是静默的（序列化成功），验证唯一可靠的时机是"真加载一次"。
**How to apply**: 新模型接入后跑一次 fp16 任务即知转换是否健康；坏转换的处置模式=manifest fp16:false + 通用验证兜底，不要试图逐算子 block-list 救特定架构（DAT2 的 Cast 在注意力深处，block 不完）。存量版本临时解法=设置切 FP32。
