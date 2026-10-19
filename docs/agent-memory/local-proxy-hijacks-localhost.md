---
name: local-proxy-hijacks-localhost
description: 本机系统代理(Clash 7897)会劫持 localhost HTTP 请求，curl 探测 sidecar 必须加 --noproxy
metadata:
  node_type: memory
  type: user
  originSessionId: sess_b30b9045-a96b-45c3-911c-d1af82427698
---

这台开发机的系统代理（Clash，127.0.0.1:7897）会接管 localhost 请求：对无监听端口返回空 200 或 502，导致 `curl http://127.0.0.1:8730/api/health` **退出码 0 但无 JSON**，误判"sidecar 在跑"。

**Why:** 2026-08-24 验证剪切功能时被坑：真实情况是端口上什么都没有，代理伪造了响应。

**How to apply:** 命令行探测/冒烟 sidecar 一律 `curl --noproxy 127.0.0.1` 或设 `NO_PROXY=127.0.0.1,localhost`（python urllib 也吃 NO_PROXY）。判断 sidecar 是否存活以 netstat LISTENING 为准。Electron 应用内不受影响（app 自身 healthy() 校验 JSON 标记，v0.1.3 修过）。另一坑（2026-08-25 用户实测）：大文件长连接经系统代理下载会被掐断——1.5GB GitHub 资产下到一半 `SSL: UNEXPECTED_EOF_WHILE_READING`，处置=重试/显式指定代理/换节点；上传则反过来，走代理仅 ~47KB/s、直连 5MB/s（上传用 `curl --noproxy '*'`）。**再补（2026-09-02）：本机 python urllib 调 GitHub API 即使环境有 HTTPS_PROXY 也不走代理、直连 SSL EOF**——git bash 里 `python -c` 的 urllib 直连 api.github.com 必挂（curl 同环境却正常，走系统代理）；调 GitHub API 用 curl：`git credential fill` 取 token + `curl -x http://127.0.0.1:7897 -H "Authorization: Bearer $TOKEN"`，查 Release/上传资产（POST uploads.github.com）均实测可靠。
