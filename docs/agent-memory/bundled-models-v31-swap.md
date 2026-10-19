---
name: bundled-models-v31-swap
description: 2026-09-02 内置模型换血为 AnimeJaNai V3.1 HD 四件——V3.1 权重特性、测试 fixture 分布策略、遗留待拍板项
metadata:
  node_type: memory
  type: project
  originSessionId: sess_e31e4034-0142-47be-b0b7-646533c8686d
---

2026-09-02 随包内置模型从 RealESR AnimeVideo x2/x4（fp32+fp16 四文件 7.4MB）换成 AnimeJaNai V3.1 HD 四件（极速/均衡 × 标准/锐利，官方原生 fp16 ONNX，5.4MB）。RealESR AnimeVideo 全系转纯下载模型（x4 本在 models-v1，xsx2 补传+补 url，见 [[models-v1-single-source]]）；老用户升级后此前吃内置 realesr 的首次使用会提示去市场下载。

**V3.1 权重特性（写测试/引擎相关代码前先记起）：** ONNX 输入名就是 `input`、tensor(float16) 原生进出（喂探测帧必须 np.float16）、batch 维固定 1（测不了批量路径）、H/W 动态任意边长、RGB/0-1、2x。

**Why:** 用户点名"内置换成 V3.1 HD 系列这几个"；四件 sha256 已与注册表对账。

**How to apply:**
- **测试 fixture 分布**：真实权重测试（A/B 一致、marker、跑帧、e2e）用内置 V3.1（原生 fp16 正是 u8 包装的设计对象）；fp32→fp16 转换链路用 onnx.helper 动态构造玩具 conv 模型（仓库不再有 fp32 权重可当输入，玩具权重要够大否则「转换后减半」断言被 protobuf 固定开销淹没）；**动态 batch 场景必须 RealESR**（V3.1 batch 固定 1 会被钳单帧），test_batch 从 models_store 解析、无权重则 skip；conftest 的 dml_available 探测已换 V3.1+float16
- **测试污染坑（换模型才暴露）**：test_image_out 的 e2e 用 cleanup=False（产物给 resume 测试复用），TEMP_DIR 跨轮残留旧 checkpoint 会让本轮直接跳过推理、断言误读上一轮尺寸的旧帧图——换引擎/倍率类改动后该测试必须先清 out 目录与 segmented/<task_id>
- **遗留待用户拍板**：recommend.py `_ANIME_PREF` 首选仍是 realesr-animevideov3（现为下载模型，新装机被推荐一个要先下载的模型）；用户有「TRT 真机基准后再调位次」的既定存档（见 [[vset-model-gap-and-tier1]]），未动
- release.yml 资产同步对 V3.1 四件幂等无害（已在 models-v1）；`/api/models` 的 bundled 状态是动态判定（manifest 全文件在 BUNDLED_DIR 即 true），前端/打包脚本零改动
- **换血连带雷（2026-09-03 实爆）**：移出 RealESR x4 时没人 grep 硬编码引用——engine_select 探测写死该文件名，v0.4.0+ 安装版 CUDA/TRT 探测必败恒回落 DML，已修（动态选 bundled onnx，见 [[engine-probe-hardcoded-model-bug]]）；以后动 bundled 集必须先 `grep -rn <移除的文件名>` 全仓（bench/calibrate 脚本仍有独立引用，属离线工具未清）
