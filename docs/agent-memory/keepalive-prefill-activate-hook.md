---
name: keepalive-prefill-activate-hook
description: 四个 KeepAlive 常驻表单页的跨页预填必须同时挂 onMounted+onActivated，只挂 onMounted 会静默失效
metadata:
  node_type: memory
  type: project
  originSessionId: sess_17413170-0490-4de1-9726-9d9dfa7ed7aa
---

v0.2.8 起 NewTask/Trim/ImageSR/CompareModels 被 `<KeepAlive :include>` 常驻（App.vue）。这些页面消费跨页预填信号（ui.pendingInput/pendingModel/pendingScale/pendingCompare/pendingTaskParams）时，**只挂 onMounted 是错的**：首次进入后组件进缓存，再次跳转只触发 onActivated，`onMounted` 不再执行——信号被静默忽略（2026-08 用户报"剪切页拿这段对比模型→对比界面点不了开始对比"即此因）。

**Why:** KeepAlive 下 mounted 只有一次；activated 每次缓存激活都发（含首次），两钩子连发靠消费后置空信号保证幂等。NewTask 用 `watch(ui.page)` 是等价做法。

**How to apply:** 给这四页（或未来任何进 include 列表的页）加跨页预填/跳转衔接时，一律写成 `function consume(){...}; onMounted(consume); onActivated(consume)`；带新素材跳入还要顺手 reset 上次残留的 job/result 卡，否则停在旧结果视图看不到配置区。参考 CompareModels.vue 的 consumePendingCompare / [[queue-reorder-and-resume]]。

**2026-09-13 补：单挂 watch 也有对偶坑**——`watch([() => ui.page, () => ui.pendingTaskParams])` 只在组件已存在时收得到通知；从任务页「改参数重试」**首次直达**目标页（组件从未挂载）时 watcher 注册晚于 store 变更，参数静默丢失（NewTask 历史上靠"用户总先访问过该页"掩盖）。稳妥写法=双钩子+watch 三管齐下靠置空幂等，NewTask/ImageSR/MangaSR 的 consumeRetryParams 均此结构。改参数重试已按任务类型分流：视频→newtask、图片→imagesr、漫画（含旧 folder_src 任务）→mangasr，分流在 Tasks.vue retryWithParams。
