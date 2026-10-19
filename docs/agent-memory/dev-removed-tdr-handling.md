---
name: dev-removed-tdr-handling
description: 887A0005/6/7 设备移除族=环境瞬态 TDR；加载降链等 3s×2 重试→CPU 兜底且不进常驻缓存（cb139b4）
metadata:
  node_type: memory
  type: project
  originSessionId: sess_a3398c1f-1992-48ff-95b5-e12ddab90214
---

2026-09-12 用户实机（RTX 5080）：53 张 MangaJaNai x4 任务引擎加载即失败，DML 会话创建抛 887A0005「GPU 设备实例已经暂停」。该码族（887A0005 REMOVED / 887A0006 HUNG / 887A0007 RESET）= **任务开始前驱动已 TDR 重置**，与任务配置无关——发生在创建/执行皆可能。

**Why:** 与 [[masked-oom-cpu-fallback]] 的 OOM 路径病因相反：OOM 后进程内 DML 损坏不可逆（缓存 CPU 正确）；TDR 是环境瞬态，驱动数秒即恢复——兜底出的 CPU 引擎若进 `_ENGINE_CACHE` 会让同签名后续任务全部静默走 CPU，必须不缓存、下一任务重新 GPU 起链。

**How to apply:** `_dev_removed`（worker_engine）识别 str + args bytes（GBK 掩盖）双形态；撞上先 `time.sleep(3)`×2 原配置重试，仍失败 ort_device=cpu/tile=0 保出片。用户侧若反复出现 887A0005 属驱动/硬件层（事件查看器 Event ID 4101 nvlddmkm、DDU 重装驱动、查超频温度），软件侧只负责不硬失败。
