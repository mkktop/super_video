---
name: optimization-batch-v048
description: 2026-09-04 优化大批次落地清单:app.py 按域拆 routes/+state.py、worker.py 拆
  common/engine/image、gpu_lease 租约、前端 async 页面+manualChunks+vitest、progress 就地
  patch、db user_version
metadata:
  node_type: memory
  type: project
  originSessionId: sess_475b603b-a100-4625-923a-45d254f6b786
---

2026-09-04 一次性优化批次(基于三轮代码扫描的报告),全部落地,未 commit、未发版:

**引擎跨任务进程复用(后续补充实现,386 passed 验证)**
- worker `main(serve=True)` 循环:任务 rc=0 后不退出,`sys.stdin.readline()` 读下一行 task_id 续跑;rc!=0 直接退出(失败/取消后会话健康度不可信,OOM 即损坏级联);stdin EOF 优雅退出。runner spawn 带 `--serve` + `stdin=PIPE`(`_worker_spawn_cmd` 纯函数)。
- runner 事件循环拆 `_pump_until_final(cur)`(stdout 跨任务连续流,终态事件不断流)+ 续喂判定:done 且未 stopping 且闸门放行 → `db.next_queued()` 取下一任务 → finalize done → stdin 喂 id → 继续泵;每续喂任务写各自 owner.pid。
- worker_engine `_ENGINE_CACHE`(进程内单条):签名=(weight,scale,variant,precision,tile初值,batch,warmup_hw,ort_device初值)全用入口参数——降 tile/降链(TRT→trt_cpu)是同签名下的确定性修复,降链成功后的会话按初值签名缓存,下个同签名任务直接命中(避免 MangaJaNai TRT 每任务白崩一次)。换签名重建前 clear+gc 释放旧显存。
- 测试 test_worker_reuse.py 5 个:同签名构造 1 次/换签名重建 2 次/失败不续读 stdin/spawn 命令带 --serve/缓存同进程生效。坑:**模块级 _ENGINE_CACHE 会跨测试残留**——用引擎打桩的测试(par_task fixture 等)必须先 clear,否则上个用例的缓存条目绕过打桩。

**后端**
- `app.py` 1552→~160 行:路由按域拆 `sv/server/routes/{system,models,tasks,trim,compare}.py`;单例下沉 `state.py`(bus/runner/perf/_download_lock/cached_hardware,顺带消灭 trt_component 的延迟 import 循环 hack);共享常量 `consts.py`(_CODECS 等)。测试兼容:app.py re-export create_task/TaskCreate/_render_output_stem/gc_sr_logs/get_models/create_preset 等;monkeypatch 目标已迁移(app.X→routes.tasks.X / routes.models.X / routes.compare.X,见 test_gpu_decode/test_compare/test_downloader/test_engine_display/test_sr_profiling)。
- `worker.py` 1002→508 行:`worker_common.py`(emit/prof 性能日志)、`worker_engine.py`(_load_onnx_engine/_cugan_alt_hint)、`worker_image.py`(_run_image_job)。测试 patch 目标:OnnxSrEngine/settings→worker_engine;_load_onnx_engine/model_file/emit→worker_image;SR_LOG_DIR→worker_common(test_sr_profiling、test_image_sr、test_pdf_merge 已改)。
- **gpu_lease.py**:队列任务(runner._run_one 包装 _run_one_guarded,try_acquire 循环可取消+finally 释放)与 compare/trim 作业(线程内阻塞 acquire)显存互斥;辅助作业插队语义(任务在 release 后、aux 等待计数未清零前 try_acquire 让位)。try_acquire 只给任务方用,who 参数仅是持有者标签。
- db.py `PRAGMA user_version` 版本化迁移(=1);perf.py 采样失败计数+首败/每 30 连败 print 落 sidecar.log;CUGAN 拒绝提示按 N 卡/非 N 卡分路(_cugan_alt_hint)。

**前端**
- 7 页 defineAsyncComponent 拆 chunk(Home/Tasks/Models/Perf/Logs/Compare/Settings);**KeepAlive 四常驻页(NewTask/Trim/ImageSR/CompareModels)必须静态 import**——Vue KeepAlive 对未 resolve 的 async wrapper 不匹配 include(取 __asyncResolved.name),首切草稿会丢。
- manualChunks:vue/naive-ui 独立(主 chunk 1.58MB→44KB+分层);build script 前置双 vue-tsc;vitest+jsdom 落地 18 个测试(store.sameTask/mergeTasks、utils、videoPicks composable)。
- store.ts progress 事件就地 patch(progress_frames/fps_run/eta_sec),不再每拍全量拉任务表;Tasks 页 tick 仅在关机横幅存在时运行;api.ts `_get_json` 助手统一查 response.ok。
- NewTask/Trim 拖拽+最近文件逻辑抽 `composables/videoPicks.ts`(useRecentVideos/useFileDrop)。

相关:[[app-split-router-test-patch-map]] [[ast-split-loses-main-entry]]

**前端 Vue 拆分(后续补充)**
- NewTask.vue 1295→1161:抽 useModelOptions(场景筛选/已装优先排序/倍率降噪选项)/useEncoderOptions(编解码音轨字幕选项与提示)/useCustomResolution(只缩不放纪律);顺带修排序比较器 NaN bug(bundled 缺省时 Number(undefined) 致排序静默失效)。
- Settings.vue 1055→807:抽 useScheduleGate/useOutputSettings/useCompareCache/useTrtComponent/useAppUpdate(各域状态+保存动作+apply(s) 供 onMounted 装配)。
- 模板/样式不动,KeepAlive 语义不变;vitest 24 个(新增 newtask-composables.spec)。
