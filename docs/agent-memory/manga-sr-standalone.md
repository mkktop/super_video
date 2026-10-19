---
name: manga-sr-standalone
description: 漫画超分从图片超分独立成档：MangaSR 页 + params.kind=manga 任务类型（2026-09-13）
metadata:
  node_type: memory
  type: project
  originSessionId: sess_49ed2f01-1b84-4841-a442-8c53770ae0ca
---

2026-09-13 漫画超分独立：前端新页 `pages/MangaSR.vue`（文件夹整本为主入口、散页补选为辅、模型默认过滤 scenes 含 manga + 「显示全部模型」开关、整本合并 PDF），图片超分页 ImageSR.vue 移除文件夹模式只管散图。任务类型 `params.kind="manga"`。

**两处默认值翻转（2026-09-30 用户拍板）**：① 整本合并 PDF **默认关**（原默认开；超分后整本图片体积大，无损封装 PDF 易失败/耗时；预设装载语义同步改为 `p.merge_pdf === true` 显式开过才开，与 ImageSR 同款；后端/MCP 从不默认）；② 漫画+图片两页分块默认 **256**（原 0=auto，全图推理大图易爆显存），经 `composables/useTileDefault.ts` localStorage 记忆上次选择（key `sv-tile-image`/`sv-tile-manga` 各自独立；tile 是逐任务参数按纪律不进 settings，与主题同走纯 UI 偏好路线；坑：`Number('')===0`，无存储须先判 null 再喂 Number——test_tile-default.spec.ts 回归）。

**Why:** 用户要把漫画超分独立出来做更细致的优化（去网点/双页/灰度保持等），需要干净的挂点；旧漫画整本任务是 kind=image+folder_src，无法区分统计。

**How to apply:**
- 后端唯一合法值常量 `backend/sv/server/consts.py::_IMAGE_TASK_KINDS = ("image", "manga")`；消费点：worker.py 分岔、runner.py 删源清单(L139)+取消清理豁免(L589)、tasks.py `_create_image_task` 校验落库。**新增图片系消费判断必须用该常量**，漏一处漫画任务会掉进视频探测分支。task_stills 的 `_kind()` 按 params.images 判断，天然覆盖无需改。
- 漫画专属优化在 `worker_image.py::_run_image_job` 按 kind 分岔（docstring 已注明），未拆 worker_manga.py（无空壳）。
- 前端任务类型筛选：Tasks.vue `ofKind()`——视频=无 kind 键；漫画=kind==='manga' 或（kind==='image' && folder_src 旧任务归漫画）。
- CompareModels useModel 按 scenes 含 manga 分流跳 mangasr/imagesr；uiFx.ORDER 与 Sidebar items 顺序必须同步（方向过渡）。
- IAB 走查坑复发：面板遮挡 rAF 停跳→页面过渡卡 enter-from；解法=reload 后立即 evaluate 注入 rAF=setTimeout 替身，交互用 evaluate dispatchEvent（playwright click 的 actionability 永远超时）。

**混装双模型（2026-09-13 同日加）**：`params.model_id_color` + 逐页 `images_meta["lane"]="color"`（缺省 bw）。识别在创建期（tasks.py `_page_is_color`：320² 缩略图通道极差的 **std>4**——单色调图含均匀纸色偏黄旧扫描 std≈0，占比判据会把黄纸误判彩色，勿改回）；结果随任务持久化续跑不重算。worker 双引擎并存懒加载（纯黑白本不建彩模、全彩本不建主模）；`_ENGINE_CACHE` 已槽位化（main/color 各自单条语义），`_main_once` 开头 `_release_aux_slots()` 清彩模槽（不跨任务复用，安全优先）。`_load_onnx_engine` 的 slot 参数必须放关键字末尾（测试大量位置调用）。前端 MangaSR 混装开关：倍率=两模型 scale 交集，交集空禁提交+警告；auto_variant 各车道独立按各自首图高度选档；denoise 档两模型同用。模型卡片网格抽在 `components/MangaModelGrid.vue` 复用三处。

**分趟模式（2026-09-15，已随 v0.5.4 发布）**：`params.mix_pass="split"`（缺省并存 coexist 不落库、非法值 400、仅混装持久化）。worker 分趟=先黑白趟（原页序）→ 趟末丢引用+`_release_slot("main")`+gc → 再建彩模彩色趟；**释放必须在彩模构建之前**（DML「建新再析构旧」崩溃前科的逆序安全面）；纯黑白本不释放（保缓存复用）。关键不变量：PDF/输出按**原页序**（`written_idx` dict 按页号排序，非处理序）、进度按处理位数单调走满、预览取原页序最前成功页（first_pair 带 k 比较）、剖析用 `lane_providers` 构建时记录（趟末引擎已释放仍可列出）。页处理体已抽成 `_process_page(k, meta, engine)` 闭包，两种模式共用。彩色趟重建失败报「黑白页产物已保留」。前端 MangaSR 混装区内「分趟处理（省显存）」复选框，主 hint 文案随开关切换；重试回填 `mixSplit.value = p.mix_pass === 'split'`。

相关：[[compare-thumb-cap-image-tasks]]（图片任务对比口径）、[[vset-model-gap-and-tier1]]（MangaJaNai 漫画首选）

**断点续跑（2026-09-24）**：图片系任务的 checkpoint=逐页产物本身。判据 `worker_image._page_done`：产物存在、非空、**mtime ≥ 任务 created_at−5s**——mtime 门槛区分「本任务中断前写出的页」（跳过续跑）与「上一轮跑完的旧产物」（重跑同文件夹刻意沿用同名逐页覆盖，必须重做），不能用单纯 exists 判断否则重跑秒完成返回旧图。`resume_task` 对图片任务不删 output_path（删了丢断点）且不走视频的 segmented 目录守卫，改为「有任一页产物在即可续，跑过却全没了才 409」。配套：`_skip_page` 记账（ok/written_idx/PDF 原页序都含跳过页，进度事件 1/512 限防 runner 十万次 DB UPDATE）；全跳过路径不建引擎、first_pair 从磁盘回读出预览；分趟模式黑白页全跳过时不做趟末释放。测试 test_image_resume.py。

**十万页扩容（2026-09-24 同批）**：`_FOLDER_SCAN_CAP` 3000→100000。配套防拖垮：创建期尺寸只读图片头部+EXIF 方向换算（不再 exif_transpose 全解码，十万页省几十分钟；混装 lane 识别仍需解码属固有成本）；`_input_exists` 对 >512 清单均匀抽样（全量 stat 会拖死 8s 轮询的列表接口）；列表响应 `_slim_image_manifest` 裁掉 >2000 页的 images 清单（只留首页+images_count，对比页取 imgs[0]、任务卡用 images_count）——详情端点不裁，前端「改参数重试」先 `api.task(id)` 拉全量再回填；`_batch_heights` 抽样（纯提示日志）。DB 里 params 仍存全量清单（worker/删源直读 DB）。
