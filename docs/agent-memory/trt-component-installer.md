---
name: trt-component-installer
description: 安装版 TRT 可选组件（v0.1.18~19）机制、资产布局、frozen GBK 坑、上传直连提速、py7zr CI 事故
metadata:
  node_type: memory
  type: project
  originSessionId: sess_47ac3143-b4a6-445d-ba79-34e058291244
---

安装版吃 TRT 的载体 = `ROOT/trt-runtime` 组件目录（2026-08-25 落地，v0.1.18）：
`python/onnxruntime`（GPU 版包）+ `dlls/`（CUDA/TRT DLL 打平）+ manifest.json（ABI 判据）。
frozen worker 在首次 import onnxruntime 前往 `sys.meta_path[0]` 插定向 finder 重定向导入
（FrozenImporter 优先于 sys.path，insert 没用）；DLL 靠 add_dll_directory+PATH。
激活过晚（DML 版已进 sys.modules）必须拒绝。资产 = GitHub release `runtime-v1`：
core 1.33GB + 8 个分架构 builder 包（nvidia-smi compute_cap 探测选装，砍 nvrtc 省 179MB，
cufft 是硬依赖）。`backend/scripts/build_trt_component.py` 产出/回填清单/上传。
**2026-09-04 起下载加速**：trt_component_assets.json 九资产 url→ModelScope 主源 + mirror_urls→GitHub
（95be133，随 0.4.8+ 生效）；trt_component._download_asset 主源 404/网络错自动回落镜像；
资产铺底与后续增量由 [[modelscope-primary-mirror]] 的同一对账池覆盖（sync 脚本读 TRT json），
远端已核验（core 1.33GB 匿名直链实测 14.3MB/s、sha256 逐字节匹配）。

**Why:** 基础安装包体积不能涨（用户约束），TRT 库只能按需下载；frozen 与 dev 两套进程
模型差异是所有坑的根源。

**How to apply:**
- 换 sidecar Python 大版本 → 组件 ABI 失效自动回退，需 bump 组件 VERSION+RELEASE_TAG 重产。
- **frozen GBK 坑**：PyInstaller exe 无视 PYTHONIOENCODING/PYTHONUTF8（实测），管道 stdout
  走系统 GBK——任何中文输出的子命令都要在 cli.main() 入口 reconfigure utf-8（已做）。
  症状：任务失败 "runner: 'gbk' codec can't encode"。
- 上传 GitHub release 走本机代理只有 ~47KB/s；`curl --noproxy '*'` 直连 3.4~6MB/s（76 倍）。
  git credential fill 取 token + curl 直传是既定路线。
- 组件安装排障：看 `resources/.tmp/sidecar.log` 与 `/api/trt-component`（phase/done/total）；
  `sidecar.exe ort-check --session <模型>` 一键验证组件链路。
- 组件相关链路细节见 BENCH.md「TRT 可选组件」节；TRT 引擎缓存/编译提示见
  [[uint8-graph-wrap-speedup]]，显卡架构事实见 [[model-broadening-and-trt]]。

**发布与支持矩阵（v0.1.18 已发，2026-08-25）**：release 绿（CI 产物 setup.exe 253MB）。
TRT 10 硬性要求 Turing sm75+（2018 起的 N 卡）：20/30/40/50 系各有专属 builder 包，未知新架构
走 ptx JIT 兜底；**GTX 10 系（Pascal sm61，含用户的 1060）装组件无益**——CUDA 档对小模型还慢于
DML；驱动≥525。A 卡/Intel 不适用（走 DML）。已知 UX 洞：设置页卡片对非 N 卡无拦截，点了会
白下 1.5GB 后回退 DML（待加守卫）。v0.1.17 的 3.6x 通用优化全卡生效（DML 层）。

**用户侧下载翻车模式（首发实测）**：1.5GB 组件下载共用 download_proxy 设置（默认跟随系统代理
=Clash），长连接易被掐——症状 `SSL: UNEXPECTED_EOF_WHILE_READING`，卡片报"上次安装失败"。
处置=重试/显式代理 http://127.0.0.1:7897/换节点。**短板：无自动重试、无 Range 断点续传
（失败从零重下）**，已向用户提过加固方案待确认。

**测试坑（CI 抓到）**：trt_component 与 trt_runtime 各持一份 `COMPONENT_DIR` 副本（Path 导入时
拷贝），monkeypatch 只改前者时 status()→read_manifest() 仍读后者——**本机真实组件目录把洞
掩盖成全绿**。修法：status() 显式传参 + 测试双模块都替换。写组件状态相关测试前先把本机
trt-runtime 挪走复验（模拟 CI 干净环境）。

**`/api/engine` 展示口径（2026-08-25 修）**：曾在 runner 启动时 `select_engine()` 不带设置参数
缓存一次、永久显示 DirectML——用户切 TensorRT 后设置页不认（任务本身一直正确：_run_one
每任务按设置现选）。现改：空闲时按设置推算下一任务后端（与 _run_one 同口径）、任务中如实
返回 runner.engine；tests/test_engine_display.py 覆盖。**select_engine 首次 TRT 探测可达
180s，绝不能在事件循环内同步调**（_run_one 曾如此，首个 TRT 任务会把整个 sidecar 卡死：
WS/HTTP 全停）——/api/engine 与 _run_one 都走 run_in_executor；探测结果 _PROBE_CACHE
进程内缓存，同一 sidecar 生命周期只付一次。
