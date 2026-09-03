# 更新说明

## v0.4.7

### 修复
- 修复超大图片（如 2133p 漫画扫描页 4 倍放大）在 DirectML 下报「引擎加载失败 UnicodeDecodeError」的问题——真实原因被错误消息的编码问题掩盖了：实为显存不足。现在大图会自动启用分块处理（输出分辨率较小的任务不受影响、性能不变），一次到位不再撞显存
- 中文 Windows 上 DirectML 显存不足的错误提示会被错误编码掩盖（显示成 UnicodeDecodeError），自动降级重试因此失效——现在能正确识别这类错误并自动调小分块重试
- 修复 MangaJaNai（漫画模型）在 TensorRT / CUDA 后端任务必败的问题（「RUNTIME_EXCEPTION Resize」）——个别算子在 CUDA 执行层有缺陷，现在自动切换为 TensorRT+CPU 混合执行重试，主图仍走 TensorRT，速度基本不受影响
