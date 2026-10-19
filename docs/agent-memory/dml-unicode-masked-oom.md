---
name: dml-unicode-masked-oom
description: DML 显存不足在中文 Windows 被 UnicodeDecodeError 掩盖（ORT 错误消息 bytes 内嵌系统
  GBK 描述）——_oom 从 e.args 原始 bytes 识别 + 输出像素超 36M 预设 512 tile；73d01a0 已随
  v0.4.7 发布；性能页对瞬时 OOM 盲视的诊断口径；与 gbk-settings 同款报错的第二病因
metadata:
  node_type: memory
  type: project
  originSessionId: sess_f9e027fd-a96b-402a-bf3c-a90f940f6206
---

2026-09-03 用户装版（0.4.6，DML 模式）MangaJaNai x4 图片任务报「引擎加载失败 UnicodeDecodeError: 0xc9 position 240」——与 [[gbk-settings-unicode-engine-fail]] 的 1060 案例报错形态几乎一字不差，但**根因完全不同**。dev 同机复现拿到完整证据链（e.args[1] 是原始 bytes）：

**根因三层**：①真实错误=DML 显存不足 `0x8007000E`（2133p 漫画页 x4 输出 51M 像素，全尺寸 warmup 爆）；②中文 Windows 把系统错误描述（「存储空间不足…」）按 **GBK** 嵌进 onnxruntime 错误消息 bytes，Python 绑定层按 UTF-8 硬解失败 → UnicodeDecodeError 假象完全掩盖 OOM（0xc9@240 就是 GBK 汉字字节）；③worker `_oom` 只认英文 memory/alloc/oom 字样，被假象骗过——本该触发的降 tile 重试没走直接失败。矩阵实测补充：tile 尺寸（264×264）fp32/fp16 全健康、全尺寸全 OOM、干净进程 256 tile DML 全绿；**且 OOM 后同进程 DML 会话已损坏（gc.collect 救不回），降档重建会被 ORT 静默回退 CPU**。

**修复（commit 73d01a0，已随 v0.4.7 发布，2026-09-04）**：A. `_oom` 遍历 `e.args` 的 bytes（GBK+UTF-8 双解）识别 `8007000e/e_outofmemory/out of memory/存储空间不足`——中文系统 DML OOM 从此可判定；B. 降 tile 重试前显式释放失败引擎（engine=None+gc.collect）；C. **治本**：tile==0 且输出像素 `h*w*scale² > 36_000_000` 自动预设 512 分块（1080p x4=33M 不受影响保全尺寸性能、2133p x4≈51M 命中；4K 源 x2 的 8K 输出同样受益）——干净会话一次到位不经历 OOM。测试 +2（args bytes 含 GBK 串的 UnicodeDecodeError 降档 calls==[512,256]、大图预设/1080p 不动），worker/engine 六模块 56 passed；dev 实测 2133p DML+512 tile 全绿。

**诊断知识（2026-09-04 用户连问「16G 显存都不够？」「性能页为何看不出爆显存」后沉淀）**：①大图 OOM=单次大块分配被拒（DML committed resource 需连续大块，桌面/浏览器把显存碎片化后总量有余也借不出），**不是先占满后溢出**——失败瞬间显存可能只用了几个 GB；②事件亚秒级（分配→抛异常→资源释放），性能页 2s 系统级采样天然盲视——性能页只擅长持续性泄漏（CUGAN×DML 型逐帧爬坡一眼可见），瞬时分配失败要靠错误码而非曲线；③16G 跑 1080p→4K 一直稳，是「扫描页分辨率 × 4x」组合过临界；512 分块带重叠拼接逐像素一致，不损画质速度几乎不变。**待查**：装版 0.4.6 现场日志未见 emit_failed 堆栈行（grep「堆栈」零命中，与 45b1dc1 预期不符，dev 复现时堆栈正常）。

**Why:** 同一个报错串（UnicodeDecodeError）在本项目已出现两个独立病因（GBK settings.json vs GBK ORT 错误消息）——见报错先查 `e.args` 里有没有 bytes 再定方向，别按上次的结论套。
**How to apply:** 存量版本临时解法=任务高级选项手动把分块设 512px（甚至 256px）；ORT 异常的真实信息在 UnicodeDecodeError.args[1]（bytes），排障脚本直接 decode('gbk','replace') 看；预设 tile 阈值若将来显存档位分级可按 hardware vram 调整。关联 [[trt-cuda-resize-mangajanai]]（同一天 MangaJaNai 三连坑之二）、[[x4-on-1080p-is-8k-trap]]（大输出显存陷阱族）。
