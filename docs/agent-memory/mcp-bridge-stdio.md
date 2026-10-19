---
name: mcp-bridge-stdio
description: MCP stdio bridge 已落地——零依赖手写协议层的拍板理由、发现/鉴权复刻 Electron 逻辑、smoke 抓出的
  POST Content-Type 坑
metadata:
  node_type: memory
  type: project
  originSessionId: sess_c926d3d9-ac48-4dcd-af91-54e9597100c7
---

2026-09-29 落地 `backend/sv/mcp_server.py`（`sidecar.exe mcp` / `python -m sv.mcp_server`），把运行中 sidecar 暴露为 MCP server 给 AI 客户端（Claude Desktop/ZCode/Cursor）。34 项测试 + 真机 smoke 全过。**已随 v0.7.0 发布（2026-09-30）**；同版带上：漫画 PDF 默认关、漫画/图片分块默认 256+localStorage 记忆（useTileDefault）。

关键拍板与事实：
- **协议层零依赖手写**（initialize/tools/list/tools/call/ping 逐行 JSON-RPC，stdlib）：官方 mcp SDK 的 httpx 传递依赖与仓库 pin 的 httpx2 共存未经验证，且协议子集自 2024-11-05 稳定。用户未答选择题，按推荐项执行。
- **发现/鉴权复刻 Electron**：扫 8730-8739 + health `ok` 标记 + 版本一致优先（smoke 实证：8730 挂着安装版 v0.5.5 时正确选中 8731 dev v0.6.0）；token 候选 = SV_TOKEN → 数据根 token 文件（frozen 从 exe 位置复刻 resolveDataRoot 序列）→ None，逐个试 /api/engine，401 重读重试一次（adoptReuseToken 语义）。
- **smoke 抓出的真 bug（单测结构上抓不到）**：urllib 带 data 默认 Content-Type=x-www-form-urlencoded → FastAPI 422；必须显式 `Content-Type: application/json`。教训：FakeBridge 跳过了 _http_json，POST 带体的路径只有真 sidecar 能验。
- **Windows 关管道抛 OSError 22 而非 BrokenPipeError**，_serve 写失败静默收工。
- stdin 必须 reconfigure utf-8（cli.main 只配了 stdout/stderr）；中文路径才不乱码。
- 工具面 10 个 rf_*：不暴露 delete/remove/reorder/settings 写；输出纪律 30 任务/20 文件/images>20 只回计数。漫画整夹走 rf_task_create 的 input_folder（bridge 侧扫描，清单不过 AI 上下文）。
- **准入总闸已产品化（同日二期）**：settings `mcp_enabled` 默认**开**（用户拍板；真正 opt-in=配置 AI 客户端那一刻，开关是吊销权）。sidecar 中间件对 bridge UA（`rainframe-mcp/*`）关闭时 403、文案自解释（指向「AI 助手」页）；`/api/health` 豁免（否则 bridge 报「连不上」而非「去开闸」）；普通 UI 流量零开销（仅 bridge UA 才读 settings 文件）。**UI 入口是侧栏独立页「MCP 服务」（日志下方、设置之前，key=aiassistant；用户拍板从设置卡升级——三段配置/用法说明是指南不是设置项；9-30 再拍板显示名由「AI 助手」改「MCP 服务」，组件文件/路由 key 刻意未动）**：状态+开关+三段预填路径片段（Claude Desktop/Cursor/ZCode）+能力边界两栏；另有「任意 AI 客户端（通用）」指令块（9-30 用户提出"丢给 AI 自己接入"：跨客户端无统一深链标准，通用路径=把命令行+rf_status 验证+两种报错指引写成给 AI 的自然语言指令，AI 自己写自家 MCP 配置并验证）；路径来自 `backend:info` 新增 `mcpCommand`（安装版 `resources\sidecar\sidecar.exe mcp`；dev=venv python + `cli.py mcp`，与 spawn 同源解析）。接线四处：App.vue PAGES（异步 chunk）/Sidebar items+spark 图标/uiFx ORDER/store ui.page 联合类型/CommandPalette 页面清单。
- bridge 只代理不拉起 sidecar（双 runner 抢 GPU/sqlite）；用户侧配一次 stdio 即可。ZCode 项目级配置已写 `D:\work\super_video\.zcode\config.json`（真实客户端连接验证仍待用户重启会话后做）。

相关：[[super-video-project-goal]]、[[local-proxy-hijacks-localhost]]（bridge 用 ProxyHandler({}) 直连）、[[brand-yuzhen-rainframe]]
