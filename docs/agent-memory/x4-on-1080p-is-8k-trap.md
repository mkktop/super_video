---
name: x4-on-1080p-is-8k-trap
description: x4 模型吃 1080p 源=7680×4320 八 K 输出，超 H.264 规格致 h264_nvenc 报误导性 No
  capable devices；hevc_nvenc 支持 8K
metadata:
  node_type: memory
  type: project
  originSessionId: sess_e3931df5-502a-4c1c-a3a4-22785930fc75
---

2026-08-27 双路基准复盘定论（曾一度误立「NVENC@4K 故障」案并已撤销）：

v3-x4/自定义高倍率套在 ≥1080p 源上时输出达 7680×4320（8K）。H.264 规格不含
此尺寸，h264_nvenc 报的错是误导性的 `No capable devices found`（用户可见）；
hevc_nvenc 同尺寸正常，libx264 无限制。三段裸命令验证：nvenc@真4K ✓ /
nvenc@8K ✗ / hevc@8K ✓——与驱动、并发、u8 包装、TRT 全部无关。

**Why**: 排障时容易被报错文案带去查设备/驱动，实际先对照推理矩阵 hw_out 列
（frame_w=源宽×倍率）看是否越界；「硬件编码坏了」这类直觉判断要过一遍尺寸。
**How to apply**: 产品改进候选——提交任务前预检「模型倍率×源分辨率 选 H.264
(软/硬) 时的输出尺寸」，超规给明确提示或建议 HEVC。复现一行：
ffmpeg -f lavfi -i testsrc2=size=7680x4320 -c:v h264_nvenc out.mp4 → No capable devices。
关联 [[dual-stream-parallel]] [[perf-numbers-verify-with-elapsed]]
