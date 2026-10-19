---
name: dual-stream-parallel
description: 双路并行产品化（v0.1.20）——CPU 饱和真相、进度局部口径、门槛与协调者结构
metadata:
  node_type: memory
  type: project
  originSessionId: sess_47ac3143-b4a6-445d-ba79-34e058291244
---

双路并行（v0.1.20，commit 76ae88a）：worker 协调者=第 0 路 + 子进程第 1 路，交错分段
（第 i 段归 i%2 路），checkpoint 并集合并，取消/失败 kill_tree 连坐。设置开关
parallel_streams。真机 A/B（5459 帧）：软编 libx264 双路**零增益**（4K 软编单路就吃满
9800X3D，两路抢核）+ NVENC 硬编 **77.5fps（+84%）**——双路必须配硬编才有肉。

**Why:** 用户质疑「总时间一样」拆穿了进度聚合假象（fps 虚高 2 倍）；靠 elapsed_s 对账
才拿到真实数字。产物帧数精确一致 + 耗时逻辑可证无重复处理。

**How to apply:**
- 进度聚合协议：分片路径按「本路局部帧号」上报（segmented 里扣 seg_start），两路局部数
  相加=真实进度。全局帧号直接相加、或增量累计（段间跳跃误计）都坑过。
- 门槛 ≥8 段才并行：每路各自加载 TRT 引擎 ~5-8s，小片双路倒亏（91 帧 51→29fps）。
- 用户反馈速度类问题先看 elapsed_s 而非事件 fps（事件口径曾被聚合 bug 污染）。
- 相关：[[uint8-graph-wrap-speedup]]（单路提速链）、[[trt-component-installer]]（安装版 TRT）。
