<script setup lang="ts">
import { onMounted, ref } from 'vue'
import {
  NButton,
  NCard,
  NCode,
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

onMounted(async () => {
  const s = (await api.settings()) as {
    engine?: 'auto' | 'cuda' | 'directml'
    precision?: 'fp16' | 'fp32'
  }
  engine.value = s.engine ?? 'auto'
  precision.value = s.precision ?? 'fp16'
  loadLog()
})

async function loadLog() {
  logLines.value = (await api.logTail()).lines
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
      </template>
      <NCode :code="logLines.join('\n') || '(空)'" language="text" :word-wrap="true" class="log" />
    </NCard>
  </div>
</template>

<style scoped>
.settings-page { display: flex; flex-direction: column; gap: 16px; max-width: 860px; }
h1 { font-size: 20px; font-weight: 700; }
.hint { color: #9aa0a6; font-size: 12.5px; margin-bottom: 12px; }
.log {
  max-height: 300px;
  overflow-y: auto;
  font-size: 11.5px;
  background: #101113;
  padding: 10px;
  border-radius: 6px;
}
</style>
