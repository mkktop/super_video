<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  NButton,
  NCard,
  NInput,
  NPopover,
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
import { checkAppUpdate, refreshModels, refreshTrt, store } from '../store'

const message = useMessage()
const engine = ref<'auto' | 'cuda' | 'trt' | 'directml'>('auto')
const precision = ref<'fp16' | 'fp32'>('fp16')
const saving = ref(false)
const appVersion = ref('')
const checking = ref(false)
const proxyMode = ref<'auto' | 'direct' | 'custom'>('auto')
const proxyAddr = ref('')
const savingProxy = ref(false)
const perfSampling = ref(true)
const autoCheck = ref(true)
const trcBusy = ref(false)
const parallelStreams = ref(false)
// 已保存的引擎/精度（脏状态对比用；保存成功后同步）
const savedEngine = ref<'auto' | 'cuda' | 'trt' | 'directml'>('auto')
const savedPrecision = ref<'fp16' | 'fp32'>('fp16')
const proxyOptions = [
  { label: '跟随系统代理', value: 'auto' },
  { label: '直连（不走代理）', value: 'direct' },
  { label: '自定义代理', value: 'custom' },
]
// 更新状态全部从全局 store 派生：事件监听在 store 层注册，
// 切页/重进设置页不丢"已下载待安装"与下载进度
const updateVersion = computed(() => store.update.version)
const updateNotes = computed(() => store.update.notes)
const readyVersion = computed(() => store.update.ready)
const downloading = computed(() => store.update.downloading)
const downloadPercent = computed(() => store.update.percent)
const updateMsg = computed(() => {
  const u = store.update
  if (u.ready) return `新版本 v${u.ready} 已下载完成，点击"立即重启"生效`
  if (u.downloading) return '正在下载更新…（下载完成后可点击"立即重启"）'
  if (u.downloadError) return `下载失败：${u.downloadError}`
  if (!u.checked) return ''
  if (u.status === 'dev') return '开发模式不检查更新（打包版自动检查 GitHub Releases）'
  if (u.status === 'available')
    return `发现新版本 v${u.version}，点击"下载更新"获取（全量安装包）${
      u.notes ? '（悬浮在"检查更新"上可查看更新内容）' : ''
    }`
  if (u.status === 'latest') return `已是最新版本（${u.current}）`
  if (u.status === 'error') return `检查失败：${u.error ?? '未知错误'}（发布前属正常，见 README 发布流程）`
  return ''
})
const engineDirty = computed(() => engine.value !== savedEngine.value || precision.value !== savedPrecision.value)
const backendLabel = computed(() => {
  const b = store.engine?.backend
  if (b === 'trt') return 'CUDA + TensorRT'
  if (b === 'cuda') return 'CUDA'
  return 'DirectML'
})
const backendType = computed(() =>
  store.engine?.backend === 'trt' || store.engine?.backend === 'cuda' ? 'success' : 'default',
)
/** 更新状态标签：挂在版本行右侧,一眼可辨 */
const updateTag = computed(() => {
  if (readyVersion.value) return { text: `v${readyVersion.value} 已就绪`, type: 'success' as const }
  if (downloading.value) return { text: '下载中', type: 'info' as const }
  const u = store.update
  if (u.status === 'available') return { text: `可更新 v${u.version}`, type: 'warning' as const }
  if (u.status === 'latest' && u.checked) return { text: '已是最新', type: 'success' as const }
  return null
})
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

onMounted(async () => {
  const s = (await api.settings()) as {
    engine?: 'auto' | 'cuda' | 'trt' | 'directml'
    precision?: 'fp16' | 'fp32'
    download_proxy?: string
    perf_sampling?: boolean
    auto_update_check?: boolean
  }
  engine.value = s.engine ?? 'auto'
  precision.value = s.precision ?? 'fp16'
  parallelStreams.value = s.parallel_streams === true
  perfSampling.value = s.perf_sampling !== false
  autoCheck.value = s.auto_update_check !== false
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

async function saveAutoCheck(v: boolean) {
  const r = await api.saveSettings({ auto_update_check: v })
  if (!r.ok) {
    message.error(`保存失败: ${(await r.json()).detail ?? r.status}`)
    autoCheck.value = !v
  }
}

async function checkUpdate() {
  checking.value = true
  try {
    await checkAppUpdate() // 结果进 store.update,本页文案由 computed 派生
  } finally {
    checking.value = false
  }
}

async function doDownload() {
  store.update.downloading = true
  store.update.percent = 0
  store.update.downloadError = ''
  const r = await window.sv.downloadUpdate()
  if (!r.ok) {
    store.update.downloading = false
    store.update.downloadError = r.error ?? '未知错误'
  }
  // 成功时 update-ready 事件会把 downloading 置 false 并写入 ready
}

function doInstall() {
  void window.sv.installUpdate() // 静默安装后自动拉起新版本
}

async function saveEngine() {
  saving.value = true
  const r = await api.saveSettings({ engine: engine.value, precision: precision.value })
  saving.value = false
  if (r.ok) {
    savedEngine.value = engine.value
    savedPrecision.value = precision.value
    message.success('已保存，从下一个任务起生效')
    store.engine = await api.engine()
  } else {
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

// ---- TRT 可选组件 ----

function fmtGB(b: number): string {
  return (b / 1e9).toFixed(2) + ' GB'
}

/** 安装需下载的体积（core + 匹配架构的 builder 包） */
const trcDownloadBytes = computed(() => {
  const t = store.trt
  if (!t || t.installed || t.installing) return 0
  const a = t.assets?.assets ?? {}
  const core = a.core?.size ?? 0
  const key = t.gpu_arch && a[`builder-${t.gpu_arch}`] ? `builder-${t.gpu_arch}` : 'builder-ptx'
  return core + (a[key]?.size ?? 0)
})

async function installTrc() {
  trcBusy.value = true
  const r = await api.installTrtComponent()
  if (!r.ok) message.error(`无法开始安装: ${(await r.json()).detail ?? r.status}`)
  // 成功则进度走 WS trt_component 事件;失败恢复按钮
  trcBusy.value = r.ok
}

async function uninstallTrc() {
  trcBusy.value = true
  const r = await api.uninstallTrtComponent()
  trcBusy.value = false
  if (r.ok) {
    message.success('已卸载，推理回退 DirectML')
    await refreshTrt()
  } else {
    message.error(`${(await r.json()).detail ?? r.status}`)
  }
}
</script>

<template>
  <div class="settings-page">
    <div class="page-head">
      <h1>设置</h1>
    </div>

    <div class="settings-grid">
    <NCard title="推理后端与精度" size="small">
      <div class="backend-status">
        <span class="field-label">当前后端</span>
        <NTag size="small" :bordered="false" :type="backendType">{{ backendLabel }}</NTag>
        <span v-if="store.engine?.detail" class="hint-inline">{{ store.engine.detail }}</span>
      </div>

      <div class="field">
        <div class="field-label">推理后端</div>
        <NRadioGroup v-model:value="engine">
          <NRadioButton value="auto">自动</NRadioButton>
          <NRadioButton value="directml">DirectML</NRadioButton>
          <NRadioButton value="cuda">CUDA</NRadioButton>
          <NRadioButton value="trt">TensorRT</NRadioButton>
        </NRadioGroup>
        <p class="hint" style="margin-top: 8px">
          DirectML 兼容所有显卡；CUDA / TensorRT 仅限 NVIDIA 显卡。
          TensorRT 需安装下方加速组件，未安装时自动回退 DirectML。
        </p>
      </div>

      <div class="field">
        <div class="field-label">计算精度</div>
        <NRadioGroup v-model:value="precision">
          <NRadioButton value="fp16">FP16（推荐）</NRadioButton>
          <NRadioButton value="fp32">FP32</NRadioButton>
        </NRadioGroup>
        <p class="hint" style="margin-top: 8px">
          FP16 处理速度约提升 1.4~1.7 倍，画质无可感知差异；FP32 供个别模型出现数值异常时使用
        </p>
      </div>

      <div class="update-row" style="margin-top: 2px">
        <span>双路并行：两个进程分段同时处理，提升 GPU 利用率；配合硬件编码器效果最佳，使用软编码时提升有限。显存占用约增加一倍，低显存设备建议关闭</span>
        <NSwitch v-model:value="parallelStreams" size="small" @update:value="saveParallel" />
      </div>

      <div class="save-row">
        <span v-if="engineDirty" class="hint-inline">有未保存的修改，保存后从下一个任务起生效</span>
        <NButton type="primary" size="small" :loading="saving" @click="saveEngine">保存</NButton>
      </div>
    </NCard>

    <NCard v-if="store.trt" title="TensorRT 加速组件（可选）" size="small">
      <!-- 安装中：进度 -->
      <template v-if="store.trt.installing">
        <p class="hint" style="margin-bottom: 8px">
          {{ store.trt.phase === 'download'
            ? `正在下载 ${store.trt.file}（${fmtGB(store.trt.done)} / ${fmtGB(store.trt.total)}）`
            : `正在解压 ${store.trt.file} …` }}
        </p>
        <NProgress
          :percentage="store.trt.total ? Math.min(100, Math.round(store.trt.done / store.trt.total * 100)) : 0"
          :height="6"
          :indicator-placement="'inside'"
        />
      </template>

      <!-- 已安装：状态 + 卸载 -->
      <template v-else-if="store.trt.installed">
        <div class="update-row">
          <span>
            已安装（v{{ store.trt.version }} · onnxruntime-gpu {{ store.trt.ort }} ·
            TensorRT {{ store.trt.trt }} · 占用 {{ fmtGB(store.trt.size_bytes) }}）
          </span>
          <NButton size="small" :loading="trcBusy" @click="uninstallTrc">卸载</NButton>
        </div>
        <p class="hint" style="margin-top: 8px">
          配合上方推理后端选「TensorRT」使用；卸载后自动回退 DirectML。
          若提示文件被占用，退出应用后重试。
        </p>
      </template>

      <!-- 未安装：安装入口 -->
      <template v-else>
        <p class="hint">
          适用于 NVIDIA 显卡（2018 年及之后架构）的推理加速组件，1080p→4K
          处理速度最高可提升约 2.5 倍。安装需从 GitHub 下载约 1.5 GB
          运行库，耗时取决于网络环境；未安装时推理使用 DirectML，功能不受影响。
        </p>
        <div class="update-row" v-if="store.trt.error">
          <span style="color: #e88080">上次安装失败：{{ store.trt.error }}</span>
          <NButton size="small" type="primary" :loading="trcBusy" @click="installTrc">重试安装</NButton>
        </div>
        <div class="update-row" v-else>
          <span>检测到显卡架构：{{ store.trt.gpu_arch ?? '未知（将下载通用包）' }}</span>
          <NButton size="small" type="primary" :loading="trcBusy" @click="installTrc">下载并安装（约 {{ fmtGB(trcDownloadBytes) }}）</NButton>
        </div>
      </template>
    </NCard>

    <NCard title="模型下载" size="small">
      <p class="hint">
        下载源为 GitHub Releases。若「跟随系统代理」模式下下载缓慢，可能是代理规则
        未覆盖 GitHub CDN 域名，可切换为「自定义代理」并填写本地代理地址
        （如 http://127.0.0.1:7890）。
      </p>
      <NSpace :size="8" align="center" wrap>
        <NSelect v-model:value="proxyMode" :options="proxyOptions" size="small" style="width: 170px" />
        <NInput
          v-if="proxyMode === 'custom'"
          v-model:value="proxyAddr"
          size="small"
          placeholder="http://127.0.0.1:7890"
          style="width: 230px"
        />
        <NButton size="small" type="primary" :loading="savingProxy" @click="saveProxy">保存</NButton>
      </NSpace>
    </NCard>

    <NCard title="性能监控" size="small">
      <div class="update-row">
        <span>后台性能采样：每 2 秒采集 CPU / GPU / 内存占用与任务进程开销，供「性能」页仪表盘与趋势图使用</span>
        <NSwitch v-model:value="perfSampling" size="small" @update:value="savePerfSampling" />
      </div>
      <p class="hint" style="margin-top: 8px">
        关闭后停止采样，重新开启立即生效；历史数据保留最近 1 小时，应用重启后清零。
      </p>
    </NCard>

    <NCard title="应用与更新" size="small">
      <div class="update-row">
        <span class="version-line">
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
      <div class="update-row" style="margin-top: 12px">
        <span>启动时自动检查更新（有新版本时在顶栏版本号旁提示）</span>
        <NSwitch v-model:value="autoCheck" size="small" @update:value="saveAutoCheck" />
      </div>
    </NCard>

    <NCard title="设备信息" size="small">
      <div class="spec-grid">
        <span class="k">显卡</span>
        <span>{{ store.gpuName }}<template v-if="store.hardware?.gpus?.[0]?.vram_gb">（{{ store.hardware.gpus[0].vram_gb }}GB 显存）</template></span>
        <span class="k">处理器</span>
        <span>{{ store.hardware?.cpu }} · {{ store.hardware?.cpu_cores }} 核心</span>
        <span class="k">内存</span>
        <span>{{ store.hardware?.ram_gb }} GB</span>
        <span class="k">硬件编码</span>
        <span>{{ encSummary }}</span>
      </div>
    </NCard>
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
/* 宽窗口两列铺满(推理|TRT 组件 / 下载|监控 / 更新|设备),窄窗口自动回落单列 */
.settings-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(460px, 1fr));
  gap: 16px;
}
h1 { font-size: 20px; font-weight: 700; }
.hint { color: #9aa0a6; font-size: 12.5px; margin-bottom: 12px; }
.update-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap; /* 窄行时控件换行堆到下一行,不与长文字互相挤 */
}
.update-notes-head { font-weight: 600; margin-bottom: 6px; }
.update-notes-body {
  white-space: pre-wrap;
  font-size: 12px;
  line-height: 1.6;
  color: #c9cdd4;
  max-height: 240px;
  overflow-y: auto;
}
/* 后端状态行：标签 + 彩色 Tag + 说明，一眼可辨 */
.backend-status {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}
.field { margin-bottom: 14px; }
.field-label { font-weight: 600; font-size: 13px; margin-bottom: 8px; }
.backend-status .field-label { margin-bottom: 0; }
.hint-inline { color: #9aa0a6; font-size: 12px; }
.save-row {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 10px;
  margin-top: 14px;
}
.version-line { display: inline-flex; align-items: center; gap: 8px; }
/* 设备信息：标签-值两列网格，整齐对齐 */
.spec-grid {
  display: grid;
  grid-template-columns: 76px 1fr;
  row-gap: 8px;
  align-items: baseline;
}
.spec-grid .k { color: #9aa0a6; font-size: 12.5px; }
</style>
