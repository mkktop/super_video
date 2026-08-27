# 更新说明

## v0.2.7
性能修复版本:TRT 全面提速 + CUGAN 稳定性围栏

### 性能:TRT 加速现在对全模型生效
此前多数模型在 TensorRT 后端下未吃到加速——u8 图手术包装图的通道翻转发生在 uint8 域,
被 ORT 的 TRT 导入器拒绝(不支持 UINT8 中间张量),包装自动弃用后 CPU 前后处理重新成为
瓶颈,部分场景反而比 DirectML 慢数倍。本次修复(通道翻转挪到浮点域 + 校验容差按后端
分档)后,TRT 下包装全部生效,实测(RTX 5080,960x540 输入):

| 模型 | 修复前 TRT | 修复后 TRT |
|---|---|---|
| AnimeVideo v3 x4 | 98.6 ms | **6.8 ms** |
| AnimeJaNai V2 L2 | 30.7 ms | **3.1 ms** |
| Real-CUGAN x2 | 33.4 ms | **13.4 ms** |
| AnimeVideo xs x2 | 25.0 ms | **5.4 ms** |

端到端(1080p 片源,双路并行+NVENC):AnimeJaNai V2 L2 20.7 → **79 fps**,V3 HD L2 20.5 → **79 fps**。

### 修复
- **CUGAN 视频任务崩溃**:Real-CUGAN 权重在 DirectML 后端存在逐帧显存泄漏(实测 1080p
  约 8~30 帧触发 0x887A0006 设备摘除,进程崩溃/挂死),且 CPU/CUDA 路径同样不可靠。
  现在 DML/CUDA 后端创建 CUGAN 任务会直接给出明确提示(安装 TensorRT 组件走 TRT,
  或显式切换 CPU);TRT 后端下 CUGAN 工作正常,不受影响
- u8 包装缓存按源模型失效重建的既有机制保持不变;本轮改动涉及的所有单测与全量回归通过

### 内部
- 新增全模型基准体系(可复跑):纯推理矩阵 `bench_all_models.py`、端到端管线
  `bench_e2e_models.py`、双路+NVENC 天花板编排;完整数据与方法论见 BENCH.md §12~14
- 测试 182 项全绿 + 1 跳过(无 GPU 场景)
