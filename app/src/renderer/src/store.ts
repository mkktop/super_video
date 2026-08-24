import { reactive } from 'vue'
import {
  api,
  initBase,
  wsUrl,
  type Task,
  type ModelInfo,
  type Preset,
  type Stats,
  type PerfSample,
} from './api'

export const store = reactive({
  ready: false,
  connected: false,
  tasks: [] as Task[],
  stats: { total: 0, done: 0, frames: 0, bytes: 0 } as Stats,
  models: [] as ModelInfo[],
  presets: [] as Preset[],
  downloadProgress: {} as Record<string, number>, // model_id -> 0~1
  gpuName: '',
  engine: null as null | { backend: string; python: string; detail: string },
  perf: {
    latest: null as PerfSample | null,
    samples: [] as PerfSample[], // 最近 1 小时,与后端环形缓冲同步
  },
  hardware: null as null | {
    gpus: Array<{ name: string; vram_gb: number | null }>
    cpu: string
    cpu_cores: number
    ram_gb: number
    nvenc?: boolean
    av1_nvenc?: boolean
    amf?: boolean
    svt_av1?: boolean
  },
})

/** 界面状态：当前页 / 新建任务弹窗 / 全页对比 */
export const ui = reactive({
  page: 'home' as
    | 'home'
    | 'trim'
    | 'tasks'
    | 'models'
    | 'perf'
    | 'logs'
    | 'settings'
    | 'compare',
  showNewTask: false,
  compareTaskId: null as string | null,
  pendingInput: null as string | null, // 打开向导时预填的输入（剪切→超分衔接）
})

/** 打开新建任务向导并预填输入文件 */
export function openWizardWith(input: string) {
  ui.pendingInput = input
  ui.showNewTask = true
}

export function openCompare(taskId: string) {
  ui.compareTaskId = taskId
  ui.page = 'compare'
}

let refreshTimer: ReturnType<typeof setTimeout> | null = null
let pollTimer: ReturnType<typeof setTimeout> | null = null
let wsOk = false

/** 任务逐字段相等（params 深比较）。相等时保留旧对象引用，TaskCard 的 props
 *  不变即可整体跳过重渲染——高频轮询下只有真正变化的卡片会重新渲染。 */
function sameTask(a: Task, b: Task): boolean {
  if (a === b) return true
  const keys = Object.keys(a) as (keyof Task)[]
  if (keys.length !== Object.keys(b).length) return false
  for (const k of keys) {
    if (a[k] !== b[k]) {
      if (k !== 'params' || JSON.stringify(a.params) !== JSON.stringify(b.params)) return false
    }
  }
  return true
}

function mergeTasks(incoming: Task[]): void {
  const old = new Map(store.tasks.map((t) => [t.id, t]))
  store.tasks = incoming.map((t) => {
    const o = old.get(t.id)
    return o && sameTask(o, t) ? o : t
  })
}

export async function refreshTasks() {
  try {
    mergeTasks(await api.tasks())
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

export async function refreshStats() {
  try {
    store.stats = await api.stats()
  } catch {
    /* 统计失败不致命，下次事件再刷新 */
  }
}

/** 拉性能历史快照(整段替换);断线期间服务端继续缓冲,重连后由此补齐 */
export async function refreshPerf() {
  try {
    const h = await api.perfHistory()
    store.perf.samples = h.samples
    store.perf.latest = h.samples[h.samples.length - 1] ?? null
  } catch {
    /* 性能历史失败不致命,WS 推送会继续补点 */
  }
}

/** 兜底轮询：WS 健康时事件推送是主通道，降频到 8s；WS 断开回到 1.5s 保实时。
 *  页面隐藏时跳过刷新（visibilitychange 恢复时立即刷一次）。 */
function schedulePoll() {
  if (pollTimer) clearTimeout(pollTimer)
  pollTimer = setTimeout(async () => {
    if (!document.hidden) await refreshTasks()
    schedulePoll()
  }, wsOk ? 8000 : 1500)
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
    // 性能采样 2s 一拍就地入库,必须 return:落入末尾兜底会每拍触发一次全量任务刷新
    if (ev.type === 'perf') {
      const s = ev as PerfSample
      store.perf.latest = s
      store.perf.samples.push(s)
      if (store.perf.samples.length > 1800) store.perf.samples.shift()
      return
    }
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
    // 统计只在状态切换时刷新（低频）；progress 高频事件只驱动列表刷新
    if (ev.type === 'task_status') refreshStats()
  } catch {
    /* 非 JSON 事件按任务刷新处理 */
  }
  scheduleRefresh()
}

function connectWs() {
  // WS 是事件推送主通道；连接灯由 HTTP 轮询单独判定，避免单通道失败时来回闪
  const ws = new WebSocket(wsUrl())
  ws.onopen = () => {
    wsOk = true
    refreshTasks()
    refreshPerf()
    schedulePoll()
  }
  ws.onmessage = handleWsEvent
  ws.onclose = () => {
    wsOk = false
    schedulePoll()
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
  await Promise.all([refreshTasks(), refreshStats(), refreshPerf()])
  connectWs()
  schedulePoll()
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refreshTasks()
  })
  store.ready = true
}
