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
  type TrcStatus,
} from './api'

export const store = reactive({
  ready: false,
  initError: '', // 初始化失败信息（非空=显示错误横幅+重试按钮，不再永久卡 loading）
  connected: false,
  tasks: [] as Task[],
  stats: { total: 0, done: 0, frames: 0, bytes: 0 } as Stats,
  models: [] as ModelInfo[],
  presets: [] as Preset[],
  downloadProgress: {} as Record<string, number>, // model_id -> 0~1
  /** 最近一次模型下载失败（Models 页 watch 弹 toast；null=无）。失败此前被静默吞掉 */
  downloadFailed: null as null | { id: string; msg: string; ts: number },
  gpuName: '',
  engine: null as null | {
    backend: string
    python: string
    detail: string
    /** 任务进行中才有：当前任务实际所用后端（设置热切换下一任务生效，可能与 backend 不同） */
    running?: { backend: string; detail: string }
  },
  trt: null as TrcStatus | null,
  perf: {
    latest: null as PerfSample | null,
    samples: [] as PerfSample[], // 最近 1 小时,与后端环形缓冲同步
  },
  settings: {} as Record<string, unknown>,
  /** 队列完成动作进行中（关机/休眠倒计时）；null=无。endsAt 为本地时间戳 */
  queueAction: null as null | { action: string; endsAt: number },
  update: {
    checked: false, // 启动检查是否已完成(只查一次)
    status: '' as '' | 'dev' | 'available' | 'latest' | 'busy' | 'error',
    version: '',
    current: '',
    notes: '',
    error: '',
    ready: '', // 已下载待安装的版本(空=未下载)
    downloading: false,
    percent: 0,
    downloadError: '',
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
    nvdec?: boolean
    d3d11va?: boolean
  },
})

/** 界面状态：当前页 / 全页对比 / 新建任务页预填输入 */
export const ui = reactive({
  page: 'home' as
    | 'newtask'
    | 'home'
    | 'trim'
    | 'tasks'
    | 'models'
    | 'perf'
    | 'logs'
    | 'settings'
    | 'compare'
    | 'imagesr'
    | 'mcompare',
  compareTaskId: null as string | null,
  pendingInput: null as string | null, // 跳转新建任务页时预填的输入（剪切→超分衔接）
  pendingModel: null as string | null, // 跳转时预选的模型（模型对比→新建任务/图片超分衔接）
  pendingScale: null as number | null,
  pendingCompare: null as { input: string; start_s: number; end_s: number } | null, // 剪切页→模型对比：带区间直达
  pendingTaskParams: null as Task | null, // 失败/取消任务「改参数重试」：带原参数进新建任务页
})

/** 跳转新建任务页并预填输入文件 */
export function openWizardWith(input: string) {
  ui.pendingInput = input
  ui.page = 'newtask'
}

export function openCompare(taskId: string) {
  ui.compareTaskId = taskId
  ui.page = 'compare'
}

let refreshTimer: ReturnType<typeof setTimeout> | null = null
let pollTimer: ReturnType<typeof setTimeout> | null = null
let wsOk = false
// 视为"正在运行"的任务 id 集合：跨过 renderer 重载也不漏掉完成通知
// （启动时从当前任务列表回填，终态事件靠它识别迁移）
const runningIds = new Set<string>()

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
  // 任务栏进度条：有运行中任务按帧数百分比，否则清除（<0）
  const r = store.tasks.find((t) => t.status === 'running')
  window.sv.taskProgress(r && r.total_frames ? Math.min(1, r.progress_frames / r.total_frames) : -1)
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

export async function refreshTrt() {
  try {
    store.trt = await api.trtComponent()
  } catch {
    /* 组件状态失败不致命,设置页进页时再拉 */
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
    // TRT 组件安装进度:就地更新状态,必须 return(同 perf,2s 级高频)
    if (ev.type === 'trt_component') {
      if (store.trt) {
        store.trt = {
          ...store.trt,
          installing: ev.phase === 'download' || ev.phase === 'extract',
          phase: ev.phase ?? null,
          file: ev.file ?? '',
          done: ev.done ?? store.trt.done,
          total: ev.total ?? store.trt.total,
          error: ev.error ?? null,
        }
        if (ev.phase === 'done') {
          // 安装完成:重拉完整状态(版本/体积/资产)与引擎探测结果
          refreshTrt()
          api.engine().then((e) => (store.engine = e))
        }
      }
      return
    }
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
      if (ev.failed) store.downloadFailed = { id, msg: String(ev.failed), ts: Date.now() }
      if (ev.done || ev.failed) {
        const { [id]: _drop, ...rest } = store.downloadProgress
        store.downloadProgress = rest
        refreshModels()
      } else if (typeof ev.progress === 'number') {
        store.downloadProgress = { ...store.downloadProgress, [id]: ev.progress }
      }
      return
    }
    // 处理时机闸门（定时/闲时）：就地更新 stats 内嵌字段，低频事件
    if (ev.type === 'queue_gate') {
      store.stats = { ...store.stats, queue_gate: { active: !!ev.active, reason: String(ev.reason ?? '') } }
      return
    }
    // 队列完成动作（关机/休眠倒计时）：低频、只驱动任务页横幅，直接处理
    if (ev.type === 'queue_done') {
      if (ev.grace_s > 0) {
        store.queueAction = { action: ev.action, endsAt: Date.now() + ev.grace_s * 1000 }
      } else {
        // notify：即时系统通知（复用任务完成通道），无横幅
        window.sv.taskEvent('done', '任务队列已全部完成')
      }
      return
    }
    if (ev.type === 'queue_done_canceled') {
      store.queueAction = null
      return
    }
    if (ev.type === 'queue_done_fired') {
      store.queueAction = null
      return
    }
    // 统计只在状态切换时刷新（低频）；progress 高频事件只驱动列表刷新
    if (ev.type === 'task_status') {
      refreshStats()
      // 任务落终态：设置页「当前任务仍使用 X」提示需退场（仅在有 running 标记时重拉）
      if (ev.status !== 'running' && store.engine?.running) {
        api.engine().then((e) => (store.engine = e))
      }
      // running→终态迁移 → 系统通知+任务栏闪烁（主进程判定窗口焦点）。
      // 事件先于列表刷新到达，此时 store 里还是旧态：任务名从列表取得到。
      if (typeof ev.task_id === 'string') {
        if (ev.status === 'running') runningIds.add(ev.task_id)
        else if ((ev.status === 'done' || ev.status === 'failed') && runningIds.delete(ev.task_id)) {
          const name =
            store.tasks.find((t) => t.id === ev.task_id)?.input_path.split(/[\\/]/).pop() ?? ''
          // 设置「任务完成通知」关闭时只静默更新列表（任务栏进度条不受影响）
          if (store.settings.notify_task_done !== false) window.sv.taskEvent(ev.status, name)
        }
      }
    }
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
    refreshTrt()
    // 断线间隙可能错过 model_download done 事件，进度条残留卡住：
    // 清空后靠 refreshModels 纠正完成态，下载中的条目由下一个进度事件补回
    store.downloadProgress = {}
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

/** 应用更新检查:启动一次 + 设置页手动触发,结果共享给顶栏提示与设置页 */
export async function checkAppUpdate() {
  try {
    const r = await window.sv.checkUpdate()
    store.update.status = r.status as typeof store.update.status
    store.update.version = r.version ?? ''
    store.update.current = r.current ?? ''
    store.update.notes = r.notes ?? ''
    store.update.error = r.error ?? ''
  } catch (e) {
    store.update.status = 'error'
    store.update.error = String(e)
  } finally {
    store.update.checked = true
  }
}

/** 更新下载事件 → 全局 store：监听随 initStore 注册且不注销，
 *  页面切换/组件重建不丢状态；update-ready 只广播一次，重进设置页靠 store 还原。 */
function watchUpdateEvents() {
  window.sv.onUpdateProgress((pct) => {
    store.update.percent = pct
  })
  window.sv.onUpdateReady((v) => {
    store.update.ready = v
    store.update.downloading = false
    store.update.percent = 100
  })
  // renderer 重载兜底：向主进程查询当前下载状态
  void window.sv.updateState().then((s) => {
    if (s.ready) store.update.ready = s.ready
    if (s.downloading) store.update.downloading = true
  })
}

export async function initStore() {
  // 关键四连（models/presets/hardware/engine）任一失败都要落地为可重试的错误，
  // 不能让 ready 永远 false——那会永久卡在"正在连接后端服务…"且无任何提示
  try {
    await initBase()
    store.models = await api.models()
    store.presets = await api.presets()
    store.hardware = (await api.hardware()) as NonNullable<typeof store.hardware>
    store.gpuName = store.hardware.gpus?.[0]?.name ?? '未知显卡'
    store.engine = await api.engine()
    try {
      store.settings = await api.settings()
    } catch {
      /* 设置读取失败按默认(自动检查开) */
    }
    // 「关闭到托盘」行为由主进程执行：读到设置后同步过去（托盘随之建立）
    window.sv.win.setCloseToTray(store.settings.close_to_tray === true)
    // 更新通道同样由主进程消费：须在下方启动自动检查之前同步，否则首查用错通道
    window.sv.setUpdateChannel(store.settings.update_channel === 'preview' ? 'preview' : 'stable')
    await Promise.all([refreshTasks(), refreshStats(), refreshPerf(), refreshTrt()])
    // renderer 中途重启：把已 running 的任务补进通知追踪集合
    for (const t of store.tasks) if (t.status === 'running') runningIds.add(t.id)
    store.initError = ''
    store.ready = true
    connectWs()
    schedulePoll()
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) refreshTasks()
    })
    // 启动只检查一次更新,可在设置中关闭;异步进行不阻塞界面就绪
    if (store.settings.auto_update_check !== false) void checkAppUpdate()
    watchUpdateEvents()
  } catch (e) {
    store.initError = String(e)
  }
}

/** 初始化失败后的重试（错误横幅按钮触发） */
export async function retryInit() {
  if (store.ready) return
  store.initError = ''
  await initStore()
}
