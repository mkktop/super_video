---
name: sv-data-env-migration-trap
description: 本仓库子进程绝不能设 SV_DATA 环境变量——paths.py 会把
  models_store/data/trt-runtime/.tmp 整体搬走到新数据根
metadata:
  node_type: memory
  type: project
  originSessionId: sess_3f086968-1c73-477c-b686-cc409d63b467
---

2026-09-02 事故：测试 `/api/models` 时给子进程设了 `SV_DATA=$(mktemp -d)`，
sv/paths.py 的「旧版数据目录迁移」逻辑随即把仓库根的 models_store、data、
trt-runtime、.tmp（部分，vset_src 的 git pack 被锁失败中止）**搬进临时目录**，
后续误删临时目录导致已导出 ONNX/源权重丢失（靠 models-v1 已上传部分 +
注册表 sha256 对账 + ONNX 导出字节确定性才恢复）。

**Why:** paths.py 认定仓库根的这些目录是「旧版数据布局」，SV_DATA 指向别处
即触发一次性迁移，没有任何确认。
**How to apply:** 在本仓库跑后端代码/TestClient 一律不要设 SV_DATA；API 冒烟
遇到 401 是因为仓库 .tmp/sidecar.token 存在（dev 运行过），直接调端点函数或
临时改 token 环境变量绕过，别动 SV_DATA。另：background shell 不继承主 shell
的 cd，相对路径一律换绝对路径。
