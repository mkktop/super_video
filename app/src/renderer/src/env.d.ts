/// <reference types="vite/client" />

declare global {
  interface Window {
    sv: {
      backendInfo: () => Promise<{ baseUrl: string }>
      pickVideo: () => Promise<string[]>
      pickOutput: (suggest: string) => Promise<string | null>
      showInFolder: (p: string) => void
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
