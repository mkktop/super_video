---
name: uint8-graph-wrap-speedup
description: 2026-08-25 提速已产品化(commit 6164108)：u8 图手术+管线三协程重叠，真机
  5.65→20.2fps(3.6x)；DML 末端 Cast 坑与实现位置
metadata:
  node_type: memory
  type: project
  originSessionId: sess_47ac3143-b4a6-445d-ba79-34e058291244
---

2026-08-25 研究并当日产品化（commit 6164108；基准脚本 bench_fullspeed.py/bench_uint8.py，
数据在 BENCH.md；场景：动漫测试1.mp4 1080p→4K，AnimeJaNai V3 HD L2 fp16，RTX 5080/9800X3D）：

- 原瓶颈：每帧 ~100ms CPU numpy 前后处理（GPU 裸推理仅 44ms，GPU 利用率 27%）。
- 落地后真机全链路：**libx264 默认链 5.65→20.2fps（3.6x，91 帧 16.1s→5.9s）**；nvenc
  20.0fps 持平 → 已是纯推理瓶颈，编码器不影响速度。下一步天花板：装 TRT wheel
  （预期推理再 -30~50%，e2e 冲 30fps+）。
- 实现位置：`sv/engines/u8_wrap.py`（图手术，io 约定自适应 rgb/bgr × 0-1/0-255，fp16
  末端 Cast 绕过）；OnnxSrEngine `u8_wrap=True` 参数 + load 惰性生成缓存
  （.tmp/u8_wrap/<stem>_u8.onnx）+ 每模型×后端一次 A/B 逐位校验（.ok.<provider> 标记，
  不过静默回退原路径）；tile/固定尺寸/batch>1/TRT 不包装；StreamPipeline 三协程重叠
  （reader/executor 线程推理/writer）。
- 实现期踩的坑：①新增图输入 frame_u8 必须 g.input.insert 声明（漏了 checker 报拓扑错）；
  ②onnx 这版无 PixelShuffle schema，合成测试模型用 DepthToSpace(CRD) 替代；③管线里
  batch>1 推理保持同步调用（DML 线程池批量路径有死锁前科）；④worker 的 [engine] print
  会被 runner 存进任务 error 字段（非 UI 报错，别被吓到）。
- 证伪方向不必再试：par2 双段并行无益（DML 串行 session.run）、batch=4 全后端倒退
  （1080p 恒 b1）、CUDA EP 包装后仍慢于 DML（48.2 vs 36.7ms）、**同进程双 DML session
  并发=0x887A0005 驱动级崩溃**（硬否决）；跨进程双实例稳态 26.5fps(+17%)/GPU 83.5%
  有效但要改 runner 架构+显存×2，性价比低于 TRT，暂不产品化（bench_dual.py 可复测）。
- 用户问过"[engine] u8 包装生效但还在跑"——正常：20fps ≈ 1.5 倍实时出片，首任务另有
  几秒一次性包装/校验开销；健康指标 = 任务 fps ~20 + 性能页 GPU ~70%/200W（若 fps ~6
  是连了改码前的旧 sidecar，重启应用）。

**Why:** 这些坑（DML 末端 Cast 0x8007023E、frame_u8 声明、PixelShuffle 缺席、批量死锁
前科）重踩各花半小时以上；证伪结论不记会被人反复提议再测。

**How to apply:** 后续给引擎/管线动刀前先看本条与 BENCH.md 的落地记录；新增模型遇包装
失败会自动回退属预期；TRT 落地时先读 [[model-broadening-and-trt]]（fp16/Clip 家族同类坑）。

**TRT 实测补充（2026-08-25 下午，commit 5328695）**：pip 默认 tensorrt=11.2/cu13 与
ORT 1.24.4 不兼容（要 nvinfer_10.dll）——装 `tensorrt-cu12-libs==10.16.1.11`（纯库 wheel），
且 site-packages/tensorrt_libs 必须进 PATH。TRT+u8包装 = 13.9ms/帧（DML 36.7 的 2.6x），
+重叠 e2e **50.7fps**（现版 2.2x，超实时）；maxdiff=1 恒定在 A/B 容差内。新瓶颈=软解码
（57fps 顶）。产品化清单在 BENCH.md（engine=trt 别跳过包装 / nvidia_dlls 补 tensorrt_libs /
首任务引擎构建数十秒需提示）。TRT 引擎=按"模型图×输入分辨率×GPU 架构×精度"编译的专属
二进制（每图约 6MB，.tmp/trt_cache，类比游戏着色器编译）：首任务构建几十秒进度条不动
（必须给"仅首次编译"提示防误判死机），之后秒级加载；换模型/换分辨率/驱动大版本更新/
清 .tmp 会触发重建——用户反馈"怎么又慢了"先查这几样。

**TRT 已产品化（同日 commit c0a1bd4）**：engine=trt+包装合体，真机软编默认链 39.7fps 冷/
49.5fps 热（91 帧 3.3s）；包装 session 走 `_try_session(..., model_path=cache)`——曾误建到
原始 float 模型文件（marker 存在时潜伏、首帧才炸），已有防回归测试（断言 _u8_sess 入参
uint8）。升级复用旧后端已修：health 报 `sv.__version__`（**发版时与 app/package.json 同步
bump，当前 0.1.17**），Electron 比对版本——旧版空闲则杀旧拉新、有任务暂用并提示。用户
dev 机 engine 已设为 trt；打包版无 TRT 组件自动回退 DML（"多种显卡"边界在此）。

**batch_hint 静默禁用包装 + 64×64 预热毒害 DML（2026-08-25 晚，修复来自 test 分支/用户
5070 Ti 机，已 review 合并进 main）**：注册表 9 个 onnx 模型全带 `batch_hint:4`，worker 对
≤720p 输入取 hint → batch=4 命中"batch>1 不包装"，**静默**走标准慢路径；>720p 本来就强制
b1 不受影响。修复（worker.py）：非用户显式指定 batch 且无 tile 时强制 batch=1（批量 +6%
收益远小于禁包装代价）。第二坑：DML 会话跑过 64×64 小形状后，真实尺寸执行路径被拖慢且
**不可逆**（先小后大无法自愈）——预热改用源帧真实尺寸 (info.height, info.width)。5080
独立复测：93.4ms/帧(b4+64 预热)→76.2(b1+64 预热)→68.2(b1+真实预热)；幅度硬件相关
（5070 Ti 报 4x/+50%，5080 为 +37%/+12%），方向一致即可信。同分支另带分段剖析埋点
（StreamPipeline.stage_stats → work/perf_stages.jsonl，无行为变化，排查用）。协作模式：
用户在另一台 5070 Ti 机器修代码推 test 分支，我 review+本机复现后合并。
