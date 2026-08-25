<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'
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
import { checkAppUpdate, refreshModels, store } from '../store'

const message = useMessage()
const engine = ref<'auto' | 'cuda' | 'trt' | 'directml'>('auto')
const precision = ref<'fp16' | 'fp32'>('fp16')
const saving = ref(false)
const appVersion = ref('')
const checking = ref(false)
const downloading = ref(false)
const downloadPercent = ref(0)
const updateMsg = ref('')
const updateNotes = ref('') // 新版本更新内容（Release 正文），悬浮按钮时展示
const updateVersion = ref('') // 发现的新版本（待下载）
const readyVersion = ref('') // 已下载完成、待重启安装的版本
const proxyMode = ref<'auto' | 'direct' | 'custom'>('auto')
const proxyAddr = ref('')
const savingProxy = ref(false)
const perfSampling = ref(true)
const autoCheck = ref(true)
const proxyOptions = [
  { label: '跟随系统代理', value: 'auto' },
  { label: '直连（不走代理）', value: 'direct' },
  { label: '自定义代理', value: 'custom' },
]
let offProgress: (() => void) | null = null
let offReady: (() => void) | null = null

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
  perfSampling.value = s.perf_sampling !== false
  autoCheck.value = s.auto_update_check !== false
  const p = s.download_proxy ?? ''
  if (p === 'direct') proxyMode.value = 'direct'
  else if (p.startsWith('http')) {
    proxyMode.value = 'custom'
    proxyAddr.value = p
  } else proxyMode.value = 'auto'
  appVersion.value = await window.sv.appVersion()
  offProgress = window.sv.onUpdateProgress((pct) => {
    downloadPercent.value = pct
  })
  offReady = window.sv.onUpdateReady((v) => {
    readyVersion.value = v
    downloading.value = false
    updateMsg.value = `新版本 v${v} 已下载完成，点击"立即重启"生效`
  })
})

/** 更新区显示跟随启动检查结果(启动时只查一次,进设置页不再发起网络检查) */
watch(
  () => store.update,
  (u) => {
    if (!u.checked) return
    if (u.status === 'dev') {
      updateMsg.value = '开发模式不检查更新（打包版自动检查 GitHub Releases）'
    } else if (u.status === 'available') {
      updateVersion.value = u.version
      updateNotes.value = u.notes
      updateMsg.value = `发现新版本 v${u.version}，点击"下载更新"获取（全量安装包）${
        u.notes ? '（悬浮在"检查更新"上可查看更新内容）' : ''
      }`
    } else if (u.status === 'latest') {
      updateVersion.value = ''
      updateMsg.value = `已是最新版本（${u.current}）`
    } else if (u.status === 'error') {
      updateMsg.value = `检查失败：${u.error ?? '未知错误'}（发布前属正常，见 README 发布流程）`
    }
  },
  { deep: true, immediate: true },
)

onUnmounted(() => {
  offProgress?.()
  offReady?.()
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
  updateMsg.value = ''
  updateNotes.value = ''
  try {
    await checkAppUpdate() // 结果进 store.update,由 watcher 刷新本页与顶栏
  } finally {
    checking.value = false
  }
}

async function doDownload() {
  downloading.value = true
  downloadPercent.value = 0
  updateMsg.value = '正在下载更新…（下载完成后可点击"立即重启"）'
  const r = await window.sv.downloadUpdate()
  if (!r.ok) {
    downloading.value = false
    updateMsg.value = `下载失败：${r.error ?? '未知错误'}`
  }
  // 成功时 update-downloaded 事件会把 downloading 置 false 并提示重启
}

function doInstall() {
  void window.sv.installUpdate() // 静默安装后自动拉起新版本
}

async function saveEngine() {
  saving.value = true
  const r = await api.saveSettings({ engine: engine.value, precision: precision.value })
  saving.value = false
  if (r.ok) {
    message.success('已保存，从下一个任务起生效')
    store.engine = await api.engine()
  } else {
    message.error(`保存失败: ${(await r.json()).detail ?? r.status}`)
  }
}
</script>

<template>
  <div class="settings-page">
    <div class="page-head">
      <h1>设置</h1>
    </div>

    <NCard title="推理后端与精度" size="small">
      <p class="hint">
        DirectML：全显卡兼容（实测本机最快）· CUDA：NVIDIA 专用（供 M3 扩散模型）·
        TensorRT：N 卡最快推理（实验性，需 .venv-cuda + tensorrt 组件；缺失自动回退）·
        当前实际后端：<NTag size="small" type="info" :bordered="false">
          {{ store.engine?.backend === 'trt' ? 'CUDA + TensorRT' : store.engine?.backend === 'cuda' ? 'CUDA' : 'DirectML' }}
        </NTag>
      </p>
      <NRadioGroup v-model:value="engine">
        <NRadioButton value="auto">自动</NRadioButton>
        <NRadioButton value="directml">DirectML</NRadioButton>
        <NRadioButton value="cuda">CUDA</NRadioButton>
        <NRadioButton value="trt">TensorRT</NRadioButton>
      </NRadioGroup>
      <p class="hint" style="margin: 14px 0 12px">
        FP16：实测提速 1.36~1.73x，画质无感知差异（输出 PSNR 74dB+）·
        FP32：极少数模型数值异常时回退用
      </p>
      <NRadioGroup v-model:value="precision">
        <NRadioButton value="fp16">FP16（推荐）</NRadioButton>
        <NRadioButton value="fp32">FP32</NRadioButton>
      </NRadioGroup>
      <div style="margin-top: 14px">
        <NButton type="primary" size="small" :loading="saving" @click="saveEngine">保存</NButton>
      </div>
    </NCard>

    <NCard title="模型下载" size="small">
      <p class="hint">
        下载源为 GitHub Releases（models-v1）。代理软件开了"系统代理"但下载仍慢时，
        多半是 PAC 模式不生效或代理规则没覆盖 GitHub CDN 域名——选"自定义代理"填本地
        代理地址（如 Clash 的 http://127.0.0.1:7890）最稳。
      </p>
      <NSpace :size="8" align="center">
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
        关闭后采样停止（笔记本省电可关），重新开启立即生效；历史数据保留最近 1 小时，随重启清零。
      </p>
    </NCard>

    <NCard title="应用与更新" size="small">
      <div class="update-row">
        <span>当前版本 <b>v{{ appVersion }}</b> · 更新源：GitHub Releases</span>
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

    <NCard title="本机环境" size="small">
      <NSpace vertical :size="6">
        <div>GPU：{{ store.gpuName }} <span v-if="store.hardware?.gpus?.[0]?.vram_gb">({{ store.hardware.gpus[0].vram_gb }}GB)</span></div>
        <div>CPU：{{ store.hardware?.cpu }} · {{ store.hardware?.cpu_cores }} 核心</div>
        <div>内存：{{ store.hardware?.ram_gb }} GB</div>
        <div>NVENC 硬编：{{ store.hardware?.nvenc ? '可用' : '不可用' }}<template v-if="store.hardware?.av1_nvenc"> · AV1 硬编：可用</template><template v-if="store.hardware?.amf"> · AMF：可用</template><template v-if="store.hardware?.svt_av1"> · SVT-AV1：可用</template></div>
      </NSpace>
    </NCard>
  </div>
</template>

<style scoped>
.settings-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  width: 100%;
  min-width: 620px; /* 窄于此宽度改为横向滚动,不挤压内部控件 */
  max-width: 860px;
  margin: 0 auto; /* 窗口宽时列居中,窄时随窗口收窄 */
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
</style>
