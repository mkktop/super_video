<script setup lang="ts">
import { onMounted, ref } from 'vue'
import {
  NButton,
  NCard,
  NCode,
  NPopover,
  NRadioButton,
  NRadioGroup,
  NSpace,
  NTag,
  useMessage,
} from 'naive-ui'
import { api } from '../api'
import { refreshModels, store } from '../store'

const message = useMessage()
const engine = ref<'auto' | 'cuda' | 'directml'>('auto')
const precision = ref<'fp16' | 'fp32'>('fp16')
const logLines = ref<string[]>([])
const saving = ref(false)
const appVersion = ref('')
const checking = ref(false)
const updateMsg = ref('')
const updateNotes = ref('') // 新版本更新内容（Release 正文），悬浮按钮时展示

onMounted(async () => {
  const s = (await api.settings()) as {
    engine?: 'auto' | 'cuda' | 'directml'
    precision?: 'fp16' | 'fp32'
  }
  engine.value = s.engine ?? 'auto'
  precision.value = s.precision ?? 'fp16'
  appVersion.value = await window.sv.appVersion()
  loadLog()
})

async function loadLog() {
  logLines.value = (await api.logTail()).lines
}

async function exportLog() {
  const p = await window.sv.saveLog(logLines.value.join('\n') || '(空)')
  if (p) message.success(`日志已导出: ${p}`)
}

async function checkUpdate() {
  checking.value = true
  updateMsg.value = ''
  updateNotes.value = ''
  try {
    const r = await window.sv.checkUpdate()
    if (r.status === 'dev') {
      updateMsg.value = '开发模式不检查更新（打包版自动检查 GitHub Releases）'
    } else if (r.status === 'downloading') {
      updateNotes.value = r.notes ?? ''
      updateMsg.value = `发现新版本 v${r.version}，正在后台下载，完成后会弹窗提示重启${
        updateNotes.value ? '（悬浮在按钮上可查看本次更新内容）' : ''
      }`
    } else if (r.status === 'latest') {
      updateMsg.value = `已是最新版本（${r.current}）`
    } else if (r.status === 'error') {
      updateMsg.value = `检查失败：${r.error ?? '未知错误'}（发布前属正常，见 README 发布流程）`
    } else {
      updateMsg.value = '检查中，请稍候'
    }
  } finally {
    checking.value = false
  }
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
        当前实际后端：<NTag size="small" type="info" :bordered="false">
          {{ store.engine?.backend === 'cuda' ? 'CUDA' : 'DirectML' }}
        </NTag>
      </p>
      <NRadioGroup v-model:value="engine">
        <NRadioButton value="auto">自动</NRadioButton>
        <NRadioButton value="directml">DirectML</NRadioButton>
        <NRadioButton value="cuda">CUDA</NRadioButton>
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

    <NCard title="应用与更新" size="small">
      <div class="update-row">
        <span>当前版本 <b>v{{ appVersion }}</b> · 更新源：GitHub Releases</span>
        <NPopover trigger="hover" placement="top-end" :disabled="!updateNotes" :width="380" trigger-style="display: inline-flex">
          <template #trigger>
            <NButton size="small" :loading="checking" @click="checkUpdate">检查更新</NButton>
          </template>
          <div class="update-notes">
            <div class="update-notes-head">本次更新内容</div>
            <div class="update-notes-body">{{ updateNotes }}</div>
          </div>
        </NPopover>
      </div>
      <p v-if="updateMsg" class="hint" style="margin-top: 8px">{{ updateMsg }}</p>
    </NCard>

    <NCard title="本机环境" size="small">
      <NSpace vertical :size="6">
        <div>GPU：{{ store.gpuName }} <span v-if="store.hardware?.gpus?.[0]?.vram_gb">({{ store.hardware.gpus[0].vram_gb }}GB)</span></div>
        <div>CPU：{{ store.hardware?.cpu }} · {{ store.hardware?.cpu_cores }} 核心</div>
        <div>内存：{{ store.hardware?.ram_gb }} GB</div>
        <div>NVENC 硬编：{{ store.hardware?.nvenc ? '可用' : '不可用' }}</div>
      </NSpace>
    </NCard>

    <NCard size="small">
      <template #header>
        服务日志（最近 120 行）
        <NButton size="tiny" style="margin-left: 10px" @click="loadLog">刷新</NButton>
        <NButton size="tiny" style="margin-left: 8px" @click="exportLog">导出</NButton>
      </template>
      <NCode :code="logLines.join('\n') || '(空)'" language="text" :word-wrap="true" class="log" />
    </NCard>
  </div>
</template>

<style scoped>
.settings-page { display: flex; flex-direction: column; gap: 16px; max-width: 860px; }
h1 { font-size: 20px; font-weight: 700; }
.hint { color: #9aa0a6; font-size: 12.5px; margin-bottom: 12px; }
.update-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.update-notes-head { font-weight: 600; margin-bottom: 6px; }
.update-notes-body {
  white-space: pre-wrap;
  font-size: 12px;
  line-height: 1.6;
  color: #c9cdd4;
  max-height: 240px;
  overflow-y: auto;
}
.log {
  max-height: 300px;
  overflow-y: auto;
  font-size: 11.5px;
  background: #101113;
  padding: 10px;
  border-radius: 6px;
}
</style>
