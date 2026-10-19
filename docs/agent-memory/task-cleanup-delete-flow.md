---
name: task-cleanup-delete-flow
description: 删除任务后台清工作目录+预览并广播事件；启动清扫超龄孤儿；取消不删(续跑依据)；"删除慢"实为 8s 轮询滞后
metadata:
  node_type: memory
  type: project
  originSessionId: sess_47ac3143-b4a6-445d-ba79-34e058291244
---

2026-08-25 修（发布前）：取消/失败任务的 `TEMP_DIR/segmented|chunked/{id}` 工作目录刻意保留供续跑（约定见 [[queue-reorder-and-resume]]），但删除任务原本只删 DB 行——本机 .tmp/segmented 已积 560MB/11 目录。修：DELETE /api/tasks 后台线程 `purge_task_files`（segmented/chunked 工作目录 + `previews/{id}*.jpg`）+ 广播 `task_deleted` WS 事件；启动 lifespan `sweep_orphan_workdirs` 只清「DB 无对应任务行 + mtime>1h」的目录（1h 防误删测试/其他 sidecar 实例的新目录）。

"删除响应慢"根因不是接口慢（db 删除毫秒级），是不发事件 + WS 健康时任务列表轮询 8s——卡片最长 8s 才消失。前端删除成功后立即 refreshTasks+refreshStats 兜底。**判例：用户报"慢"先查 UI 刷新链路（事件+轮询间隔），别只盯接口耗时。**

遗留待拍板：任务行永不裁剪（list_tasks 仅显示上限 100），老取消任务目录随行永久保留；超 100 条后界面看不到也删不掉。已向用户提议"N 天未续跑自动清理"。
