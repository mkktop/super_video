import { reactive } from 'vue'
import { api, initBase, type Task, type ModelInfo } from './api'

export const store = reactive({
  ready: false,
  connected: false,
  tasks: [] as Task[],
  models: [] as ModelInfo[],
  gpuName: '',
})

let refreshTimer: ReturnType<typeof setTimeout> | null = null

async function refreshTasks() {
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
  const hw = (await api.hardware()) as { gpus?: Array<{ name: string }> }
  store.gpuName = hw.gpus?.[0]?.name ?? '未知显卡'
  await refreshTasks()
  connectWs()
  store.ready = true
}
