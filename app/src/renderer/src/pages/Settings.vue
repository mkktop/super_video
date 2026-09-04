<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
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
  checking, autoCheck, updateChannel, updateVersion, updateNotes, readyVersion,
  downloading, downloadPercent, updateMsg, updateTag,
  saveAutoCheck, saveUpdateChannel, checkUpdate, doDownload, doInstall, apply: applyUpdate,
} = useAppUpdate()

const queueDoneOptions = [
  { label: '不做任何事', value: 'none' },
  { label: '系统通知', value: 'notify' },
  { label: '关机（60 秒可取消）', value: 'shutdown' },
  { label: '休眠（60 秒可取消）', value: 'sleep' },
]
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
</script>

<template>
  <div class="settings-page">
    <div class="page-head">
      <h1>设置</h1>
    </div>

    <!-- 固定两列按语义分组、按高度配平（左≈右），宽窗口卡片变宽而不是多挤出几列窄条 -->
    <div class="settings-cols">
      <!-- 左列：处理与队列——引擎 / TensorRT / 队列自动化（通知·关机）/ 领取时机 -->
      <div class="col">
        <!-- 处理引擎 -->
        <section class="card">
          <header class="card-head">
            <div class="card-title">处理引擎</div>
            <div class="card-sub">推理后端与计算精度，影响画质细节的还原方式</div>
          </header>
          <div class="card-body">
            <div class="row">
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
              <span class="row-label">推理后端</span>
              <NRadioGroup v-model:value="engine" size="small">
                <NRadioButton value="auto">自动</NRadioButton>
                <NRadioButton value="directml">DirectML</NRadioButton>
                <NRadioButton value="cuda">CUDA</NRadioButton>
                <NRadioButton value="trt">TensorRT</NRadioButton>
                <NRadioButton value="cpu">CPU</NRadioButton>
              </NRadioGroup>
            </div>
            <p class="hint">DirectML 兼容所有显卡；CUDA / TensorRT 仅限 NVIDIA 显卡。TensorRT 需安装下方加速组件，未安装时自动回退 DirectML。CPU 为显存/GPU 异常时的兜底通道（速度慢）。</p>
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
                <small>两个进程分段同时处理，提升 GPU 利用率；配合硬件编码器效果最佳。显存占用约增加一倍，低显存设备建议关闭</small>
              </span>
              <NSwitch v-model:value="parallelStreams" size="small" @update:value="saveParallel" />
            </div>
            <div class="save-row">
              <span v-if="engineDirty" class="hint-inline">有未保存的修改，保存后从下一个任务起生效</span>
              <NButton type="primary" size="small" :loading="saving" @click="saveEngine">保存</NButton>
            </div>
          </div>
        </section>

        <!-- TensorRT 加速组件 -->
        <section v-if="store.trt" class="card">
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
              <p class="hint">
                适用于 NVIDIA 显卡（2018 年及之后架构）。安装需从 GitHub 下载约 1.5 GB
                运行库，耗时取决于网络环境；未安装时推理使用 DirectML，功能不受影响。
              </p>
              <div class="row switch-row">
                <span class="row-text" v-if="store.trt.error" style="color: #e88080">上次安装失败：{{ store.trt.error }}</span>
                <span class="row-text" v-else>检测到显卡架构：{{ store.trt.gpu_arch ?? '未知（将下载通用包）' }}</span>
                <NButton size="small" type="primary" :loading="trcBusy" @click="installTrc">
                  {{ store.trt.error ? '重试安装' : `下载并安装（约 ${fmtGB(trcDownloadBytes)}）` }}
                </NButton>
              </div>
            </template>
          </div>
        </section>

        <!-- 通知与窗口 -->
        <section class="card">
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

        <!-- 处理时机 -->
        <section class="card">
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
              <NButton type="primary" size="small" :loading="savingSchedule" @click="saveSchedule">保存</NButton>
            </div>
          </div>
        </section>
      </div>

      <!-- 右列：输出与应用——输出位置 / 对比 / 性能采样 / 更新 / 模型下载网络 -->
      <div class="col">
        <!-- 输出位置 -->
        <section class="card">
          <header class="card-head">
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
                <small>留空沿用原文件名；变量：{name} 原名 · {model} 模型 · {scale} 倍率 · {res} 输出分辨率 · {date} 日期。如 {name}_{model}_{scale}；同名冲突仍自动加后缀不覆盖</small>
              </span>
              <NSpace :size="8" :wrap="false" align="center">
                <NInput
                  v-model:value="nameTemplate"
                  size="small"
                  placeholder="{name}_{model}_{scale}"
                  style="width: 260px"
                  @keyup.enter="saveNameTemplate"
                />
                <NButton size="small" :loading="savingNameTpl" @click="saveNameTemplate">保存</NButton>
              </NSpace>
            </div>
          </div>
        </section>

        <!-- 对比缓存 -->
        <section class="card">
          <header class="card-head">
            <div class="card-title">对比</div>
            <div class="card-sub">静帧样本数设置，以及模型对比切片/成片与任务对比静帧产物的缓存管理（保留在本地且不会自动清理）</div>
          </header>
          <div class="card-body">
            <div class="row switch-row">
              <span class="row-text">
                静帧样本数
                <small>静帧对比取几张样本帧（均匀分布、自动避开黑场）；模型对比与任务对比页共用，越多样本越全、产物占用也越大。对已开始的模型对比无影响，任务对比页下次打开时按新数重建</small>
              </span>
              <NSpace :size="8" :wrap="false" align="center">
                <NInputNumber
                  v-model:value="stillCount"
                  size="small"
                  :min="1"
                  :max="8"
                  :show-button="false"
                  style="width: 84px"
                  @update:value="(v: number | null) => v === null && (stillCount = 4)"
                />
                <NButton size="small" type="primary" :loading="savingStillCount" @click="saveStillCount">保存</NButton>
              </NSpace>
            </div>
            <div class="row switch-row bordered-top">
              <span class="row-text">
                已用空间 <b>{{ cacheStats ? fmtBytes(cacheStats.bytes) : '…' }}</b>
                <small>共 {{ cacheStats ? cacheStats.jobs : '…' }} 个作业 · 清理不影响任务输出与剪切文件</small>
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
                    清理对比缓存
                  </NButton>
                </template>
                将删除全部对比切片与成片，删除后不可恢复。确定清理？
              </NPopconfirm>
            </div>
            <p class="hint">
              对比结果只存在内存里，重启后列表清空，但产物文件会一直留在磁盘。
              有对比正在进行时清理会被拒绝，等它结束再试。
            </p>
          </div>
        </section>

        <!-- 应用与更新 -->
        <section class="card">
          <header class="card-head">
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
              <span class="row-text">启动时自动检查更新<small>有新版本时在顶栏版本号旁提示</small></span>
              <NSwitch v-model:value="autoCheck" size="small" @update:value="saveAutoCheck" />
            </div>
          </div>
        </section>

        <!-- 性能监控 -->
        <section class="card">
          <header class="card-head">
            <div class="card-title">性能监控</div>
            <div class="card-sub">「性能」页仪表盘与趋势图的数据来源</div>
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

        <!-- 模型下载 -->
        <section class="card">
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
              />
              <NButton size="small" type="primary" :loading="savingProxy" @click="saveProxy">保存</NButton>
            </div>
          </div>
        </section>
      </div>
    </div>

    <!-- 设备信息：通栏规格条沉底 -->
    <section class="card">
      <header class="card-head">
        <div class="card-title">设备信息</div>
        <div class="card-sub">决定可选的处理规格与硬件编码能力</div>
      </header>
      <div class="card-body">
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
    </section>
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
h1 { font-size: 20px; font-weight: 700; }

/* 两列按语义分组 + 高度配平（左≈右，全屏下底部对齐）：
   宽屏只把卡片撑宽,永远不挤第三列；窄窗口 auto-fit 回落单列,两列各自整列下移 */
.settings-cols {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(430px, 1fr));
  gap: 16px;
  align-items: start;
}
.col { display: flex; flex-direction: column; gap: 16px; min-width: 0; }

/* 统一卡片骨架：头部(标题+副题) + 分隔线行式主体 */
.card {
  background: #1a1c1f;
  border: 1px solid #26292e;
  border-radius: 12px;
  overflow: hidden;
}
.card-head {
  padding: 14px 18px 12px;
  border-bottom: 1px solid #23262b;
  background: linear-gradient(180deg, rgba(255, 255, 255, 0.02), transparent);
}
.card-title { font-size: 14px; font-weight: 650; color: #e8eaed; }
.card-sub { font-size: 12px; color: #9aa0a6; margin-top: 3px; }
.card-body { padding: 4px 18px 14px; }

/* 行式布局：相邻行以发丝线分隔 */
.row { padding: 12px 0; }
.row.bordered-top { border-top: 1px solid #212429; margin-top: 4px; }
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
.row-label { font-weight: 600; font-size: 13px; color: #e8eaed; }
.row-text { font-weight: 600; font-size: 13px; color: #e8eaed; }
.row-text small {
  display: block;
  font-weight: 400;
  font-size: 12px;
  color: #9aa0a6;
  margin-top: 3px;
  max-width: 540px; /* 超宽卡片上限宽换行,避免 12px 文字拉满整行难读 */
}
.backend-status {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.hint { color: #9aa0a6; font-size: 12px; margin: 2px 0 6px; line-height: 1.55; max-width: 780px; }
.hint-inline { color: #9aa0a6; font-size: 12px; }
.save-row {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 10px;
  border-top: 1px solid #212429;
  margin-top: 2px;
  padding-top: 10px;
}

/* 输出位置卡 */
.out-path-box {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  background: #141517;
  border: 1px solid #26292e;
  border-radius: 8px;
  padding: 8px 12px;
  margin-top: 10px;
  min-height: 34px;
}
.out-path {
  font-family: Consolas, 'Courier New', monospace;
  font-size: 12.5px;
  color: #c9cdd4;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.out-path.empty { color: #7c838c; font-family: inherit; }
.out-actions { display: flex; gap: 8px; margin-top: 10px; }

.version-line { display: inline-flex; align-items: center; gap: 8px; }
.version-line b { font-weight: 700; }
.update-notes-head { font-weight: 600; margin-bottom: 6px; }
.update-notes-body {
  white-space: pre-wrap;
  font-size: 12px;
  line-height: 1.6;
  color: #c9cdd4;
  max-height: 240px;
  overflow-y: auto;
}

/* 设备信息：标签-值两列网格；通栏时四项两行铺满，窄处自动合并单列 */
.spec-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 10px 32px;
  padding-top: 10px;
  align-items: baseline;
}
.spec-grid > span { display: inline-flex; gap: 10px; align-items: baseline; min-width: 0; }
.spec-grid .k { color: #9aa0a6; font-size: 12.5px; flex-shrink: 0; width: 60px; }
.spec-grid > span:nth-child(even) { color: #e8eaed; font-size: 13px; }
</style>
