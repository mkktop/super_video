import { contextBridge, ipcRenderer } from 'electron'

contextBridge.exposeInMainWorld('sv', {
  backendInfo: () => ipcRenderer.invoke('backend:info') as Promise<{ baseUrl: string }>,
  pickVideo: () => ipcRenderer.invoke('dialog:pickVideo') as Promise<string[]>,
  pickOutput: (suggest: string) => ipcRenderer.invoke('dialog:pickOutput', suggest) as Promise<string | null>,
  showInFolder: (p: string) => ipcRenderer.invoke('shell:showInFolder', p),
})
