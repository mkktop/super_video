---
name: r2-mirror-update-fallback
description: R2 备用下载源（2026-09-02 落地；2026-09-04 起源优先级可选+预览版也进 R2）：桶/域名/secrets/机制/待办
metadata:
  node_type: memory
  type: project
  originSessionId: sess_e31e4034-0142-47be-b0b7-646533c8686d
---

description: R2 备用下载源：2026-09-29 随品牌化整体迁桶换域（桶 yuzhen / 域 yuzhen-media.yeyushi.com，旧桶删除）；机制/secrets/坑
metadata:
  node_type: memory
  type: project
  originSessionId: sess_e31e4034-0142-47be-b0b7-646533c8686d
---

**2026-09-29 迁桶换域（随 [[brand-yuzhen-rainframe]] 品牌化）**：新桶 **`yuzhen`**、新自定义域 **`https://yuzhen-media.yeyushi.com/`**（注意换了域名托管：kaikun.top → yeyushi.com，绑 R2 自定义域要求该域 DNS 托管在 Cloudflare），旧桶 `super-video` + 旧域整体**删除**；同日**预览通道整体移除**（见 [[super-video-update-flow]]），R2 只剩 `latest.yml`，CI 不再产 preview.yml/prerelease。仓库侧已改：`index.ts` R2_UPDATE_URL（带迁移注释）、release.yml wrangler 桶名、README 多处。选删除而非冻结是干净的：旧域名彻底不解析 → 旧客户端 isNetworkError → 正常回落 GitHub；**坑：删桶必须连自定义域绑定和 DNS 记录一起清**——只删桶会留下解析到 CF 错误页的记录，5xx 带 statusCode 不满足 isNetworkError 也非 404，「R2 优先」的旧客户端会卡在检查报错不回落。时序硬约束：**下个 tag（v0.5.6）打前新桶必须已建好**，否则 CI 的 Put-R2 三次重试后 exit 1 标红整条 release job（GitHub Release 本体在前面步骤已传完，可补救）。新桶初始为空，下个发版自灌；想提前可用需手动拷当前 latest.yml+exe——本机 wrangler 传 252MB 必断（见坑①），走 R2 网页控制台拖拽。

以下为迁移前的历史事实（机制部分全部仍然有效）：

**机制（electron-updater 源码逐条验证过的事实，勿凭社区说法）**：①切源 = `autoUpdater.setFeedURL()`，每次重建 provider 赋给 clientPromise，无缓存；②`channel` setter 设过就不能清成 null/空串（会 throw），但可设别的字符串；GenericProvider 按 `updater.channel` 读 `{channel}.yml`（默认 latest）且 **404 不回退**、直连 .yml URL 也不支持（newBaseUrl 补斜杠拼坏）→ 2026-09-04（11c8081）起检查时显式置 `channel='preview'|'latest'`：预览通道读 R2 的 preview.yml、稳定读 latest.yml；setter 顺手打开 allowDowngrade 须显式关回（主进程 newerVersion 仍防降级）；③`downloadUpdate` 用检查时缓存的 updateInfoAndProvider（含 provider 快照）→ 每次下载尝试前必须先 checkForUpdates 刷新；④downloadPromise 有 finally 复位，失败后可重试；⑤连通性错误判定 isNetworkError：有 statusCode（HTTP 层到达过）不算网络错不切源。**v0.4.1 实弹翻车（f9843dd 修，随 0.4.3+ 生效）**：Electron 主进程网络失败是 Chromium 格式 `net::ERR_CONNECTION_TIMED_OUT`（**下划线**），旧正则 `timed ?out` 只认空格/无分隔，全串零命中被判"非网络错误"→ R2 回退从未触发（用户 0.4.1 装机国内直连 GitHub 手动检查，超时错误直接透出）。修正则 `timed[-_ ]?out` + 兜底 `net::err_`（Electron 网络失败全是 net::ERR_* 传输层错误，此回退语境都值得试镜像），13 例消息电池验证含原漏判案。修复上线前的 0.4.1 存量用户升级路：开 Clash 走代理检查，或 R2 直链手装 `https://super-video.kaikun.top/super_video_0.4.2_setup.exe`。顺带修 Settings 检查失败文案「发布前属正常，见 README 发布流程」→「请检查网络后重试」——那句是尚无 Release 时代的 dev 语境，error 分支只出现在打包版（dev 走独立 status='dev'），正式版用户看到会误判问题性质。

**客户端回退**（main/index.ts，commit ca4e126）：检查更新先走 GitHub（事实源：通道/更新说明/预发布语义仍在 Release），网络错误自动切 R2 重查一次；下载从最近成功检查的源（checkSource）发起、网络错切另一源重试一次（覆盖国内典型"检查通、下载域不通"）。GitHub 恢复后每次检查先主源自动回。

**CI 与 R2 布局**：release.yml tag 发版时用 `npx wrangler r2 object put` 传同构建的 latest.yml（逐版覆盖，R2 上只有"最新正式版"）+ 安装包（按原名平铺不重名）；secrets 未配置时脚本内判断自动跳过不阻塞发版。latest.yml 内容由 package.json `build.releaseInfo.releaseNotesFile: ../RELEASE_NOTES.md` 烧入 releaseNotes 字段——R2 兜底路径没有 GitHub Release 正文，更新说明靠 yml 字段显示（GitHub 路径 notes 也随之统一为 markdown，cleanNotes 的 HTML/markdown 两分支都兼容）。**~~预览 tag 跳过 R2 上传~~（2026-09-03 实施 48675fb → 2026-09-04 被 11c8081 取代）**：原方案预览 tag 完全跳过 R2（防稳定用户兜底收到预览版）。取代方案=通道文件分名：release.yml 预览 tag 把 latest.yml 改名 preview.yml + exe 同传 R2，稳定读 latest.yml、预览读 preview.yml（客户端 channel=preview 与 CI 同版生效，11c8081 之后），互不越界；旧预览客户端不设 channel 读 latest.yml 只收正式版属设计内降级。主进程对 R2 通道文件 404（preview.yml 未同步）有 isChannelFileMissing 回落 GitHub 的保险。发版核验：预览版 `curl .../preview.yml`、正式版 `curl .../latest.yml` 的 version/sha512 应与 GitHub Release 配套。

**坑**：①本机 wrangler 传 252MB 单请求必断（fetch failed 网络层，走代理也断；CI 无此问题）→ 铺底用网页控制台拖拽；②R2 临时 S3 凭证 API（temporaryCredentials?parentIdType=r2_bucket）404 不通，别走那条路。

**待办**：①0.4.0 铺底 exe 未上传（文件已下载校验好：`C:\Users\28640\AppData\Local\Temp\r2-setup.exe`，sha512 与 latest.yml 一致；latest.yml 已传且裸连 200）→ 用户网页拖拽，并删测试文件 枫丹.pptx；②回退代码随 **v0.4.1 才生效**（存量客户端无回退代码，已问用户是否发版未答）；③两个 CF token 曾在对话明文暴露 → 提醒用户轮换并更新 secrets。
