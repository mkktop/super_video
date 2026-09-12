<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import {
  NButton,
  NInput,
  NInputNumber,
  NPopover,
  NPopconfirm,
  NProgress,
  NRadioButton,
  NRadioGroup,
  NSelect,
  NSpace,
  NSwitch,
  NTag,
  useMessage,
} from 'naive-ui'
import { api } from '../api'
import { refreshTrt, store } from '../store'
import { fmtBytes } from '../utils'
import { themeMode, type ThemeMode } from '../theme'
import { useAppUpdate } from '../composables/useAppUpdate'
import { useCompareCache } from '../composables/useCompareCache'
import { useOutputSettings } from '../composables/useOutputSettings'
import { useScheduleGate } from '../composables/useScheduleGate'
import { fmtGB, trtSrcText, useTrtComponent } from '../composables/useTrtComponent'

const message = useMessage()
const engine = ref<'auto' | 'cuda' | 'trt' | 'directml' | 'cpu'>('auto')
const precision = ref<'fp16' | 'fp32'>('fp16')
const saving = ref(false)
const appVersion = ref('')
const proxyMode = ref<'auto' | 'direct' | 'custom'>('auto')
const proxyAddr = ref('')
const savingProxy = ref(false)
const perfSampling = ref(true)
const parallelStreams = ref(false)
const notifyTask = ref(true) // 任务完成/失败系统通知
const closeToTray = ref(false) // 关闭按钮=最小化到托盘
const queueDoneAction = ref<'none' | 'notify' | 'shutdown' | 'sleep'>('none') // 队列全部完成后
const savedQueueDone = ref<'none' | 'notify' | 'shutdown' | 'sleep'>('none') // 回滚基准
const srProfiling = ref(false) // 超分性能日志（完成的任务可查看瓶颈分析日志）

// ---- 各设置域（状态+保存动作内聚在 composables，本页只做装配） ----
const {
  queueSchedule, scheduleStart, scheduleEnd, idleMinutes, savingSchedule,
  scheduleOptions, saveSchedule, apply: applySchedule,
} = useScheduleGate()
const {
  outputDir, savingOutDir, nameTemplate, savingNameTpl, outDirShown,
  persistOutDir, pickOutDir, clearOutDir, openOutDir, saveNameTemplate, apply: applyOutput,
} = useOutputSettings()
const {
  cacheStats, clearingCache, stillCount, savingStillCount,
  saveStillCount, loadCacheStats, doClearCache, apply: applyCompare,
} = useCompareCache()
const { trcBusy, trcDownloadBytes, installTrc, uninstallTrc } = useTrtComponent()
const {
  checking, autoCheck, updateChannel, updateSource, updateVersion, updateNotes, readyVersion,
  downloading, downloadPercent, updateMsg, updateTag,
  saveAutoCheck, saveUpdateChannel, saveUpdateSource, checkUpdate, doDownload, doInstall, apply: applyUpdate,
} = useAppUpdate()

const queueDoneOptions = [
  { label: '不做任何事', value: 'none' },
  { label: '系统通知', value: 'notify' },
  { label: '关机（60 秒可取消）', value: 'shutdown' },
  { label: '休眠（60 秒可取消）', value: 'sleep' },
]

// ---- 外观（纯 UI 偏好：localStorage 持久化，不经后端设置） ----
const themeOptions: Array<{ label: string; value: ThemeMode }> = [
  { label: '深色', value: 'dark' },
  { label: '浅色', value: 'light' },
  { label: '跟随系统', value: 'system' },
]
// 全局快捷键对照（命令面板等入口的说明，纯静态展示）
const shortcuts: Array<[string, string]> = [
  ['Ctrl + K', '打开命令面板（搜索页面 / 动作 / 最近任务）'],
  ['1 ~ 6', '切换对比模型（模型对比结果页）'],
  ['[  /  ]', '切换静帧样本（任务对比 / 模型对比）'],
  ['←  /  →', '微调分割线位置（Shift 加大步长）'],
  ['Esc', '返回任务页 / 退出全屏'],
]
// 超分完成后删除源文件（危险项，默认关；删除不进回收站）
const deleteSource = ref(false)
async function saveQueueDone(v: 'none' | 'notify' | 'shutdown' | 'sleep') {
  const r = await api.saveSettings({ queue_done_action: v })
  if (!r.ok) {
    message.error(`保存失败: ${(await r.json()).detail ?? r.status}`)
    queueDoneAction.value = savedQueueDone.value
    return
  }
  savedQueueDone.value = v
  message.success(v === 'none' ? '已关闭' : '已保存，当前队列跑完后生效')
}

async function saveDeleteSource(v: boolean) {
  const r = await api.saveSettings({ delete_source_after_done: v })
  if (!r.ok) {
    message.error(`保存失败: ${(await r.json()).detail ?? r.status}`)
    deleteSource.value = !v
  }
}

const proxyOptions = [
  { label: '跟随系统代理', value: 'auto' },
  { label: '直连（不走代理）', value: 'direct' },
  { label: '自定义代理', value: 'custom' },
]
// 已保存的引擎/精度（脏状态对比用；保存成功后同步）
const savedEngine = ref<'auto' | 'cuda' | 'trt' | 'directml' | 'cpu'>('auto')
const savedPrecision = ref<'fp16' | 'fp32'>('fp16')
const engineDirty = computed(() => engine.value !== savedEngine.value || precision.value !== savedPrecision.value)
/** 后端取值 → 展示名（当前生效值与运行中实况共用） */
const backendText = (b?: string) =>
  b === 'trt' ? 'CUDA + TensorRT' : b === 'cuda' ? 'CUDA' : b === 'cpu' ? 'CPU' : 'DirectML'
const backendLabel = computed(() => backendText(store.engine?.backend))
/** 任务运行中且实况后端 ≠ 设置生效值：提示并排展示两态（下一任务起切换） */
const runningBackend = computed(() => {
  const r = store.engine?.running
  return r && r.backend !== store.engine?.backend ? backendText(r.backend) : ''
})
const backendType = computed(() =>
  store.engine?.backend === 'trt' || store.engine?.backend === 'cuda' ? 'success' : 'default',
)
/** 硬件编码能力汇总（无任何硬编时提示软编兜底） */
const encSummary = computed(() => {
  const h = store.hardware
  if (!h) return ''
  const parts: string[] = []
  if (h.nvenc) parts.push('NVENC H.264')
  if (h.av1_nvenc) parts.push('NVENC AV1')
  if (h.amf) parts.push('AMF')
  if (h.svt_av1) parts.push('SVT-AV1（软件）')
  return parts.length ? parts.join(' · ') : '无（使用软件编码）'
})
/** 硬件解码能力汇总（任务页按所选视频实测后开放对应选项） */
const decSummary = computed(() => {
  const h = store.hardware
  if (!h) return ''
  const parts: string[] = []
  if (h.nvdec) parts.push('NVDEC（NVIDIA）')
  if (h.d3d11va) parts.push('D3D11VA（AMD / Intel）')
  return parts.length ? parts.join(' · ') : '无（使用软件解码）'
})

onMounted(async () => {
  void loadCacheStats()
  const s = (await api.settings().catch(() => {
    message.error('设置读取失败，按默认值展示')
    return {}
  })) as {
    engine?: 'auto' | 'cuda' | 'trt' | 'directml' | 'cpu'
    precision?: 'fp16' | 'fp32'
    download_proxy?: string
    perf_sampling?: boolean
    auto_update_check?: boolean
    update_channel?: 'stable' | 'preview'
    output_dir?: string
    parallel_streams?: boolean
    notify_task_done?: boolean
    close_to_tray?: boolean
    sr_profiling?: boolean
    compare_still_count?: number
    queue_done_action?: 'none' | 'notify' | 'shutdown' | 'sleep'
    queue_schedule?: 'always' | 'window' | 'idle'
    schedule_start?: string
    schedule_end?: string
    idle_minutes?: number
    output_name_template?: string
    delete_source_after_done?: boolean
  }
  engine.value = s.engine ?? 'auto'
  precision.value = s.precision ?? 'fp16'
  parallelStreams.value = s.parallel_streams === true
  perfSampling.value = s.perf_sampling !== false
  applySchedule(s)
  applyOutput(s)
  applyCompare(s)
  applyUpdate(s)
  notifyTask.value = s.notify_task_done !== false
  closeToTray.value = s.close_to_tray === true
  queueDoneAction.value = s.queue_done_action ?? 'none'
  savedQueueDone.value = queueDoneAction.value
  srProfiling.value = s.sr_profiling === true
  deleteSource.value = s.delete_source_after_done === true
  const p = s.download_proxy ?? ''
  if (p === 'direct') proxyMode.value = 'direct'
  else if (p.startsWith('http')) {
    proxyMode.value = 'custom'
    proxyAddr.value = p
  } else proxyMode.value = 'auto'
  appVersion.value = await window.sv.appVersion()
  savedEngine.value = engine.value
  savedPrecision.value = precision.value
  refreshTrt()
})

async function saveProxy() {
  const v = proxyMode.value === 'direct' ? 'direct'
    : proxyMode.value === 'custom' ? proxyAddr.value.trim() : ''
  if (proxyMode.value === 'custom' && !/^https?:\/\//.test(v)) {
    message.error('代理地址需以 http:// 或 https:// 开头')
    return
  }
  savingProxy.value = true
  const r = await api.saveSettings({ download_proxy: v })
  savingProxy.value = false
  if (r.ok) {
    message.success('已保存，新的模型下载立即生效')
  } else {
    message.error(`保存失败: ${(await r.json()).detail ?? r.status}`)
  }
}

async function savePerfSampling(v: boolean) {
  const r = await api.saveSettings({ perf_sampling: v })
  if (!r.ok) {
    message.error(`保存失败: ${(await r.json()).detail ?? r.status}`)
    perfSampling.value = !v
  }
}

async function saveNotifyTask(v: boolean) {
  const r = await api.saveSettings({ notify_task_done: v })
  if (!r.ok) {
    message.error(`保存失败: ${(await r.json()).detail ?? r.status}`)
    notifyTask.value = !v
    return
  }
  store.settings = { ...store.settings, notify_task_done: v }
}

async function saveCloseToTray(v: boolean) {
  const r = await api.saveSettings({ close_to_tray: v })
  if (!r.ok) {
    message.error(`保存失败: ${(await r.json()).detail ?? r.status}`)
    closeToTray.value = !v
    return
  }
  store.settings = { ...store.settings, close_to_tray: v }
  // 行为主进程执行：开关即时生效（建/撤托盘）
  window.sv.win.setCloseToTray(v)
}

async function saveEngine() {
  saving.value = true
  const r = await api.saveSettings({ engine: engine.value, precision: precision.value })
  if (r.ok) {
    savedEngine.value = engine.value
    savedPrecision.value = precision.value
    message.success('已保存，从下一个任务起生效')
    // 回拉含真探测（会话内首次切 CUDA/TRT 可达数秒）：loading 一直盖到标签刷新，
    // 避免"保存完了当前后端迟迟不动"的观感
    try {
      store.engine = await api.engine()
    } catch {
      message.error('引擎状态刷新失败')
    }
    saving.value = false
  } else {
    saving.value = false
    message.error(`保存失败: ${(await r.json()).detail ?? r.status}`)
  }
}

async function saveParallel(v: boolean) {
  const r = await api.saveSettings({ parallel_streams: v })
  if (!r.ok) {
    message.error(`保存失败: ${(await r.json()).detail ?? r.status}`)
    parallelStreams.value = !v
  } else {
    message.success(v ? '已开启，从下一个任务起生效' : '已关闭')
  }
}

async function saveSrProfiling(v: boolean) {
  const r = await api.saveSettings({ sr_profiling: v })
  if (!r.ok) {
    message.error(`保存失败: ${(await r.json()).detail ?? r.status}`)
    srProfiling.value = !v
  } else {
    message.success(v ? '已开启，从下一个任务起生效' : '已关闭，已有日志仍可在任务卡查看')
  }
}

/* ================= 以下为本次界面重设计新增的纯 UI 层 =================
   只做锚点导航与脏状态标记：不调 API、不改任何保存时机；
   脏状态全部是「当前值 vs 已提交值」的 computed 对比（与引擎卡 engineDirty 同一约定）。 */

// ---- 顶部锚点导航 + scroll-spy ----
const groups = [
  { id: 'general', label: '通用' },
  { id: 'processing', label: '处理' },
  { id: 'output', label: '输出' },
  { id: 'compare', label: '对比' },
  { id: 'system', label: '系统' },
  { id: 'about', label: '关于与设备' },
] as const
type GroupId = (typeof groups)[number]['id']
/** 线性单色图标（stroke=currentColor，随导航项文字变色） */
const navIcons: Record<GroupId, string> = {
  general: 'M4 6h10 M18 6h2 M16 4v4 M4 12h3 M11 12h9 M9 10v4 M4 18h11 M19 18h1 M17 16v4',
  processing: 'M13 2 4 14h6l-1 8 9-12h-6l1-8z',
  output: 'M3 7c0-1.1.9-2 2-2h4l2 2h8c1.1 0 2 .9 2 2v8c0 1.1-.9 2-2 2H5c-1.1 0-2-.9-2-2V7z',
  compare: 'M4 5h16v14H4z M12 5v14',
  system: 'M12 3v10 M8 9l4 4 4-4 M4 17v2c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2v-2',
  about: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z M12 16v-4 M12 8h.01',
}
const activeGroup = ref<GroupId>('general')
const flashGroup = ref('')

function gotoGroup(id: GroupId) {
  activeGroup.value = id
  flashGroup.value = ''
  document.getElementById(`sg-${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  // 置空一帧再赋值：连续点同一项也能重放定位高亮动画
  requestAnimationFrame(() => { flashGroup.value = id })
}

// 吸顶导航高度以上、下 60% 屏以下才算"当前分组"，手动滚动时激活态跟随
let spy: IntersectionObserver | null = null
onMounted(() => {
  spy = new IntersectionObserver((entries) => {
    for (const e of entries) if (e.isIntersecting) activeGroup.value = e.target.id.slice(3) as GroupId
  }, { rootMargin: '-84px 0px -60% 0px' })
  for (const g of groups) {
    const el = document.getElementById(`sg-${g.id}`)
    if (el) spy?.observe(el)
  }
})
onUnmounted(() => spy?.disconnect())

// ---- 手动保存项的脏状态基线（纯 UI：设置装载完成后拍一次，保存发起时拍一次） ----
const settingsLoaded = ref(false)
const savedSched = ref({ mode: 'always', start: '22:00', end: '08:00', idle: 15 })
const savedTpl = ref('')
const savedStillCount = ref(4)
const savedProxy = ref<{ mode: 'auto' | 'direct' | 'custom'; addr: string }>({ mode: 'auto', addr: '' })

function snapSched() {
  savedSched.value = {
    mode: queueSchedule.value,
    start: scheduleStart.value.trim(),
    end: scheduleEnd.value.trim(),
    idle: Math.round(idleMinutes.value || 15),
  }
}
function snapTpl() { savedTpl.value = nameTemplate.value.trim() }
function snapStill() { savedStillCount.value = stillCount.value }
function snapProxy() { savedProxy.value = { mode: proxyMode.value, addr: proxyAddr.value.trim() } }

// appVersion 由 onMounted 末段写入 → 作为"后端设置已装配完成"的信号，此后脏判断才生效
watch(appVersion, () => {
  settingsLoaded.value = true
  snapSched(); snapTpl(); snapStill(); snapProxy()
})
// 保存发起（loading 位翻起）即视为当前值已提交；失败路径各保存函数已各自报错并保留输入
watch(savingSchedule, (busy) => { if (busy) snapSched() })
watch(savingNameTpl, (busy) => { if (busy) snapTpl() })
watch(savingStillCount, (busy) => { if (busy) snapStill() })
watch(savingProxy, (busy) => { if (busy) snapProxy() })

const schedDirty = computed(() => settingsLoaded.value && (
  queueSchedule.value !== savedSched.value.mode
  || scheduleStart.value.trim() !== savedSched.value.start
  || scheduleEnd.value.trim() !== savedSched.value.end
  || Math.round(idleMinutes.value || 15) !== savedSched.value.idle
))
const tplDirty = computed(() => settingsLoaded.value && nameTemplate.value.trim() !== savedTpl.value)
const stillDirty = computed(() => settingsLoaded.value && stillCount.value !== savedStillCount.value)
const proxyDirty = computed(() => settingsLoaded.value && (
  proxyMode.value !== savedProxy.value.mode
  || (proxyMode.value === 'custom' && proxyAddr.value.trim() !== savedProxy.value.addr)
))
</script>

<template>
  <div class="settings-page">
    <div class="page-head">
      <h1>设置</h1>
    </div>

    <div class="settings-body">
      <!-- 吸顶分类导航：点击锚点跳转，滚动时 scroll-spy 跟随高亮 -->
      <nav class="group-nav" aria-label="设置分类">
        <button
          v-for="g in groups"
          :key="g.id"
          class="nav-item"
          :class="{ on: activeGroup === g.id }"
          :aria-current="activeGroup === g.id ? 'location' : undefined"
          @click="gotoGroup(g.id)"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"
               stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path :d="navIcons[g.id]" />
          </svg>
          <span>{{ g.label }}</span>
        </button>
      </nav>

      <!-- ==================== 通用 ==================== -->
      <section id="sg-general" class="settings-group">
        <h2 class="group-title">通用</h2>
        <div class="group-cards">
          <!-- 外观 -->
          <section class="card sv-card">
            <header class="card-head" :class="{ flash: flashGroup === 'general' }" @animationend="flashGroup = ''">
              <div class="card-title">外观</div>
              <div class="card-sub">界面主题（偏好保存在本机，不进设置文件）</div>
            </header>
            <div class="card-body">
              <div class="row switch-row">
                <span class="row-label">主题</span>
                <NRadioGroup :value="themeMode" size="small" @update:value="themeMode = $event as ThemeMode">
                  <NRadioButton v-for="o in themeOptions" :key="o.value" :value="o.value">{{ o.label }}</NRadioButton>
                </NRadioGroup>
              </div>
              <p class="hint">切换立即生效；「跟随系统」会随 Windows 深浅色模式自动切换。也可随时用 Ctrl+K 命令面板快速切换。</p>
            </div>
          </section>

          <!-- 通知与窗口 -->
          <section class="card sv-card">
            <header class="card-head">
              <div class="card-title">通知与窗口</div>
              <div class="card-sub">任务完成提醒与关闭按钮的行为</div>
            </header>
            <div class="card-body">
              <div class="row switch-row">
                <span class="row-text">
                  任务完成系统通知
                  <small>任务完成/失败时弹系统通知并闪烁任务栏图标（仅窗口未聚焦时打扰）；任务栏图标上的进度显示不受此开关影响</small>
                </span>
                <NSwitch v-model:value="notifyTask" size="small" @update:value="saveNotifyTask" />
              </div>
              <div class="row switch-row bordered-top">
                <span class="row-text">
                  关闭时最小化到托盘
                  <small>点关闭按钮或 Alt+F4 时隐藏窗口到系统托盘，任务继续处理、通知照常弹出；从托盘图标菜单可还原窗口或退出应用</small>
                </span>
                <NSwitch v-model:value="closeToTray" size="small" @update:value="saveCloseToTray" />
              </div>
              <div class="row switch-row bordered-top">
                <span class="row-text">
                  队列全部完成后
                  <small>最后一个任务收尾后的自动动作；关机/休眠前有 60 秒反悔窗口（任务页横幅可取消），期间新入队任务会自动撤销。需保持应用运行，配合「关闭到托盘」可后台等完</small>
                </span>
                <NSelect
                  v-model:value="queueDoneAction"
                  :options="queueDoneOptions"
                  size="small"
                  style="width: 190px"
                  @update:value="saveQueueDone"
                />
              </div>
            </div>
          </section>
        </div>
      </section>

      <!-- ==================== 处理 ==================== -->
      <section id="sg-processing" class="settings-group">
        <h2 class="group-title">处理</h2>
        <div class="group-cards">
          <!-- 处理引擎 -->
          <section class="card sv-card">
            <header class="card-head" :class="{ flash: flashGroup === 'processing' }" @animationend="flashGroup = ''">
              <div class="card-title">处理引擎</div>
              <div class="card-sub">推理后端与计算精度，影响画质细节的还原方式；保存后从下一个任务起生效</div>
            </header>
            <div class="card-body">
              <div class="row switch-row">
                <span class="row-label">当前后端</span>
                <span class="backend-status">
                  <NTag size="small" :bordered="false" :type="backendType">{{ backendLabel }}</NTag>
                  <span v-if="store.engine?.detail" class="hint-inline">{{ store.engine.detail }}</span>
                </span>
              </div>
              <p v-if="runningBackend" class="hint">
                当前任务仍使用 {{ runningBackend }}，下一个任务起使用 {{ backendLabel }}
              </p>
              <div class="row stack">
                <span class="row-label label-with-info">
                  推理后端
                  <NPopover trigger="hover" placement="top" :width="360">
                    <template #trigger>
                      <svg class="info-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                           stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                        <path d="M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 16v-4M12 8h.01" />
                      </svg>
                    </template>
                    <div class="pop-text">
                      DirectML 兼容所有显卡；CUDA / TensorRT 仅限 NVIDIA 显卡。TensorRT
                      需安装下方加速组件，未安装时自动回退 DirectML。CPU 为显存/GPU
                      异常时的兜底通道（速度慢）。
                    </div>
                  </NPopover>
                </span>
                <NRadioGroup v-model:value="engine" size="small">
                  <NRadioButton value="auto">自动</NRadioButton>
                  <NRadioButton value="directml">DirectML</NRadioButton>
                  <NRadioButton value="cuda">CUDA</NRadioButton>
                  <NRadioButton value="trt">TensorRT</NRadioButton>
                  <NRadioButton value="cpu">CPU</NRadioButton>
                </NRadioGroup>
              </div>
              <div class="row stack">
                <span class="row-label">计算精度</span>
                <NRadioGroup v-model:value="precision" size="small">
                  <NRadioButton value="fp16">FP16（推荐）</NRadioButton>
                  <NRadioButton value="fp32">FP32</NRadioButton>
                </NRadioGroup>
              </div>
              <p class="hint">FP16 处理速度约提升 1.4~1.7 倍，画质无可感知差异；FP32 供个别模型出现数值异常时使用。</p>
              <div class="row switch-row bordered-top">
                <span class="row-text">
                  双路并行
                  <small>两个进程分段同时处理，提升 GPU 利用率；配合硬件编码器效果最佳。显存占用约增加一倍，低显存设备建议关闭。开启后从下一个任务起生效</small>
                </span>
                <NSwitch v-model:value="parallelStreams" size="small" @update:value="saveParallel" />
              </div>
              <div class="save-row">
                <span v-if="engineDirty" class="dirty"><i class="dirty-dot" aria-hidden="true"></i>有未保存的修改</span>
                <NButton type="primary" size="small" :loading="saving" @click="saveEngine">保存</NButton>
              </div>
            </div>
          </section>

          <!-- TensorRT 加速组件 -->
          <section v-if="store.trt" class="card sv-card">
            <header class="card-head">
              <div class="card-title">TensorRT 加速组件</div>
              <div class="card-sub">可选 · NVIDIA 显卡推理加速（1080p→4K 最高约 2.5 倍）</div>
            </header>
            <div class="card-body">
              <!-- 安装中：进度 -->
              <template v-if="store.trt.installing">
                <p class="hint" style="margin-bottom: 8px">
                  {{ store.trt.phase === 'download'
                    ? `正在下载 ${store.trt.file}${trtSrcText(store.trt.source) ? ' · ' + trtSrcText(store.trt.source) : ''}（${fmtGB(store.trt.done)} / ${fmtGB(store.trt.total)}）`
                    : `正在解压 ${store.trt.file} …` }}
                </p>
                <NProgress
                  :percentage="store.trt.total ? Math.min(100, Math.round(store.trt.done / store.trt.total * 100)) : 0"
                  :height="6"
                  indicator-placement="inside"
                />
              </template>

              <!-- 已安装：状态 + 卸载 -->
              <template v-else-if="store.trt.installed">
                <div class="row switch-row">
                  <span class="row-text">
                    已安装
                    <small>v{{ store.trt.version }} · onnxruntime-gpu {{ store.trt.ort }} · TensorRT {{ store.trt.trt }} · 占用 {{ fmtGB(store.trt.size_bytes) }}</small>
                  </span>
                  <NButton size="small" :loading="trcBusy" @click="uninstallTrc">卸载</NButton>
                </div>
                <p class="hint">
                  配合上方推理后端选「TensorRT」使用；卸载后自动回退 DirectML。
                  若提示文件被占用，退出应用后重试。
                </p>
              </template>

              <!-- 未安装：安装入口 -->
              <template v-else>
                <p class="hint hint-with-info">
                  适用于 NVIDIA 显卡（2018 年及之后架构）；未安装时推理使用 DirectML，功能不受影响。
                  <NPopover trigger="hover" placement="top" :width="340">
                    <template #trigger>
                      <svg class="info-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                           stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                        <path d="M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 16v-4M12 8h.01" />
                      </svg>
                    </template>
                    <div class="pop-text">安装需从 GitHub 下载约 1.5 GB 运行库，耗时取决于网络环境。</div>
                  </NPopover>
                </p>
                <div class="row switch-row">
                  <span class="row-text" v-if="store.trt.error" style="color: var(--sv-danger-strong)">上次安装失败：{{ store.trt.error }}</span>
                  <span class="row-text" v-else>检测到显卡架构：{{ store.trt.gpu_arch ?? '未知（将下载通用包）' }}</span>
                  <NButton size="small" type="primary" :loading="trcBusy" @click="installTrc">
                    {{ store.trt.error ? '重试安装' : `下载并安装（约 ${fmtGB(trcDownloadBytes)}）` }}
                  </NButton>
                </div>
              </template>
            </div>
          </section>

          <!-- 处理时机 -->
          <section class="card sv-card">
            <header class="card-head">
              <div class="card-title">处理时机</div>
              <div class="card-sub">队列什么时候开始处理下一个任务——白天不抢机器，夜间/空闲自动跑</div>
            </header>
            <div class="card-body">
              <div class="row stack">
                <span class="row-label">领取时机</span>
                <NRadioGroup v-model:value="queueSchedule" size="small">
                  <NRadioButton value="always">立即处理</NRadioButton>
                  <NRadioButton value="window">指定时段</NRadioButton>
                  <NRadioButton value="idle">电脑空闲时</NRadioButton>
                </NRadioGroup>
              </div>
              <div v-if="queueSchedule === 'window'" class="row inline-wrap">
                <span class="row-text">时段</span>
                <NInput v-model:value="scheduleStart" size="small" style="width: 90px" placeholder="22:00" />
                <span class="row-text">至</span>
                <NInput v-model:value="scheduleEnd" size="small" style="width: 90px" placeholder="08:00" />
                <span class="hint-inline">起止跨午夜即夜间段（如 22:00 ~ 08:00）；只在时段内开始新任务</span>
              </div>
              <div v-if="queueSchedule === 'idle'" class="row inline-wrap">
                <span class="row-text">键鼠静置</span>
                <NInputNumber v-model:value="idleMinutes" size="small" :min="1" :max="240" style="width: 110px" />
                <span class="row-text">分钟后开始</span>
              </div>
              <p class="hint">
                只拦截「开始下一个任务」，不会打断进行中的任务（跑完当前任务即停，断点续跑安全）；
                挂起期间任务页会显示等待原因。设置立即生效，无需重启。
              </p>
              <div class="save-row">
                <span v-if="schedDirty" class="dirty"><i class="dirty-dot" aria-hidden="true"></i>有未保存的修改</span>
                <NButton type="primary" size="small" :loading="savingSchedule" @click="saveSchedule">保存</NButton>
              </div>
            </div>
          </section>

          <!-- 性能监控 -->
          <section class="card sv-card">
            <header class="card-head">
              <div class="card-title">性能监控</div>
              <div class="card-sub">「性能」页仪表盘与趋势图的数据来源；超分性能日志从下一个任务起记录</div>
            </header>
            <div class="card-body">
              <div class="row switch-row">
                <span class="row-text">
                  后台性能采样
                  <small>每 2 秒采集 CPU / GPU / 内存占用与任务进程开销；关闭后停止采样、立即生效</small>
                </span>
                <NSwitch v-model:value="perfSampling" size="small" @update:value="savePerfSampling" />
              </div>
              <div class="row switch-row bordered-top">
                <span class="row-text">
                  超分性能日志
                  <small>记录每个任务各阶段的耗时明细（引擎加载 / 解码 / 推理 / 编码 / 等待）与所用配置；开启后完成的任务卡上出现「性能日志」按钮，可用来定位速度瓶颈。不影响任务本身速度</small>
                </span>
                <NSwitch v-model:value="srProfiling" size="small" @update:value="saveSrProfiling" />
              </div>
              <p class="hint">性能采样历史保留最近 1 小时，应用重启后清零；性能日志按任务保留最近 200 份。</p>
            </div>
          </section>
        </div>
      </section>

      <!-- ==================== 输出 ==================== -->
      <section id="sg-output" class="settings-group">
        <h2 class="group-title">输出</h2>
        <div class="group-cards">
          <!-- 输出位置 -->
          <section class="card sv-card">
            <header class="card-head" :class="{ flash: flashGroup === 'output' }" @animationend="flashGroup = ''">
              <div class="card-title">输出位置</div>
              <div class="card-sub">新建超分任务的默认保存目录，剪切导出同样遵循</div>
            </header>
            <div class="card-body">
              <div class="out-path-box">
                <template v-if="outputDir">
                  <span class="out-path" :title="outputDir">{{ outDirShown }}</span>
                  <NButton quaternary size="tiny" @click="openOutDir">打开</NButton>
                </template>
                <span v-else class="out-path empty">未设置 · 保存到源视频同目录</span>
              </div>
              <div class="out-actions">
                <NButton size="small" type="primary" :loading="savingOutDir" @click="pickOutDir">浏览…</NButton>
                <NButton v-if="outputDir" size="small" quaternary :disabled="savingOutDir" @click="clearOutDir">恢复默认</NButton>
              </div>
              <p class="hint">
                目录不存在时会自动创建。输出文件在目录内没有同名时直接沿用原文件名；
                已有同名（含源文件本身）时自动改用「原名_倍率」后缀，不覆盖任何现有文件。
              </p>
              <div class="row switch-row bordered-top">
                <span class="row-text">
                  输出命名模板
                  <small>
                    留空沿用原文件名；支持变量填充，同名冲突仍自动加后缀不覆盖
                    <NPopover trigger="hover" placement="top" :width="380">
                      <template #trigger>
                        <svg class="info-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                             stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                          <path d="M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 16v-4M12 8h.01" />
                        </svg>
                      </template>
                      <div class="pop-text">
                        变量：{name} 原名 · {model} 模型 · {scale} 倍率 · {res} 输出分辨率 · {date} 日期。
                        如 {name}_{model}_{scale}。
                      </div>
                    </NPopover>
                  </small>
                </span>
                <NInput
                  v-model:value="nameTemplate"
                  size="small"
                  placeholder="{name}_{model}_{scale}"
                  style="width: 260px"
                  @keyup.enter="saveNameTemplate"
                />
              </div>
              <div class="row switch-row bordered-top danger-row">
                <span class="row-text">
                  <span class="danger-head">
                    <svg class="danger-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                         stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                      <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0zM12 9v4M12 17h.01" />
                    </svg>
                    超分完成后删除源文件
                  </span>
                  <small>任务成功完成后自动删除该任务的源文件（图片批量=全部输入图），删除不进回收站；失败/取消的任务不删。对比功能依赖源文件，删除后该任务的对比入口会置灰</small>
                </span>
                <NSwitch v-model:value="deleteSource" size="small" @update:value="saveDeleteSource" />
              </div>
              <div v-if="deleteSource" class="warn-strip">
                <svg class="danger-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                     stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0zM12 9v4M12 17h.01" />
                </svg>
                已开启：任务成功后源文件将被永久删除，不进回收站
              </div>
              <div class="save-row">
                <span v-if="tplDirty" class="dirty"><i class="dirty-dot" aria-hidden="true"></i>有未保存的修改</span>
                <NButton type="primary" size="small" :loading="savingNameTpl" @click="saveNameTemplate">保存</NButton>
              </div>
            </div>
          </section>
        </div>
      </section>

      <!-- ==================== 对比 ==================== -->
      <section id="sg-compare" class="settings-group">
        <h2 class="group-title">对比</h2>
        <div class="group-cards">
          <!-- 对比缓存 -->
          <section class="card sv-card">
            <header class="card-head" :class="{ flash: flashGroup === 'compare' }" @animationend="flashGroup = ''">
              <div class="card-title">对比</div>
              <div class="card-sub">静帧样本数设置，以及模型对比切片/成片与任务对比静帧产物的缓存管理（保留在本地且不会自动清理）</div>
            </header>
            <div class="card-body">
              <div class="row switch-row">
                <span class="row-text">
                  静帧样本数
                  <small>静帧对比取几张样本帧（均匀分布、自动避开黑场）；模型对比与任务对比页共用，越多样本越全、产物占用也越大。对已开始的模型对比无影响，任务对比页下次打开时按新数重建</small>
                </span>
                <NInputNumber
                  v-model:value="stillCount"
                  size="small"
                  :min="1"
                  :max="8"
                  :show-button="false"
                  style="width: 84px"
                  @update:value="(v: number | null) => v === null && (stillCount = 4)"
                />
              </div>
              <div class="row switch-row bordered-top danger-row">
                <span class="row-text">
                  <span class="danger-head">
                    <svg class="danger-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                         stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                      <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0zM12 9v4M12 17h.01" />
                    </svg>
                    清理对比缓存
                  </span>
                  <small>已用空间 {{ cacheStats ? fmtBytes(cacheStats.bytes) : '…' }} · 共 {{ cacheStats ? cacheStats.jobs : '…' }} 个作业 · 清理不影响任务输出与剪切文件</small>
                </span>
                <NPopconfirm @positive-click="doClearCache">
                  <template #trigger>
                    <NButton
                      size="small"
                      type="warning"
                      secondary
                      :loading="clearingCache"
                      :disabled="!cacheStats || cacheStats.jobs === 0"
                    >
                      清理
                    </NButton>
                  </template>
                  将删除全部对比切片与成片，删除后不可恢复。确定清理？
                </NPopconfirm>
              </div>
              <p class="hint">
                对比结果只存在内存里，重启后列表清空，但产物文件会一直留在磁盘。
                有对比正在进行时清理会被拒绝，等它结束再试。
              </p>
              <div class="save-row">
                <span v-if="stillDirty" class="dirty"><i class="dirty-dot" aria-hidden="true"></i>有未保存的修改</span>
                <NButton type="primary" size="small" :loading="savingStillCount" @click="saveStillCount">保存</NButton>
              </div>
            </div>
          </section>
        </div>
      </section>

      <!-- ==================== 系统 ==================== -->
      <section id="sg-system" class="settings-group">
        <h2 class="group-title">系统</h2>
        <div class="group-cards">
          <!-- 应用与更新 -->
          <section class="card sv-card">
            <header class="card-head" :class="{ flash: flashGroup === 'system' }" @animationend="flashGroup = ''">
              <div class="card-title">应用与更新</div>
              <div class="card-sub">版本检查与升级安装</div>
            </header>
            <div class="card-body">
              <div class="row switch-row">
                <span class="row-text version-line">
                  当前版本 <b>v{{ appVersion }}</b>
                  <NTag v-if="updateTag" size="small" :bordered="false" :type="updateTag.type">{{ updateTag.text }}</NTag>
                </span>
                <NSpace :size="8">
                  <NPopover trigger="hover" placement="top-end" :disabled="!updateNotes" :width="380" trigger-style="display: inline-flex">
                    <template #trigger>
                      <NButton size="small" :loading="checking" @click="checkUpdate">检查更新</NButton>
                    </template>
                    <div class="update-notes">
                      <div class="update-notes-head">本次更新内容</div>
                      <div class="update-notes-body">{{ updateNotes }}</div>
                    </div>
                  </NPopover>
                  <NButton
                    v-if="updateVersion && !readyVersion"
                    type="primary"
                    size="small"
                    :loading="downloading"
                    :disabled="downloading"
                    @click="doDownload"
                  >
                    下载更新 v{{ updateVersion }}
                  </NButton>
                  <NButton v-if="readyVersion" type="primary" size="small" @click="doInstall">立即重启 v{{ readyVersion }}</NButton>
                </NSpace>
              </div>
              <NProgress v-if="downloading" :percentage="downloadPercent" :height="6" style="margin-top: 10px" />
              <p v-if="updateMsg" class="hint" style="margin-top: 8px">{{ updateMsg }}</p>
              <div class="row switch-row bordered-top">
                <span class="row-text">
                  更新通道
                  <small>预览版更早获得新功能，成熟度可能不如稳定版；切换后立即按新通道检查</small>
                </span>
                <NRadioGroup v-model:value="updateChannel" size="small" @update:value="saveUpdateChannel">
                  <NRadioButton value="stable">稳定版</NRadioButton>
                  <NRadioButton value="preview">预览版</NRadioButton>
                </NRadioGroup>
              </div>
              <div class="row switch-row bordered-top">
                <span class="row-text">
                  更新下载源
                  <small>R2 为自建备用镜像，国内直连通常更快；自动=默认 GitHub、连不上才切 R2；仅 GitHub 不使用备用源</small>
                </span>
                <NRadioGroup v-model:value="updateSource" size="small" @update:value="saveUpdateSource">
                  <NRadioButton value="auto">自动</NRadioButton>
                  <NRadioButton value="r2">R2 优先</NRadioButton>
                  <NRadioButton value="github">仅 GitHub</NRadioButton>
                </NRadioGroup>
              </div>
              <div class="row switch-row bordered-top">
                <span class="row-text">启动时自动检查更新<small>有新版本时在顶栏版本号旁提示</small></span>
                <NSwitch v-model:value="autoCheck" size="small" @update:value="saveAutoCheck" />
              </div>
            </div>
          </section>

          <!-- 模型下载 -->
          <section class="card sv-card">
            <header class="card-head">
              <div class="card-title">模型下载</div>
              <div class="card-sub">模型从 GitHub Releases 获取时的网络通道</div>
            </header>
            <div class="card-body">
              <p class="hint">
                若「跟随系统代理」模式下下载缓慢，可能是代理规则未覆盖 GitHub CDN 域名，
                可切换为「自定义代理」并填写本地代理地址（如 http://127.0.0.1:7890）。
              </p>
              <div class="row inline-wrap">
                <NSelect v-model:value="proxyMode" :options="proxyOptions" size="small" style="width: 170px" />
                <NInput
                  v-if="proxyMode === 'custom'"
                  v-model:value="proxyAddr"
                  size="small"
                  placeholder="http://127.0.0.1:7890"
                  style="width: 230px"
                  @keyup.enter="saveProxy"
                />
              </div>
              <div class="save-row">
                <span v-if="proxyDirty" class="dirty"><i class="dirty-dot" aria-hidden="true"></i>有未保存的修改</span>
                <NButton type="primary" size="small" :loading="savingProxy" @click="saveProxy">保存</NButton>
              </div>
            </div>
          </section>
        </div>
      </section>

      <!-- ==================== 关于与设备 ==================== -->
      <section id="sg-about" class="settings-group">
        <h2 class="group-title">关于与设备</h2>
        <div class="group-cards">
          <!-- 关于：应用版本 + 设备规格 -->
          <section class="card sv-card">
            <header class="card-head" :class="{ flash: flashGroup === 'about' }" @animationend="flashGroup = ''">
              <div class="card-title">关于</div>
              <div class="card-sub">当前版本与硬件规格——决定可选的处理规格与硬件编码能力</div>
            </header>
            <div class="card-body">
              <div class="about-body">
                <div class="about-app">
                  <div class="about-name">super_video</div>
                  <div class="about-ver">
                    <span class="sv-num">v{{ appVersion }}</span>
                    <NTag v-if="updateTag" size="small" :bordered="false" :type="updateTag.type">{{ updateTag.text }}</NTag>
                  </div>
                </div>
                <div class="spec-grid">
                  <span class="k">显卡</span>
                  <span>{{ store.gpuName }}<template v-if="store.hardware?.gpus?.[0]?.vram_gb">（{{ store.hardware.gpus[0].vram_gb }}GB 显存）</template></span>
                  <span class="k">处理器</span>
                  <span>{{ store.hardware?.cpu }} · {{ store.hardware?.cpu_cores }} 核心</span>
                  <span class="k">内存</span>
                  <span>{{ store.hardware?.ram_gb }} GB</span>
                  <span class="k">硬件编码</span>
                  <span>{{ encSummary }}</span>
                  <span class="k">硬件解码</span>
                  <span>{{ decSummary }}</span>
                </div>
              </div>
            </div>
          </section>

          <!-- 快捷键 -->
          <section class="card sv-card">
            <header class="card-head">
              <div class="card-title">快捷键</div>
              <div class="card-sub">全局快捷键速查</div>
            </header>
            <div class="card-body">
              <div class="sc-grid">
                <template v-for="sc in shortcuts" :key="sc[0]">
                  <span class="sc-key">{{ sc[0] }}</span>
                  <span class="sc-desc">{{ sc[1] }}</span>
                </template>
              </div>
            </div>
          </section>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.settings-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  width: 100%;
  min-width: 560px; /* 窄于此宽度改为横向滚动,不挤压内部控件 */
}
h1 { font-size: 22px; font-weight: 600; letter-spacing: 0.3px; }

/* 单栏内容区：最大 760px 居中，分组垂直排列 */
.settings-body { width: 100%; max-width: 760px; margin: 0 auto; }

/* ---- 吸顶分类导航：胶囊 chips（区别于表单里的分段控件） ---- */
.group-nav {
  position: sticky;
  top: 10px;
  z-index: 20;
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 6px;
  margin-bottom: 18px;
  background: var(--sv-panel);
  border: 1px solid var(--sv-border-soft);
  border-radius: 999px;
  box-shadow: var(--sv-card-inset), var(--sv-shadow-card);
  overflow-x: auto; /* 实在放不下时横向滚动（不换行），并隐藏滚动条 */
  scrollbar-width: none;
}
.group-nav::-webkit-scrollbar { display: none; }
.nav-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 14px;
  border: none;
  border-radius: 999px;
  background: transparent;
  font-size: 13px;
  font-family: inherit;
  color: var(--sv-text-dim);
  cursor: pointer;
  white-space: nowrap;
  flex-shrink: 0;
  transition: background 0.15s var(--sv-ease), color 0.15s var(--sv-ease);
}
.nav-item:hover { background: var(--sv-fill-2); color: var(--sv-text); }
.nav-item.on { background: var(--sv-accent-bg); color: var(--sv-accent); font-weight: 600; }
.nav-item svg { width: 15px; height: 15px; flex-shrink: 0; }

/* ---- 分组：锚点跳转预留吸顶导航高度 ---- */
.settings-group { scroll-margin-top: 78px; }
.settings-group + .settings-group { margin-top: 24px; }
.group-title {
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.08em;
  color: var(--sv-text-faint);
  margin-bottom: 8px;
}
.group-cards { display: flex; flex-direction: column; gap: 16px; }

/* 统一卡片骨架：头部(标题+副题) + 分隔线行式主体（底/描边/圆角由 .sv-card 提供） */
.card {
  overflow: hidden;
}
.card-head {
  padding: 14px 18px 12px;
  border-bottom: 1px solid var(--sv-border-soft);
  background: linear-gradient(180deg, var(--sv-fill-1), transparent);
}
/* 锚点跳转定位：目标分组首卡头部短暂高亮后淡出 */
.card-head.flash { animation: card-flash 1s ease-out; }
@keyframes card-flash {
  from { background: var(--sv-accent-bg); }
  to { background: linear-gradient(180deg, var(--sv-fill-1), transparent); }
}
.card-title { font-size: 14px; font-weight: 650; color: var(--sv-text); }
.card-sub { font-size: 12px; color: var(--sv-text-dim); margin-top: 3px; }
.card-body { padding: 4px 18px 14px; }

/* 行式布局：相邻行以发丝线分隔 */
.row { padding: 12px 0; }
.row.bordered-top { border-top: 1px solid var(--sv-border-soft); margin-top: 4px; }
.row.stack {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 8px;
}
.row.inline-wrap {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
/* 开关/按钮行：文字描述居左，控件贴右 */
.switch-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.row-label { font-weight: 600; font-size: 13px; color: var(--sv-text); }
.label-with-info { display: inline-flex; align-items: center; gap: 6px; }
.row-text { font-weight: 600; font-size: 13px; color: var(--sv-text); }
.row-text small {
  display: block;
  font-weight: 400;
  font-size: 12px;
  color: var(--sv-text-dim);
  margin-top: 3px;
  max-width: 540px; /* 超宽卡片上限宽换行,避免 12px 文字拉满整行难读 */
}
.backend-status {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.hint { color: var(--sv-text-dim); font-size: 12px; margin: 2px 0 6px; line-height: 1.55; max-width: 780px; }
.hint-inline { color: var(--sv-text-dim); font-size: 12px; }
.save-row {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 10px;
  border-top: 1px solid var(--sv-border-soft);
  margin-top: 2px;
  padding-top: 10px;
}

/* 手动保存卡的脏状态：橙色圆点 + 文案 */
.dirty {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--sv-warning);
}
.dirty-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--sv-warning);
  box-shadow: 0 0 6px rgba(var(--sv-warning-rgb), 0.6);
}

/* ⓘ 收纳说明：hover 弹 Popover */
.info-ic {
  width: 14px;
  height: 14px;
  vertical-align: -2px;
  color: var(--sv-text-faint);
  cursor: help;
  transition: color 0.15s ease;
}
.info-ic:hover { color: var(--sv-accent); }
.pop-text { font-size: 12px; line-height: 1.6; color: var(--sv-text-dim); }

/* 危险操作行：警示图标 + 标签用 danger 色 */
.danger-head {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--sv-danger-strong);
}
.danger-ic { width: 14px; height: 14px; flex-shrink: 0; color: var(--sv-danger); }
.warn-strip {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: -4px 0 8px;
  padding: 8px 12px;
  border-radius: 8px;
  background: var(--sv-danger-bg);
  color: var(--sv-danger-strong);
  font-size: 12px;
}

/* 快捷键对照表：键帽 + 说明两列 */
.sc-grid {
  display: grid;
  grid-template-columns: 110px 1fr;
  gap: 8px 16px;
  align-items: baseline;
  padding-top: 10px;
}
.sc-key {
  font-family: Consolas, 'Courier New', monospace;
  font-size: 12px;
  color: var(--sv-text);
  background: var(--sv-fill-2);
  border: 1px solid var(--sv-border-mid);
  border-radius: 5px;
  padding: 2px 8px;
  text-align: center;
  white-space: nowrap;
  justify-self: start;
}
.sc-desc { font-size: 12.5px; color: var(--sv-text-dim); }

/* 输出位置卡 */
.out-path-box {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  background: var(--sv-well);
  border: 1px solid var(--sv-border-soft);
  border-radius: 10px;
  padding: 8px 12px;
  margin-top: 10px;
  min-height: 34px;
}
.out-path {
  font-family: Consolas, 'Courier New', monospace;
  font-size: 12.5px;
  color: var(--sv-text-code);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.out-path.empty { color: var(--sv-text-faint); font-family: inherit; }
.out-actions { display: flex; gap: 8px; margin-top: 10px; }

.version-line { display: inline-flex; align-items: center; gap: 8px; }
.version-line b { font-weight: 700; }
.update-notes-head { font-weight: 600; margin-bottom: 6px; }
.update-notes-body {
  white-space: pre-wrap;
  font-size: 12px;
  line-height: 1.6;
  color: var(--sv-text-dim);
  max-height: 240px;
  overflow-y: auto;
}

/* 关于卡：左应用名/版本，右设备规格（两列标签-值网格） */
.about-body {
  display: flex;
  gap: 16px 32px;
  align-items: flex-start;
  flex-wrap: wrap;
  padding-top: 10px;
}
.about-app { min-width: 170px; }
.about-name { font-size: 16px; font-weight: 700; letter-spacing: 0.2px; }
.about-ver {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 6px;
  font-size: 13px;
  color: var(--sv-text-dim);
}
.spec-grid {
  flex: 1;
  min-width: 280px;
  display: grid;
  grid-template-columns: max-content 1fr max-content 1fr;
  gap: 8px 24px;
  align-items: baseline;
}
.spec-grid > span { display: inline-flex; gap: 10px; align-items: baseline; min-width: 0; }
.spec-grid .k { color: var(--sv-text-dim); font-size: 12.5px; flex-shrink: 0; width: 60px; }
.spec-grid > span:not(.k) { color: var(--sv-text); font-size: 13px; }

/* 响应式：内容区收窄时导航减内距（不换行）；规格表并回单列值对 */
@media (max-width: 700px) {
  .nav-item { padding: 6px 10px; gap: 5px; font-size: 12.5px; }
  .spec-grid { grid-template-columns: max-content 1fr; }
}
</style>
