/**
 * Electron 主进程：窗口管理 + sidecar 生命周期。
 * 关键策略：sidecar detached 拉起 —— UI 崩溃/退出时任务进程不受影响；
 * 有任务运行时退出 UI 不杀 sidecar，重启后自动复用。
 */
import { app, BrowserWindow, dialog, ipcMain, shell } from 'electron'
import { spawn, type ChildProcess } from 'node:child_process'
import net from 'node:net'
import path from 'node:path'
import fs from 'node:fs'

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
    return r.ok
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
    for (let i = 0; i < 40; i++) {
      if (await healthy(p)) {
        console.log(`[sidecar] 已启动 ${baseUrl}（日志 ${logPath}）`)
        return baseUrl
      }
      await new Promise((r) => setTimeout(r, 500))
    }
    throw new Error('sidecar 启动超时，请查看 .tmp/sidecar.log')
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

function killSidecar() {
  if (!sidecar?.pid) return
  if (process.platform === 'win32') {
    spawn('taskkill', ['/pid', String(sidecar.pid), '/T', '/F'], { windowsHide: true })
  } else {
    sidecar.kill('SIGTERM')
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

app.whenReady().then(async () => {
  try {
    await startOrReuseSidecar()
  } catch (e) {
    dialog.showErrorBox('sidecar 启动失败', String(e))
    app.quit()
    return
  }
  createWindow()
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})

app.on('before-quit', async (e) => {
  // 有任务在跑：不杀 sidecar（验收要求"杀 UI 任务不丢"），下次启动复用
  if (sidecar && (await hasActiveTasks())) {
    console.log('[sidecar] 有任务运行中，保留后台服务')
    sidecar = null
    return
  }
  killSidecar()
})

ipcMain.handle('backend:info', () => ({ baseUrl }))

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
