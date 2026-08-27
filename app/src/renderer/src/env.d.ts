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
      /** 拖拽事件里 File 对象 → 本地绝对路径（Electron 43 File.path 已移除） */
      pathForFile: (f: File) => string
      /** 任务终态上报：窗口未聚焦时主进程弹系统通知并闪任务栏 */
      taskEvent: (kind: 'done' | 'failed', name: string) => void
      /** 任务栏进度：0~1，<0 清除 */
      taskProgress: (pct: number) => void
      win: {
        minimize: () => void
        toggleMaximize: () => void
        close: () => void
        /** 关闭手势=隐藏到托盘（主进程负责托盘与退出确认） */
        setCloseToTray: (v: boolean) => void
        onMaximized: (cb: (max: boolean) => void) => () => void
      }
      /** 主进程请求页面跳转（如通知点击 → 任务页） */
      onNavigate: (cb: (page: string) => void) => () => void
    }
  }
}

export {}
