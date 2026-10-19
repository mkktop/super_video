---
name: models-v1-single-source
description: 模型市场全部下载源统一为本仓库 models-v1 Release；资产上传的坑与校验方法
metadata:
  node_type: memory
  type: project
  originSessionId: sess_f28c85f3-180e-43f1-814d-6b0b3dc16d5b
---

2026-08-24 起，super_video 模型注册表（backend/sv/models/registry_json/）中所有带 url 的文件全部指向 `github.com/mkktop/super_video/releases/download/models-v1/<文件名>`，不再依赖 vs-mlrt/xinntao 等第三方直链。CUGAN×6/RIFE/pth 是一次性人工上传（本地 models_store 已有副本，sha 与注册表一致）；bundled 的模型由 release.yml 的「注册表驱动幂等同步」步骤在发版时补缺。资产数：V3.1 后 33（2026-08-31），ArtCNN×3 + DIS×2 后 38（2026-09-01），2026-09-02 内置换血补传 xsx2 后 68。

**内置（bundled/随包）模型现状（2026-09-02 v0.4.0 起）**：AnimeJaNai V3.1 HD 四件（极速/均衡 × 标准/锐利，官方原生 fp16 ONNX，共 5.4MB）；原 RealESR AnimeVideo x2/x4（fp32+fp16 四件）已全部移出 bundled 转纯下载模型，注册表全带 url。注意「bundled 模型 = manifest 无 url 只随包」的旧惯例已终结——现在 manifest 一律写 url，bundled 只是"额外随包的本地副本"；release.yml 同步步骤只上传 bundled 目录里有的文件，纯下载模型（如 xsx2）必须人工先传 models-v1。测试对 bundled 权重的依赖已全部改为：V3.1（DML 探测/A-B/marker/引擎 fixture，注意 V3.1 是 float16 进出、输入名 input、batch 固定 1）或动态构造的玩具 ONNX（fp32 转换链路/末端 Cast/batch 场景）；test_batch 专测 RealESR 动态 batch，从 models_store 解析（CI 无权重则 skip）。

**Why:** 用户明确要求模型下载只认自己的 Release（防第三方删档）；且 models-v1 是公开 Release，匿名可下载。

**How to apply:**
- 新增模型：文件上传到 models-v1（`POST uploads.github.com/repos/mkktop/super_video/releases/375371878/assets?name=X`），注册表写 url+sha256；若文件也放 bundled，release.yml 会自动同步，无需人工传。
- 上传坑：大文件 curl 上传可能连接中断返回 HTTP 100，但 GitHub 服务端会留下 `state=starter` 的僵尸资产记录（不可下载）。需 DELETE 后重传，且加 `-H "Expect:"` 禁用 100-continue。
- 校验：`GET .../releases/{id}/assets` 的 `digest` 字段（`sha256:...`）与注册表 sha256 比对即可，无需真下载；starter 态资产没有 digest。
