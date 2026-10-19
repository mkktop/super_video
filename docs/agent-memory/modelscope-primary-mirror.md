---
name: modelscope-primary-mirror
description: ModelScope 成模型主源（mengkaikun/super-video-models），models-v1 降为
  mirror_urls；同步=注册表对账脚本
metadata:
  node_type: memory
  type: project
  originSessionId: sess_e31e4034-0142-47be-b0b7-646533c8686d
---

2026-09-02 起 manifest 主源 = `https://modelscope.cn/models/mengkaikun/super-video-models/resolve/master/<文件名>`，`mirror_urls` = [models-v1 GitHub]，下载器自动回退（manager.py 原生支持，引擎零改动）。token 在 GitHub secret `MODELSCOPE_TOKEN`（SDK token，等同账号写权限）。**2026-09-03 铺底完成：68 资产 + README 全部就位并逐一核对大小、匿名直链抽验 sha256 通过。随 v0.4.2（tag a223724）发布对用户生效**：release workflow 16 分钟全绿，GitHub/R2 latest.yml 逐字节一致 + notes 烧录核验；release.yml 的 ModelScope 同步步首战即绿（对账发现齐全零重传、README 刷新）。同版落地下载渠道显示（commit 831ea71）：manager.download 新增 source_cb（每次实际发起下载前回调主源+逐镜像）→ app.py 按 URL 域名判定 modelscope/github 经 model_download 事件透出 → store.downloadSource → Models.vue 进度条旁渠道文案（回落 GitHub 镜像瞬间翻转，用户可感知）；全量 352 passed。

**Why:** 用户群在国内，GitHub 直连超时 60s/次才轮到镜像；ModelScope 阿里 CDN 裸连实测 ~10MB/s（68 资产 2.03GB），匿名 200/206 Range 通、LFS 跳 cdn-lfs-cn 签名链。

**How to apply:**
- 同步唯一入口 `.github/scripts/sync_modelscope.py`：注册表对账（远端同名同大小跳过=幂等）；取源三级 bundled → models_store/<模型id>/ → **url + mirror_urls 双源下载**（只试 url 是踩过的坑：主源切 ModelScope 后未铺底文件全 404，CI run#2 failure 与本地批量失败同根因）；sha256+size 双校验后才传；SDK 调用 `direct_connection()` 清代理直连；入口 UTF-8 reconfigure 防 cp1252。
- **批量上传必须走暂存目录 + upload_folder**：upload_file 每文件一次 git commit，跨洋 RTT 放大到 ~90s/文件；upload_folder 并行分批。pipeline 模式整批传完才统一 commit，中途死=全丢——但 ModelScope blob 按内容哈希全局去重（"Blob already exists globally, reuse"），重跑复用不重传。
- **铺底/大传必须走 CI**（sync-modelscope.yml 手动 dispatch，机房 ~10-14MB/s/连接，全量 ~24 分钟）；家宽晚高峰上行单连接 10-26KB/s 本地不可行，且 Windows 杀软杀过 sync python 进程（退出码 0xC0000428）。常规增量走 release.yml tag 步（脚本对账，缺什么传什么）。
- 首轮铺底有"404 税"：61 文件每个先在未铺底主源白烧 3 次 404 重试（~20 分钟），铺底后主源全命中自愈。
- 代理死了别慌：api.github.com 可裸连（git push 不行），GitHub Contents API 可应急把单文件提交到 main（本地事后 fetch + reset --hard 对齐）；注册表系统代理残留会毒化 urllib（`NO_PROXY=*` 绕开）。
- 不变量测试两处锁口径：test_new_models（全注册表主源+mirror 精确断言）、test_encode_opts（animejanai url 前缀）——改主源域名要连改。
- 仓库 README 由 `--readme` 从注册表生成（逐模型许可证表，manifest 自带 license 字段）。
- 相关：[[models-v1-single-source]]（语义已变：models-v1 现为备用源，注册表仍为事实源）。
