/**
 * Electron 主进程：窗口管理 + sidecar 生命周期。
 * 关键策略：sidecar detached 拉起 —— UI 崩溃/退出时任务进程不受影响；
 * 有任务运行时退出 UI 不杀 sidecar，重启后自动复用。
 */
import { app, BrowserWindow, dialog, ipcMain, net as electronNet, protocol, shell } from 'electron'
import { spawn, spawnSync, type ChildProcess } from 'node:child_process'
import net from 'node:net'
import path from 'node:path'
import fs from 'node:fs'
import { pathToFileURL } from 'node:url'

// 本地视频预览协议：渲染进程 <video src="svvideo:///D%3A%2F...">。
// 必须在 app ready 前注册 scheme（stream 特权支持视频随读随解）
protocol.registerSchemesAsPrivileged([
  { scheme: 'svvideo', privileges: { stream: true, bypassCSP: true } },
])

let mainWindow: BrowserWindow | null = null
let sidecar: ChildProcess | null = null
let baseUrl = ''

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

async function healthy(port: number): Promise<boolean> {
  if (!(await tryConnect(port))) return false
  try {
    const r = await fetch(`http://127.0.0.1:${port}/api/health`)
    if (!r.ok) return false
    // 必须是我们自己的 sidecar：校验健康标记。仅凭 200 会误复用端口上
    // 恰好对任意路径返回 2xx 的其他程序（其他电脑上实测踩过）
    const body = (await r.json()) as { ok?: boolean; version?: string }
    return body?.ok === true && typeof body.version === 'string'
  } catch {
    return false
  }
}

async function startOrReuseSidecar(): Promise<string> {
  // 1) 复用已有 sidecar（UI 重启场景，任务继续跑）
  for (let p = 8730; p < 8740; p++) {
    if (await healthy(p)) {
      baseUrl = `http://127.0.0.1:${p}`
      console.log(`[sidecar] 复用已有实例 ${baseUrl}`)
      return baseUrl
    }
  }
  // 2) 全新拉起（detached：独立于 Electron 生命周期）
  const root = findRoot()
  const logPath = path.join(root, '.tmp', 'sidecar.log')
  fs.mkdirSync(path.dirname(logPath), { recursive: true })
  const logFd = fs.openSync(logPath, 'a')

  const isPackaged = app.isPackaged
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
      env: isPackaged ? { ...process.env, SV_ROOT: root } : process.env,
    })
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
    throw new Error(
      `sidecar 启动超时（已等 60s，常见原因：杀毒软件拦截了未签名的 sidecar.exe，` +
        `或安装目录不可写）。日志：${logPath}`
    )
  }
  throw new Error('8730-8739 无可用端口')
}

async function hasActiveTasks(): Promise<boolean> {
  try {
    const r = await fetch(`${baseUrl}/api/tasks`)
    const tasks = (await r.json()) as Array<{ status: string }>
    return tasks.some((t) => t.status === 'running' || t.status === 'queued')
  } catch {
    return false
  }
}

/** 启动兜底：清理上次异常退出可能残留的 sidecar 进程（自杀自清） */
async function reapStaleSidecars(): Promise<void> {
  const root = findRoot()
  const own = path.join(root, 'sidecar', 'sidecar.exe')
  // 杀掉指向本安装目录 sidecar.exe 的所有进程（含端口已释放的孤儿）
  const { execFile } = await import('node:child_process')
  try {
    const ps = `Get-CimInstance Win32_Process -Filter "Name='sidecar.exe'" | ` +
      `Where-Object { $_.ExecutablePath -eq '${own.replace(/'/g, "''")}' } | ` +
      `ForEach-Object { Stop-Process -Id $_.ProcessId -Force; "killed $($_.ProcessId)" }`
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
    webPreferences: {
      preload: path.join(__dirname, '../preload/index.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  })
  mainWindow.once('ready-to-show', () => mainWindow?.show())
  const sendMaxState = () =>
    mainWindow?.webContents.send('win:maximized', !!mainWindow?.isMaximized())
  mainWindow.on('maximize', sendMaxState)
  mainWindow.on('unmaximize', sendMaxState)
  // electron-vite dev 模式注入 ELECTRON_RENDERER_URL
  if (process.env.ELECTRON_RENDERER_URL) {
    mainWindow.loadURL(process.env.ELECTRON_RENDERER_URL)
  } else {
    mainWindow.loadFile(path.join(__dirname, '../renderer/index.html'))
  }
}

// 单实例：二次启动聚焦已有窗口（sidecar 复用机制天然支持，但避免双 UI 抢队列）
const gotLock = app.requestSingleInstanceLock()
if (!gotLock) {
  app.quit()
} else {
  app.on('second-instance', () => {
    const win = BrowserWindow.getAllWindows()[0]
    if (win) {
      if (win.isMinimized()) win.restore()
      win.focus()
    }
  })
}

// ---- 自动更新（GitHub Releases，仅打包版；公共仓库无需 token） ----
// 交互模型：检查 -> 用户点"下载更新"（有进度条）-> 用户点"立即重启"。
// 不自动下载：全量包 241MB，静默下载既耗流量又会在用户不知情时弹窗打断。
let updaterBusy = false
let downloadBusy = false

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
  const section = notes.split('\n## ').find((s) => s.startsWith(`v${version}`))
  if (!section) return notes
  return section.slice(`v${version}`.length).trim()
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
    autoUpdater.on('update-downloaded', (info) => broadcast('app:update-ready', info.version))
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
    if (newVersion && newVersion !== current) {
      return {
        status: 'available',
        current,
        version: newVersion,
        notes: sliceNotes(normalizeNotes(r?.updateInfo?.releaseNotes), newVersion),
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
  getAutoUpdater().quitAndInstall(true, true)
}

app.whenReady().then(async () => {
  // svvideo:///D%3A%2F... -> 本地文件流（剪切页预览用）
  protocol.handle('svvideo', (req) => {
    const p = decodeURIComponent(new URL(req.url).pathname)
    return electronNet.fetch(pathToFileURL(p).toString())
  })
  try {
    await reapStaleSidecars()
    await startOrReuseSidecar()
  } catch (e) {
    dialog.showErrorBox('sidecar 启动失败', String(e))
    app.quit()
    return
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
  // 任何情况下退出都连带关掉 sidecar（用户明确要求）；
  // 若真有任务在跑，由 worker 的停止信号自行收尾
  killSidecar()
})

ipcMain.handle('backend:info', () => ({ baseUrl }))

ipcMain.handle('app:version', () => app.getVersion())

ipcMain.handle('app:check-update', () => checkUpdateManually())

ipcMain.handle('app:download-update', () => downloadUpdate())

ipcMain.handle('app:install-update', () => installUpdate())

// ---- 自绘标题栏的窗口控制 ----
ipcMain.on('win:minimize', (e) => BrowserWindow.fromWebContents(e.sender)?.minimize())
ipcMain.on('win:toggle-maximize', (e) => {
  const win = BrowserWindow.fromWebContents(e.sender)
  if (win?.isMaximized()) win.unmaximize()
  else win?.maximize()
})
ipcMain.on('win:close', (e) => BrowserWindow.fromWebContents(e.sender)?.close())

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
  const r = await dialog.showSaveDialog({
    defaultPath: suggest,
    filters: [{ name: 'MP4', extensions: ['mp4'] }, { name: 'MKV', extensions: ['mkv'] }],
  })
  return r.canceled ? null : r.filePath
})

ipcMain.handle('shell:showInFolder', (_e, p: string) => {
  shell.showItemInFolder(p)
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
