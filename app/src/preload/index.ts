import { contextBridge, ipcRenderer, webUtils, type IpcRendererEvent } from 'electron'

contextBridge.exposeInMainWorld('sv', {
  backendInfo: () => ipcRenderer.invoke('backend:info') as Promise<{ baseUrl: string; token?: string }>,
  appVersion: () => ipcRenderer.invoke('app:version') as Promise<string>,
  checkUpdate: (allowMirror?: boolean) => ipcRenderer.invoke('app:check-update', allowMirror) as Promise<{
    status: string
    current: string
    version?: string
    notes?: string
    error?: string
  }>,
  downloadUpdate: () => ipcRenderer.invoke('app:download-update') as Promise<{ ok: boolean; error?: string }>,
  installUpdate: () => ipcRenderer.invoke('app:install-update') as Promise<void>,
  updateState: () => ipcRenderer.invoke('app:update-state') as Promise<{
    ready: string
    downloading: boolean
    source: 'github' | 'r2'
  }>,
  // 更新通道同步给主进程（electron-updater allowPrerelease 的开关来源）
  setUpdateChannel: (channel: 'stable' | 'preview') =>
    ipcRenderer.send('app:set-update-channel', channel),
  // 更新下载源同步给主进程（检查/下载走哪个源：auto/github/r2）
  setUpdateSource: (source: 'auto' | 'github' | 'r2') =>
    ipcRenderer.send('app:set-update-source', source),
  onUpdateProgress: (cb: (p: { percent: number; source: 'github' | 'r2' }) => void) => {
    const fn = (_e: IpcRendererEvent, p: { percent: number; source: 'github' | 'r2' }) => cb(p)
    ipcRenderer.on('app:update-progress', fn)
    return () => ipcRenderer.removeListener('app:update-progress', fn)
  },
  onUpdateReady: (cb: (version: string) => void) => {
    const fn = (_e: IpcRendererEvent, version: string) => cb(version)
    ipcRenderer.on('app:update-ready', fn)
    return () => ipcRenderer.removeListener('app:update-ready', fn)
  },
  pickVideo: () => ipcRenderer.invoke('dialog:pickVideo') as Promise<string[]>,
  pickImages: () => ipcRenderer.invoke('dialog:pickImages') as Promise<string[]>,
  pickOutput: (suggest: string) => ipcRenderer.invoke('dialog:pickOutput', suggest) as Promise<string | null>,
  pickDir: () => ipcRenderer.invoke('dialog:pickDir') as Promise<string | null>,
  pickModel: () => ipcRenderer.invoke('dialog:pickModel') as Promise<string | null>,
  saveLog: (content: string) => ipcRenderer.invoke('dialog:saveLog', content) as Promise<string | null>,
  showInFolder: (p: string) => ipcRenderer.invoke('shell:showInFolder', p),
  fsExists: (p: string) => ipcRenderer.invoke('fs:exists', p) as Promise<boolean>,
  openPath: (p: string) => ipcRenderer.invoke('shell:openPath', p) as Promise<void>,
  // 拖拽取本地路径：Electron 43 的 File.path 已移除，必须走 webUtils
  pathForFile: (f: File) => webUtils.getPathForFile(f),
  // 任务终态通知（未聚焦时系统通知+闪任务栏）与任务栏进度
  taskEvent: (kind: 'done' | 'failed', name: string) =>
    ipcRenderer.send('task:event', { kind, name }),
  taskProgress: (pct: number) => ipcRenderer.send('task:progress', pct),
  win: {
    minimize: () => ipcRenderer.send('win:minimize'),
    toggleMaximize: () => ipcRenderer.send('win:toggle-maximize'),
    close: () => ipcRenderer.send('win:close'),
    // 「关闭时最小化到托盘」模式开关（主进程建/撤托盘并改变关闭手势行为）
    setCloseToTray: (v: boolean) => ipcRenderer.send('win:set-close-to-tray', v),
    onMaximized: (cb: (max: boolean) => void) => {
      const fn = (_e: IpcRendererEvent, max: boolean) => cb(max)
      ipcRenderer.on('win:maximized', fn)
      return () => ipcRenderer.removeListener('win:maximized', fn)
    },
  },
  // 通知点击后主进程聚焦窗口并让 renderer 跳任务页
  onNavigate: (cb: (page: string) => void) => {
    const fn = (_e: IpcRendererEvent, page: string) => cb(page)
    ipcRenderer.on('win:navigate', fn)
    return () => ipcRenderer.removeListener('win:navigate', fn)
  },
})
