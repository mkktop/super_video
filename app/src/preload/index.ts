import { contextBridge, ipcRenderer, type IpcRendererEvent } from 'electron'

contextBridge.exposeInMainWorld('sv', {
  backendInfo: () => ipcRenderer.invoke('backend:info') as Promise<{ baseUrl: string }>,
  pickVideo: () => ipcRenderer.invoke('dialog:pickVideo') as Promise<string[]>,
  pickOutput: (suggest: string) => ipcRenderer.invoke('dialog:pickOutput', suggest) as Promise<string | null>,
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
