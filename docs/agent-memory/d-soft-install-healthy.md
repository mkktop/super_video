---
name: d-soft-install-healthy
description: D:\soft\super_video 健康安装已升级为用户主力实测机（跟版极快，0.4.5-preview.1→0.4.6 一天三版）；装版排障工具箱（token/DB/日志路径）
metadata:
  node_type: memory
  type: project
  originSessionId: sess_f28c85f3-180e-43f1-814d-6b0b3dc16d5b
---

2026-08-24 用户在这台开发机（ZCode 所在机器）把 v0.1.3 装到 `D:\soft\super_video`，验证健康：`resources\.tmp\sidecar.log` 存在且持续 200（UI 轮询正常），`sidecar.db` 已建，版本 0.1.3。

另一台电脑（用户提到"其他电脑装到 D 盘连不上后端"）的诊断要点：
- 那次 `resources` 下**无 .tmp 目录** → 主程序从未成功拉起 sidecar；可能原因：启动端口扫描误复用端口上其他 HTTP 服务（已修，v0.1.3 的 healthy() 校验 ok+version），或杀软拦截未签名 sidecar.exe
- 排查命令：看 `resources\.tmp\sidecar.log`；`netstat -ano | findstr 873` 查端口占用；杀软隔离记录
- v0.1.3 已带修复，若仍失败优先怀疑杀软，加信任目录 `安装目录\resources\sidecar\`

**Why:** 安装版"连不上后端"最可能是端口误复用或杀软拦截，本机 D:\soft 装的是好的参照系。

**How to apply:** 用户报安装问题先看 `.tmp\sidecar.log` 是否存在/内容，再查 8730-8739 端口，最后查杀软记录；本机健康安装可对比。

**2026-09-03~04 起该装版已是用户主力实测机**（跟版极快：两天内 0.4.5-preview.1→0.4.5→0.4.6→0.4.7），GPU/TRT 类修复先用它试跑再发版已成惯例。数据根=`D:\soft\super_video_data`，排障工具箱：
- `.tmp\sidecar.log`：uvicorn access log + 任务 log 事件（TRT 编译提示、降链/降 tile 说明都在这）
- `.tmp\sidecar.db`：**sqlite 查失败任务最快定位模型**（日志无模型名时）——`SELECT model_id, error FROM tasks WHERE status='failed' ORDER BY created_at DESC`（MangaJaNai CUDA 崩溃即靠此定位）
- `.tmp\sidecar.token` + `curl --noproxy "*" -H "X-SV-Token: ..." http://127.0.0.1:8730/api/health`（探活/拉作业状态/资产 206 验证）
- `data\settings.json`（GBK 排查对象）、`models_store\`（权重+fp16 兄弟文件）、`compare\`（对比产物布局可辨作业形态）、`trt-runtime\`（TRT 组件）
- 已知盲区：个别 worker 堆栈行未落装版日志（见 [[dml-unicode-masked-oom]] 待查项），日志无痕时改查 DB 或 dev 复现

**2026-09-12 补充**：`data\sr_logs\*.log`（sr_profiling 开着就逐任务落盘，含模型/provider/precision/tile/张数/成败）是**确认可疑任务实际跑在哪个引擎的最快途径**——DAT2 黑图排查即靠它实锤 TensorrtExecutionProvider；`tasks` 表有 model_id/params 列（params 里 tile:0 是 falsy 默认，实际 tile 由 spec.tile_hint 兜底，勿被误导）。用户日常配置 engine=trt+precision=fp16——TRT 层缺陷总是先在用户手里爆。
