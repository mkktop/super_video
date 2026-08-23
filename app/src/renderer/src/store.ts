import { reactive } from 'vue'
import { api, initBase, wsUrl, type Task, type ModelInfo, type Preset } from './api'

export const store = reactive({
  ready: false,
  connected: false,
  tasks: [] as Task[],
  models: [] as ModelInfo[],
  presets: [] as Preset[],
  downloadProgress: {} as Record<string, number>, // model_id -> 0~1
  gpuName: '',
  engine: null as null | { backend: string; python: string; detail: string },
  hardware: null as null | {
    gpus: Array<{ name: string; vram_gb: number | null }>
    cpu: string
    cpu_cores: number
    ram_gb: number
    nvenc?: boolean
  },
})

/** 界面状态：当前页 / 新建任务弹窗 */
export const ui = reactive({
  page: 'home' as 'home' | 'tasks' | 'models' | 'settings',
  showNewTask: false,
})

let refreshTimer: ReturnType<typeof setTimeout> | null = null

export async function refreshTasks() {
  try {
    store.tasks = await api.tasks()
    store.connected = true
  } catch {
    store.connected = false
  }
}

function scheduleRefresh() {
  if (refreshTimer) return
  refreshTimer = setTimeout(() => {
    refreshTimer = null
    refreshTasks()
  }, 250)
}

export async function refreshModels() {
  try {
    store.models = await api.models()
  } catch {
    /* 模型列表失败不致命 */
  }
}

function handleWsEvent(raw: MessageEvent) {
  try {
    const ev = JSON.parse(String(raw.data))
    if (ev.type === 'model_download') {
      const id: string = ev.model_id
      if (ev.done || ev.failed) {
        const { [id]: _drop, ...rest } = store.downloadProgress
        store.downloadProgress = rest
        refreshModels()
      } else if (typeof ev.progress === 'number') {
        store.downloadProgress = { ...store.downloadProgress, [id]: ev.progress }
      }
      return
    }
  } catch {
    /* 非 JSON 事件按任务刷新处理 */
  }
  scheduleRefresh()
}

function connectWs() {
  const ws = new WebSocket(wsUrl())
  ws.onopen = () => {
    store.connected = true
    refreshTasks()
  }
  ws.onmessage = handleWsEvent
  ws.onclose = () => {
    store.connected = false
    setTimeout(connectWs, 2000)
  }
  ws.onerror = () => ws.close()
}

export async function initStore() {
  await initBase()
  store.models = await api.models()
  store.presets = await api.presets()
  store.hardware = (await api.hardware()) as NonNullable<typeof store.hardware>
  store.gpuName = store.hardware.gpus?.[0]?.name ?? '未知显卡'
  store.engine = await api.engine()
  await refreshTasks()
  connectWs()
  // 轮询兜底：WS 失效时列表仍然实时（本地服务，开销可忽略）
  setInterval(refreshTasks, 1500)
  store.ready = true
}
