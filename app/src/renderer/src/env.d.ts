/// <reference types="vite/client" />

declare global {
  interface Window {
    sv: {
      backendInfo: () => Promise<{ baseUrl: string; token?: string }>
      appVersion: () => Promise<string>
      checkUpdate: () => Promise<{
        status: string
        current: string
        version?: string
        notes?: string
        error?: string
      }>
      downloadUpdate: () => Promise<{ ok: boolean; error?: string }>
      installUpdate: () => Promise<void>
      updateState: () => Promise<{ ready: string; downloading: boolean }>
      onUpdateProgress: (cb: (percent: number) => void) => () => void
      onUpdateReady: (cb: (version: string) => void) => () => void
      pickVideo: () => Promise<string[]>
      pickImages: () => Promise<string[]>
      pickOutput: (suggest: string) => Promise<string | null>
      pickModel: () => Promise<string | null>
      saveLog: (content: string) => Promise<string | null>
      showInFolder: (p: string) => void
      fsExists: (p: string) => Promise<boolean>
      pickDir: () => Promise<string | null>
      openPath: (p: string) => Promise<void>
      win: {
        minimize: () => void
        toggleMaximize: () => void
        close: () => void
        onMaximized: (cb: (max: boolean) => void) => () => void
      }
    }
  }
}

export {}
