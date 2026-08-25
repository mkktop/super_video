import { contextBridge, ipcRenderer, type IpcRendererEvent } from 'electron'

contextBridge.exposeInMainWorld('sv', {
  backendInfo: () => ipcRenderer.invoke('backend:info') as Promise<{ baseUrl: string }>,
  appVersion: () => ipcRenderer.invoke('app:version') as Promise<string>,
  checkUpdate: () => ipcRenderer.invoke('app:check-update') as Promise<{
    status: string
    current: string
    version?: string
    notes?: string
    error?: string
  }>,
  downloadUpdate: () => ipcRenderer.invoke('app:download-update') as Promise<{ ok: boolean; error?: string }>,
  installUpdate: () => ipcRenderer.invoke('app:install-update') as Promise<void>,
  updateState: () => ipcRenderer.invoke('app:update-state') as Promise<{ ready: string; downloading: boolean }>,
  onUpdateProgress: (cb: (percent: number) => void) => {
    const fn = (_e: IpcRendererEvent, percent: number) => cb(percent)
    ipcRenderer.on('app:update-progress', fn)
    return () => ipcRenderer.removeListener('app:update-progress', fn)
  },
  onUpdateReady: (cb: (version: string) => void) => {
    const fn = (_e: IpcRendererEvent, version: string) => cb(version)
    ipcRenderer.on('app:update-ready', fn)
    return () => ipcRenderer.removeListener('app:update-ready', fn)
  },
  pickVideo: () => ipcRenderer.invoke('dialog:pickVideo') as Promise<string[]>,
  pickOutput: (suggest: string) => ipcRenderer.invoke('dialog:pickOutput', suggest) as Promise<string | null>,
  pickDir: () => ipcRenderer.invoke('dialog:pickDir') as Promise<string | null>,
  pickModel: () => ipcRenderer.invoke('dialog:pickModel') as Promise<string | null>,
  saveLog: (content: string) => ipcRenderer.invoke('dialog:saveLog', content) as Promise<string | null>,
  showInFolder: (p: string) => ipcRenderer.invoke('shell:showInFolder', p),
  win: {
    minimize: () => ipcRenderer.send('win:minimize'),
    toggleMaximize: () => ipcRenderer.send('win:toggle-maximize'),
    close: () => ipcRenderer.send('win:close'),
    onMaximized: (cb: (max: boolean) => void) => {
      const fn = (_e: IpcRendererEvent, max: boolean) => cb(max)
      ipcRenderer.on('win:maximized', fn)
      return () => ipcRenderer.removeListener('win:maximized', fn)
    },
  },
})
