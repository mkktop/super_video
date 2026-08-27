/**
 * Electron 主进程：窗口管理 + sidecar 生命周期。
 * 关键策略：sidecar detached 拉起 —— UI 崩溃/退出时任务进程不受影响；
 * 有任务运行时退出 UI 不杀 sidecar，重启后自动复用（启动时先探测复用，
 * 只有复用失败才清理残留进程——见 whenReady 的顺序）。
 */
import {
  app,
  BrowserWindow,
  dialog,
  ipcMain,
  Menu,
  nativeImage,
  Notification,
  shell,
  Tray,
  type NativeImage,
} from 'electron'
import { spawn, spawnSync, type ChildProcess } from 'node:child_process'
import crypto from 'node:crypto'
import net from 'node:net'
import path from 'node:path'
import fs from 'node:fs'

// 本地视频预览：webSecurity:false 下 <video> 直接 file:// 加载（见 createWindow 注释），
// 渲染进程用 api.ts 的 mediaSrc() 构造地址


let mainWindow: BrowserWindow | null = null
let sidecar: ChildProcess | null = null
let baseUrl = ''
let apiToken = '' // 本地 API 令牌：写 dataRoot/.tmp/sidecar.token + spawn env，renderer 经 backend:info 取
let closeConfirmed = false // 本次关窗已确认过（确认后放行真正的 close）
let quittingForInstall = false // 更新安装拉起的退出：不能被关窗确认拦住
let closeToTray = false // 设置「关闭时最小化到托盘」：关闭手势=隐藏窗口而非退出
let explicitQuit = false // 托盘菜单「退出」：绕过托盘隐藏，走退出确认
let tray: Tray | null = null

/** 关窗确认：后端可达且有 running/queued 任务时问一次。
 *  后端不可达 = 没有可中断的任务，直接退出（否则坏后端时窗口关不掉）。 */
async function confirmClose(): Promise<void> {
  const win = mainWindow
  if (!win) return
  let active = false
  try {
    const r = await fetch(`${baseUrl}/api/tasks`)
    const tasks = (await r.json()) as Array<{ status: string }>
    active = tasks.some((t) => t.status === 'running' || t.status === 'queued')
  } catch {
    active = false
  }
  if (!active || win.isDestroyed()) {
    closeConfirmed = true
    win.close()
    return
  }
  const { response } = await dialog.showMessageBox(win, {
    type: 'question',
    title: '任务正在运行',
    message: '有超分任务正在处理，退出将中断当前任务。',
    detail: '已完成的分段会保留，下次启动后可在任务列表点「继续」续跑。',
    buttons: ['退出', '取消'],
    defaultId: 1,
    cancelId: 1,
    noLink: true,
  })
  if (win.isDestroyed()) return
  if (response === 0) {
    closeConfirmed = true
    win.close()
  } else {
    explicitQuit = false // 取消退出：后续关闭手势恢复托盘隐藏行为
  }
}

// ---- 系统托盘（「关闭时最小化到托盘」模式） ----

/** 显示/还原主窗口（从托盘点击、通知点击、二次启动聚焦共用） */
function showMainWindow(): void {
  const win = BrowserWindow.getAllWindows()[0]
  if (!win || win.isDestroyed()) return
  if (win.isMinimized()) win.restore()
  win.show()
  win.focus()
}

/** 托盘图标：dev 用 build/icon.ico；打包后 build/ 不进 asar，取 exe 文件图标 */
async function trayIconImage(): Promise<NativeImage> {
  const p = path.join(__dirname, '../../build/icon.ico')
  if (fs.existsSync(p)) {
    return nativeImage.createFromPath(p).resize({ width: 16, height: 16 })
  }
  try {
    return await app.getFileIcon(process.execPath, { size: 'small' })
  } catch {
    return nativeImage.createEmpty()
  }
}

async function ensureTray(): Promise<void> {
  if (tray) return
  const img = await trayIconImage()
  if (img.isEmpty()) {
    // 拿不到图标就别启用该模式：窗口隐藏后没有托盘入口会"消失"
    closeToTray = false
    console.warn('[tray] 图标获取失败，「关闭到托盘」本次不可用（照常退出）')
    return
  }
  tray = new Tray(img)
  tray.setToolTip('super_video')
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: '显示主窗口', click: () => showMainWindow() },
      { type: 'separator' },
      { label: '退出', click: () => quitFromTray() },
    ]),
  )
  tray.on('click', () => showMainWindow())
}

function destroyTray(): void {
  tray?.destroy()
  tray = null
}

function quitFromTray(): void {
  explicitQuit = true
  // 窗口可能处于托盘隐藏态，确认框挂在窗口上——先显示再走关窗确认
  showMainWindow()
  mainWindow?.close()
}

function findRoot(): string {
  if (app.isPackaged) return process.resourcesPath  // 安装包布局：resources/{bin,sidecar}
  let dir = app.getAppPath()
  for (let i = 0; i < 5; i++) {
    if (fs.existsSync(path.join(dir, '.venv')) && fs.existsSync(path.join(dir, 'backend'))) {
      return dir
    }
    dir = path.dirname(dir)
  }
  throw new Error('找不到项目根目录（需包含 .venv 与 backend）')
}

/** 数据目录（模型/组件/任务库/日志）解析。
 *  正确位置 = 安装目录的同级目录：NSIS 更新/卸载只清空安装目录本身
 *  （RMDir $INSTDIR），同级不受影响。注意 dirname(resourcesPath) 是安装
 *  目录本身——v0.1.21~v0.2.1 的坑：数据建在了安装目录里，每次更新都被
 *  旧卸载器连带清空。 */
function resolveDataRoot(root: string): string {
  if (!app.isPackaged) return root
  const installDir = path.dirname(root)
  const wrong = path.join(installDir, 'super_video_data') // 误建位置
  const sibling = path.join(path.dirname(installDir), 'super_video_data')
  try {
    // 旧位置数据搬到新位置（同盘 rename 原子；新位置已存在则不动，不覆盖）
    if (fs.existsSync(wrong) && !fs.existsSync(sibling)) {
      fs.mkdirSync(path.dirname(sibling), { recursive: true })
      fs.renameSync(wrong, sibling)
      console.log(`[data] 已迁移数据目录 ${wrong} → ${sibling}`)
    }
    fs.mkdirSync(sibling, { recursive: true })
    fs.accessSync(sibling, fs.constants.W_OK)
    return sibling
  } catch (e) {
    // 安装在 Program Files 等不可写位置时退回用户目录（数据仍在安装目录外）
    const fallback = path.join(app.getPath('appData'), 'super_video_data')
    console.warn(`[data] 同级目录不可用(${e})，改用 ${fallback}`)
    fs.mkdirSync(fallback, { recursive: true })
    return fallback
  }
}

function tryConnect(port: number, timeoutMs = 800): Promise<boolean> {
  return new Promise((resolve) => {
    const s = net.connect({ port, host: '127.0.0.1' }, () => {
      s.destroy()
      resolve(true)
    })
    s.on('error', () => resolve(false))
    setTimeout(() => {
      s.destroy()
      resolve(false)
    }, timeoutMs)
  })
}

async function sidecarInfo(port: number): Promise<{ version: string } | null> {
  if (!(await tryConnect(port))) return null
  try {
    const r = await fetch(`http://127.0.0.1:${port}/api/health`)
    if (!r.ok) return null
    // 必须是我们自己的 sidecar：校验健康标记。仅凭 200 会误复用端口上
    // 恰好对任意路径返回 2xx 的其他程序（其他电脑上实测踩过）
    const body = (await r.json()) as { ok?: boolean; version?: string }
    return body?.ok === true && typeof body.version === 'string'
      ? { version: body.version }
      : null
  } catch {
    return null
  }
}

async function healthy(port: number): Promise<boolean> {
  return (await sidecarInfo(port)) !== null
}

/** 结束占用端口的进程（升级后残留的旧版 sidecar 用；sidecar 是 detached 的，安装器杀不到） */
async function killPortOwner(port: number): Promise<void> {
  const { execFile } = await import('node:child_process')
  try {
    const ps = `Get-NetTCPConnection -LocalPort ${port} -State Listen -ErrorAction SilentlyContinue | ` +
      `Select-Object -ExpandProperty OwningProcess -Unique | ` +
      `ForEach-Object { Stop-Process -Id $_ -Force; "killed $_" }`
    const out = await new Promise<string>((resolve, reject) => {
      execFile('powershell.exe', ['-NoProfile', '-Command', ps], {
        windowsHide: true, timeout: 15000,
      }, (err, stdout) => (err ? reject(err) : resolve(stdout)))
    })
    if (out.trim()) console.log(`[sidecar] 结束旧版进程(端口 ${port}): ${out.trim()}`)
  } catch (e) {
    console.warn(`[sidecar] 结束旧版进程失败(端口 ${port}):`, e)
  }
}

async function startOrReuseSidecar(): Promise<string> {
  // 1) 复用已有 sidecar（UI 重启场景，任务继续跑）——版本不一致的旧实例不复用：
  //    空闲则结束换新；正跑任务则暂用并在日志里提示（跑完重启应用完成切换）
  for (let p = 8730; p < 8740; p++) {
    const info = await sidecarInfo(p)
    if (info) {
      if (info.version !== app.getVersion()) {
        baseUrl = `http://127.0.0.1:${p}`
        if (await hasActiveTasks()) {
          console.log(
            `[sidecar] 复用旧版 ${info.version} 实例 ${baseUrl}（有任务在跑，` +
              `任务完成后重启应用以切换到 ${app.getVersion()}）`)
          return baseUrl
        }
        console.log(`[sidecar] 旧版 ${info.version} 空闲，结束并拉起 ${app.getVersion()}`)
        await killPortOwner(p)
        continue
      }
      baseUrl = `http://127.0.0.1:${p}`
      console.log(`[sidecar] 复用已有实例 ${baseUrl}`)
      return baseUrl
    }
  }
  // 2) 全新拉起（detached：独立于 Electron 生命周期）
  const root = findRoot()
  const isPackaged = app.isPackaged
  // 数据目录在安装目录外（详见 resolveDataRoot 注释）；sidecar 侧同名 SV_DATA
  const dataRoot = resolveDataRoot(root)
  const logPath = path.join(dataRoot, '.tmp', 'sidecar.log')
  fs.mkdirSync(path.dirname(logPath), { recursive: true })
  const logFd = fs.openSync(logPath, 'a')

  // 本地 API 令牌：写 token 文件（复用中的旧 sidecar 按文件实时校验，能接受新令牌）
  // + 随 spawn env 注入（本会话）。renderer 经 backend:info 取走，用于请求头/WS 参数
  apiToken = crypto.randomBytes(24).toString('hex')
  try {
    fs.writeFileSync(path.join(dataRoot, '.tmp', 'sidecar.token'), apiToken, 'utf-8')
  } catch (e) {
    console.warn('[sidecar] 令牌文件写入失败（API 将不鉴权）:', e)
    apiToken = ''
  }

  const py = isPackaged
    ? path.join(root, 'sidecar', 'sidecar.exe')
    : path.join(root, '.venv', 'Scripts', 'python.exe')
  const serveArgs = isPackaged
    ? ['serve', '--port', '{PORT}']
    : [path.join(root, 'backend', 'cli.py'), 'serve', '--port', '{PORT}']

  for (let p = 8730; p < 8740; p++) {
    if (await tryConnect(p)) continue // 端口被非 sidecar 占用，跳过
    baseUrl = `http://127.0.0.1:${p}`
    const args = serveArgs.map((a) => (a === '{PORT}' ? String(p) : a))
    sidecar = spawn(py, args, {
      cwd: isPackaged ? undefined : path.join(root, 'backend'),
      detached: true,
      stdio: ['ignore', logFd, logFd],
      windowsHide: true,
      env: isPackaged
        ? { ...process.env, SV_ROOT: root, SV_DATA: dataRoot, SV_TOKEN: apiToken }
        : { ...process.env, SV_TOKEN: apiToken },
    })
    // 缺 exe/被杀软拦截时 spawn 异步抛 'error'；不挂监听会成为主进程未捕获异常，
    // 秒退也不再傻等满 60s 才报"启动超时"（exit 日志可见）
    sidecar.on('error', (e) => console.error('[sidecar] 启动失败:', e))
    sidecar.on('exit', (code) => console.log(`[sidecar] 进程退出 code=${code}`))
    sidecar.unref()
    fs.closeSync(logFd)
    // 首次启动杀软可能全量扫描 sidecar 目录（155MB），20s 不够，放宽到 60s
    for (let i = 0; i < 120; i++) {
      if (await healthy(p)) {
        console.log(`[sidecar] 已启动 ${baseUrl}（日志 ${logPath}）`)
        return baseUrl
      }
      await new Promise((r) => setTimeout(r, 500))
    }
    // 超时清掉这个起不来的进程再试下一端口（不留孤儿等下次启动 reap）
    if (sidecar.pid) {
      try {
        spawnSync('taskkill', ['/pid', String(sidecar.pid), '/T', '/F'],
          { windowsHide: true, timeout: 8000 })
      } catch { /* 可能已退出 */ }
    }
    sidecar = null
    continue
  }
  throw new Error(
    `后端服务启动超时（每端口已等待 60 秒）。常见原因：安全软件拦截了未签名的 sidecar.exe，` +
      `或安装目录不可写。日志：${logPath}`)
}

async function hasActiveTasks(): Promise<boolean> {
  try {
    const r = await fetch(`${baseUrl}/api/tasks`)
    const tasks = (await r.json()) as Array<{ status: string }>
    return tasks.some((t) => t.status === 'running' || t.status === 'queued')
  } catch {
    return true // 探测失败按"有任务"保守处理：宁可不断旧实例的任务也不能误杀
  }
}

/** 启动兜底：清理上次异常退出残留的 sidecar 进程（自杀自清）。
 *  只在复用失败后调用——能被复用的 sidecar（可能正跑任务）绝不能杀；
 *  taskkill /T 树杀连带 worker/ffmpeg 子进程，避免 Stop-Process 留孤儿。 */
async function reapStaleSidecars(): Promise<void> {
  const root = findRoot()
  const own = path.join(root, 'sidecar', 'sidecar.exe')
  const { execFile } = await import('node:child_process')
  try {
    const ps = `Get-CimInstance Win32_Process -Filter "Name='sidecar.exe'" | ` +
      `Where-Object { $_.ExecutablePath -eq '${own.replace(/'/g, "''")}' } | ` +
      `ForEach-Object { & taskkill.exe /pid $_.ProcessId /T /F | Out-Null; "killed $($_.ProcessId)" }`
    const out = await new Promise<string>((resolve, reject) => {
      execFile('powershell.exe', ['-NoProfile', '-Command', ps], {
        windowsHide: true, timeout: 15000,
      }, (err, stdout) => (err ? reject(err) : resolve(stdout)))
    })
    if (out.trim()) console.log(`[sidecar] 残留清理: ${out.trim()}`)
  } catch (e) {
    console.warn('[sidecar] 残留清理跳过:', e)
  }
}

function killSidecar() {
  try {
    fs.appendFileSync(
      path.join(findRoot(), '.tmp', 'quit.log'),
      `killSidecar called ${Date.now()} sidecar=${sidecar?.pid ?? 'null'}\n`
    )
  } catch { /* 日志失败不影响清理 */ }
  // 同步执行：Electron 退出瞬间会经 job object 带走刚 spawn 的异步子进程，
  // 异步 taskkill/powershell 根本来不及跑（实测残留），必须 spawnSync 阻塞到杀完
  if (process.platform === 'win32') {
    if (sidecar?.pid) {
      try {
        spawnSync('taskkill', ['/pid', String(sidecar.pid), '/T', '/F'],
          { windowsHide: true, timeout: 8000 })
      } catch { /* 进程可能已退出 */ }
    }
    const root = findRoot()
    const own = path.join(root, 'sidecar', 'sidecar.exe').replace(/'/g, "''")
    const ps =
      `Get-CimInstance Win32_Process -Filter "Name='sidecar.exe'" | ` +
      `Where-Object { $_.ExecutablePath -eq '${own}' } | ` +
      `ForEach-Object { Stop-Process -Id $_.ProcessId -Force }`
    try {
      spawnSync('powershell.exe', ['-NoProfile', '-Command', ps],
        { windowsHide: true, timeout: 15000 })
    } catch { /* 兜底失败则交给下次启动的残留清理 */ }
  } else {
    sidecar?.kill('SIGTERM')
  }
  sidecar = null
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1200,
    height: 820,
    minWidth: 960,
    minHeight: 640,
    frame: false, // 自绘标题栏
    backgroundColor: '#141517',
    show: false,
    title: 'super_video',
    // dev 下显示应用图标（打包后窗口自动用 exe 图标；build/ 不进 asar，故判存在）
    icon: fs.existsSync(path.join(__dirname, '../../build/icon.ico'))
      ? path.join(__dirname, '../../build/icon.ico')
      : undefined,
    webPreferences: {
      preload: path.join(__dirname, '../preload/index.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      // 关闭同源限制：允许 <video> 直接用 file:// 播本地视频——Chromium 原生文件
      // 加载器自带 Range 与 moov 尾部索引处理（自定义协议不做尾部探测，moov 在尾的
      // 大 MP4 直接黑屏；转 faststart 影子文件又要吃磁盘）。纯本地单用户工具，风险可控
      // 【2026-08-26 审查复核，拍板保持】：CSP 收敛 + 本地 token 鉴权后同源放宽的
      // 剩余风险小于换自定义协议后视频源兼容性回退的风险；勿在无逐源验证的情况下改回
      webSecurity: false,
    },
  })
  mainWindow.once('ready-to-show', () => mainWindow?.show())
  const sendMaxState = () =>
    mainWindow?.webContents.send('win:maximized', !!mainWindow?.isMaximized())
  mainWindow.on('maximize', sendMaxState)
  mainWindow.on('unmaximize', sendMaxState)
  // 任务栏闪烁在窗口重新获得焦点时清除（通知点击/用户点回窗口）
  mainWindow.on('focus', () => mainWindow?.flashFrame(false))
  // 有任务在跑时关窗需确认：退出会中断处理（分段保留、可续跑，但用户未必知道）
  mainWindow.on('close', (e) => {
    if (closeConfirmed || quittingForInstall || !baseUrl) return
    if (closeToTray && !explicitQuit) {
      // 「关闭到托盘」模式：关闭手势=隐藏窗口，任务继续跑、完成通知照常弹
      e.preventDefault()
      mainWindow?.hide()
      return
    }
    e.preventDefault()
    void confirmClose()
  })
  // electron-vite dev 模式注入 ELECTRON_RENDERER_URL
  if (process.env.ELECTRON_RENDERER_URL) {
    mainWindow.loadURL(process.env.ELECTRON_RENDERER_URL)
  } else {
    mainWindow.loadFile(path.join(__dirname, '../renderer/index.html'))
  }
}

// 单实例：二次启动聚焦已有窗口（sidecar 复用机制天然支持，但避免双 UI 抢队列）
const gotLock = app.requestSingleInstanceLock()
const isSecondInstance = !gotLock
if (!gotLock) {
  app.quit()
} else {
  app.on('second-instance', () => {
    const win = BrowserWindow.getAllWindows()[0]
    if (win) {
      showMainWindow() // 托盘隐藏态也要能拉回来
    }
  })
}

// ---- 自动更新（GitHub Releases，仅打包版；公共仓库无需 token） ----
// 交互模型：检查 -> 用户点"下载更新"（有进度条）-> 用户点"立即重启"。
// 不自动下载：全量包 241MB，静默下载既耗流量又会在用户不知情时弹窗打断。
let updaterBusy = false
let downloadBusy = false
// 已下载待安装的版本：事件只广播一次，renderer 重载后靠 app:update-state 查询恢复
let readyVersion = ''

/** electron-updater 懒加载。打包后动态 import 的 CJS 互操作可能拿到
 * undefined（v0.1.1 实测手动检查报 TypeError），require 直取最稳。 */
function getAutoUpdater(): import('electron-updater').AppUpdater {
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const m = require('electron-updater')
  const updater = m.autoUpdater ?? m.default?.autoUpdater
  if (!updater) throw new Error('electron-updater 加载失败')
  return updater
}

/** releaseNotes 归一化成字符串（GitHub provider 给 release 正文） */
function normalizeNotes(raw: unknown): string {
  if (typeof raw === 'string') return raw.trim()
  if (Array.isArray(raw)) {
    return raw.map((x) => (x && typeof x === 'object' ? String((x as any).note ?? '') : String(x ?? ''))).join('\n').trim()
  }
  return ''
}

/** Release 正文是仓库根 RELEASE_NOTES.md 全量，按 "## vX.Y.Z" 分节，只取目标版本那一节 */
function sliceNotes(notes: string, version: string): string {
  // 版本号后必须紧跟空白/结尾：找 v0.2.3 不能命中 v0.2.30 开头的节
  const re = new RegExp(`^v${version.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(\\s|$)`)
  const section = notes.split('\n## ').find((s) => re.test(s))
  if (!section) return notes
  return section.slice(`v${version}`.length).trim()
}

/** 语义化版本比较：a > b（远端更旧时不得提示"可更新"，否则成了降级安装） */
function newerVersion(a: string, b: string): boolean {
  const pa = a.replace(/^v/, '').split('.').map((x) => parseInt(x, 10) || 0)
  const pb = b.replace(/^v/, '').split('.').map((x) => parseInt(x, 10) || 0)
  for (let i = 0; i < 3; i++) {
    const d = (pa[i] ?? 0) - (pb[i] ?? 0)
    if (d !== 0) return d > 0
  }
  return false
}

/** electron-updater 的 GitHub provider 把 release 正文渲染成 HTML（<h1>/<ul>…），
 * 设置页按纯文本展示会直接露出标签；无依赖地转成干净文本行。 */
function htmlToText(html: string): string {
  const s = html
    .replace(/>\s+</g, '><') // 去掉标签间源码换行，避免块级标签换行后叠加出空行
    .replace(/<br\s*\/?>/gi, '\n')
    .replace(/<\/(p|li|h1|h2|h3|h4|ul|ol|div)>/gi, '\n')
    .replace(/<li[^>]*>/gi, '- ')
    .replace(/<[^>]+>/g, '')
    .replace(/&nbsp;/gi, ' ')
    .replace(/&lt;/gi, '<')
    .replace(/&gt;/gi, '>')
    .replace(/&quot;/gi, '"')
    .replace(/&#39;/gi, "'")
    .replace(/&amp;/gi, '&')
  return s
    .split('\n')
    .map((l) => l.trim())
    .join('\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
}

/** 正文清洗：HTML→文本（去掉文档标题/版本号行，弹窗已单独显示版本）；markdown 则按版本分节 */
function cleanNotes(raw: unknown, version: string): string {
  const notes = normalizeNotes(raw)
  if (/<\/(p|li|ul|h\d|div)>/i.test(notes)) {
    const lines = htmlToText(notes).split('\n')
    while (
      lines.length &&
      (!lines[0] || lines[0] === '更新说明' || /^v?\d+\.\d+\.\d+$/.test(lines[0]))
    ) {
      lines.shift()
    }
    return lines.join('\n')
  }
  return sliceNotes(notes, version)
}

function broadcast(channel: string, payload: unknown): void {
  for (const w of BrowserWindow.getAllWindows()) w.webContents.send(channel, payload)
}

function setupAutoUpdate(): void {
  if (!app.isPackaged) return
  try {
    const autoUpdater = getAutoUpdater()
    autoUpdater.autoDownload = false
    // 差分下载实测会把旧版安装包错拼成"新更新"(v0.1.0→v0.1.1 两次复现)，
    // 禁用后走全量下载 + sha512 校验，241MB 一次性代价换正确性
    autoUpdater.disableDifferentialDownload = true
    autoUpdater.on('download-progress', (p) => broadcast('app:update-progress', Math.round(p.percent)))
    autoUpdater.on('update-downloaded', (info) => {
      readyVersion = info.version
      broadcast('app:update-ready', info.version)
    })
    // EventEmitter 无 error 监听器即抛未捕获异常（离线点"检查更新"就崩主进程）
    autoUpdater.on('error', (e) => {
      console.error('[updater] error:', e)
      broadcast('app:update-error', String(e))
    })
  } catch (e) {
    console.error('[updater] 初始化失败:', e)
  }
}

async function checkUpdateManually(): Promise<{
  status: string
  current: string
  version?: string
  notes?: string
  error?: string
}> {
  const current = app.getVersion()
  if (!app.isPackaged) return { status: 'dev', current }
  if (updaterBusy) return { status: 'busy', current }
  updaterBusy = true
  try {
    const r = await getAutoUpdater().checkForUpdates()
    const newVersion = r?.updateInfo?.version
    if (newVersion && newerVersion(newVersion, current)) {
      return {
        status: 'available',
        current,
        version: newVersion,
        notes: cleanNotes(r?.updateInfo?.releaseNotes, newVersion),
      }
    }
    return { status: 'latest', current, version: newVersion }
  } catch (e) {
    return { status: 'error', current, error: String(e) }
  } finally {
    updaterBusy = false
  }
}

async function downloadUpdate(): Promise<{ ok: boolean; error?: string }> {
  if (downloadBusy) return { ok: false, error: '下载已在进行' }
  downloadBusy = true
  readyVersion = '' // 重新下载覆盖旧包，状态回到下载中
  try {
    await getAutoUpdater().downloadUpdate()
    return { ok: true }
  } catch (e) {
    return { ok: false, error: String(e) }
  } finally {
    downloadBusy = false
  }
}

function installUpdate(): void {
  // 静默安装 + 装完自动拉起：辅助安装器(可选目录版)带 UI 运行会卡住更新流程
  quittingForInstall = true // 安装路径的退出不做关窗确认
  getAutoUpdater().quitAndInstall(true, true)
}

app.whenReady().then(async () => {
  // 顺序即语义：先探测复用（健康且版本一致的存量 sidecar——可能正跑任务——
  // 绝不能杀）；复用失败才清残留僵尸再拉新。曾先无条件 reap 再复用，
  // 把"UI 崩溃后重启接着看任务"的场景一刀杀掉了（与文件头承诺矛盾）
  try {
    await startOrReuseSidecar()
  } catch (e) {
    await reapStaleSidecars()
    try {
      await startOrReuseSidecar()
    } catch (e2) {
      dialog.showErrorBox('sidecar 启动失败', `${e2}`)
      app.quit()
      return
    }
  }
  createWindow()
  setupAutoUpdate()
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})

app.on('before-quit', () => {
  // 第二实例的 quit 不能杀 sidecar——它属于正在运行的第一实例（含其任务）
  if (isSecondInstance) return
  destroyTray()
  // 任何情况下退出都连带关掉 sidecar（用户明确要求）；
  // 若真有任务在跑，由 worker 的停止信号自行收尾
  killSidecar()
})

ipcMain.handle('backend:info', () => ({ baseUrl, token: apiToken }))

ipcMain.handle('app:version', () => app.getVersion())

ipcMain.handle('app:check-update', () => checkUpdateManually())

ipcMain.handle('app:download-update', () => downloadUpdate())

ipcMain.handle('app:install-update', () => installUpdate())

ipcMain.handle('app:update-state', () => ({ ready: readyVersion, downloading: downloadBusy }))

// ---- 自绘标题栏的窗口控制 ----
ipcMain.on('win:minimize', (e) => BrowserWindow.fromWebContents(e.sender)?.minimize())
ipcMain.on('win:toggle-maximize', (e) => {
  const win = BrowserWindow.fromWebContents(e.sender)
  if (win?.isMaximized()) win.unmaximize()
  else win?.maximize()
})
ipcMain.on('win:close', (e) => BrowserWindow.fromWebContents(e.sender)?.close())

// 「关闭到托盘」开关：renderer 读到设置后同步过来；开启即建托盘，关闭即撤
ipcMain.on('win:set-close-to-tray', (_e, v: boolean) => {
  closeToTray = !!v
  if (closeToTray) void ensureTray()
  else destroyTray()
})

// ---- 任务状态通知与任务栏进度 ----
// renderer 在 WS 事件里检测 running→终态迁移后上报；窗口未聚焦时才弹系统通知
// （聚焦时界面自身变化已足够），无论聚焦与否都闪一次任务栏图标。
ipcMain.on('task:event', (_e, payload: { kind: 'done' | 'failed'; name: string }) => {
  const win = BrowserWindow.getAllWindows()[0]
  if (!win) return
  win.flashFrame(true)
  if (win.isFocused()) return
  try {
    const n = new Notification({
      title: payload.kind === 'done' ? '超分任务完成' : '超分任务失败',
      body: payload.name,
    })
    n.on('click', () => {
      win.flashFrame(false)
      // 窗口可能被最小化或托盘隐藏：完整还原
      if (win.isMinimized()) win.restore()
      win.show()
      win.focus()
      win.webContents.send('win:navigate', 'tasks')
    })
    n.show()
  } catch (e) {
    console.warn('[notify] 系统通知不可用（仅闪烁任务栏）:', e)
  }
})

// 任务栏进度：pct ∈ [0,1]，<0 清除；无 running 任务时 renderer 传 -1
ipcMain.on('task:progress', (_e, pct: number) => {
  BrowserWindow.getAllWindows()[0]?.setProgressBar(pct)
})

ipcMain.handle('dialog:pickVideo', async () => {
  const r = await dialog.showOpenDialog({
    properties: ['openFile', 'multiSelections'],
    filters: [
      {
        name: '视频文件',
        extensions: ['mp4', 'mkv', 'mov', 'avi', 'webm', 'flv', 'ts', 'm4v', 'wmv'],
      },
    ],
  })
  return r.canceled ? [] : r.filePaths
})

ipcMain.handle('dialog:pickOutput', async (_e, suggest: string) => {
  // 过滤器跟随建议文件名的扩展名（MP4/MKV/MOV），另附所有文件兜底
  const ext = (suggest.split('.').pop() ?? '').toLowerCase()
  const filters = ['mp4', 'mkv', 'mov'].includes(ext)
    ? [
        { name: ext.toUpperCase(), extensions: [ext] },
        { name: '所有文件', extensions: ['*'] },
      ]
    : [{ name: 'MP4', extensions: ['mp4'] }, { name: 'MKV', extensions: ['mkv'] }]
  const r = await dialog.showSaveDialog({ defaultPath: suggest, filters })
  return r.canceled ? null : r.filePath
})

ipcMain.handle('dialog:pickDir', async () => {
  // 图片序列输出目录选择
  const r = await dialog.showOpenDialog({ properties: ['openDirectory'] })
  return r.canceled ? null : r.filePaths[0]
})

ipcMain.handle('dialog:pickImages', async () => {
  // 图片超分：多选图片
  const r = await dialog.showOpenDialog({
    properties: ['openFile', 'multiSelections'],
    filters: [
      { name: '图片文件', extensions: ['png', 'jpg', 'jpeg', 'webp', 'bmp', 'tif', 'tiff'] },
    ],
  })
  return r.canceled ? [] : r.filePaths
})

ipcMain.handle('fs:exists', async (_e, p: string) => {
  const fs = await import('node:fs')
  return !!p && fs.existsSync(p)
})

ipcMain.handle('shell:showInFolder', (_e, p: string) => {
  shell.showItemInFolder(p)
})

ipcMain.handle('shell:openPath', (_e, p: string) => {
  if (p) void shell.openPath(p)
})

ipcMain.handle('dialog:pickModel', async () => {
  const r = await dialog.showOpenDialog({
    properties: ['openFile'],
    filters: [{ name: 'ONNX 模型', extensions: ['onnx'] }],
  })
  return r.canceled ? null : r.filePaths[0]
})

ipcMain.handle('dialog:saveLog', async (_e, content: string) => {
  const r = await dialog.showSaveDialog({
    defaultPath: 'super_video_日志.txt',
    filters: [{ name: '文本', extensions: ['txt', 'log'] }],
  })
  if (r.canceled || !r.filePath) return null
  const fs = await import('node:fs')
  fs.writeFileSync(r.filePath, content, 'utf-8')
  return r.filePath
})
