import { reactive } from 'vue'
import { api, initBase, type Task, type ModelInfo } from './api'

export const store = reactive({
  ready: false,
  connected: false,
  tasks: [] as Task[],
  models: [] as ModelInfo[],
  gpuName: '',
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

function connectWs() {
  const ws = new WebSocket(baseUrl.replace('http', 'ws') + '/ws')
  ws.onopen = () => {
    store.connected = true
    refreshTasks()
  }
  ws.onmessage = scheduleRefresh
  ws.onclose = () => {
    store.connected = false
    setTimeout(connectWs, 2000)
  }
  ws.onerror = () => ws.close()
}

export async function initStore() {
  await initBase()
  store.models = await api.models()
  store.hardware = (await api.hardware()) as NonNullable<typeof store.hardware>
  store.gpuName = store.hardware.gpus?.[0]?.name ?? '未知显卡'
  await refreshTasks()
  connectWs()
  // 轮询兜底：WS 失效时列表仍然实时（本地服务，开销可忽略）
  setInterval(refreshTasks, 1500)
  store.ready = true
}
