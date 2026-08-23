<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
  NButton,
  NForm,
  NFormItem,
  NInput,
  NModal,
  NRadioGroup,
  NRadioButton,
  NSelect,
  NSlider,
  NSpace,
  NTag,
  useMessage,
} from 'naive-ui'
import { api } from '../api'
import { refreshTasks, store } from '../store'

const show = defineModel<boolean>('show', { default: false })
const message = useMessage()

const inputs = ref<string[]>([])
const modelId = ref<string>('')
const targetScale = ref<number>(4)
const codec = ref<string>('h264')
const crf = ref(18)
const output = ref('')
const submitting = ref(false)

const modelOptions = computed(() =>
  store.models.map((m) => ({
    label: `${m.name}（${m.speed === 'fast' ? '快速' : m.speed === 'slow' ? '慢速' : '均衡'} · ${m.content.includes('anime') ? '动漫' : '通用'}）`,
    value: m.id,
    disabled: !m.installed,
  })),
)

const selectedModel = computed(() => store.models.find((m) => m.id === modelId.value))
const nativeScale = computed(() => Math.max(...(selectedModel.value?.scale ?? [4])))

/** 模型原生倍率内的目标倍率；小于原生时"4x 重建后缩放"，避免 1080p 直接被顶到 8K */
const targetOptions = computed(() =>
  [2, 3, 4]
    .filter((t) => t <= nativeScale.value)
    .map((t) => ({
      label: t === nativeScale.value ? `x${t}（原生）` : `x${t}（重建后缩放）`,
      value: t,
    })),
)

const nvencOk = computed(() => store.hardware?.nvenc ?? false)
const codecOptions = computed(() => [
  { label: 'H.264 · 软编（兼容性好）', value: 'h264' },
  { label: nvencOk.value ? 'H.264 · 硬编 NVENC（推荐，快）' : 'H.264 · 硬编（本机不可用）', value: 'h264_nvenc', disabled: !nvencOk.value },
  { label: 'H.265 · 软编（体积小）', value: 'h265' },
  { label: nvencOk.value ? 'H.265 · 硬编 NVENC（快且小）' : 'H.265 · 硬编（本机不可用）', value: 'hevc_nvenc', disabled: !nvencOk.value },
])

watch(modelId, () => {
  targetScale.value = nativeScale.value
  autoFillOutput()
})
watch(targetScale, autoFillOutput)

function autoFillOutput() {
  if (inputs.value.length === 1) {
    const p = inputs.value[0]
    const m = p.match(/^(.*?)(\.[^.]+)?$/)
    output.value = `${m?.[1]}_${targetScale.value}x.mp4`
  } else if (inputs.value.length > 1) {
    output.value = '' // 多文件时输出自动命名
  }
}

async function pickInput() {
  const files = await window.sv.pickVideo()
  if (files.length) {
    inputs.value = files
    autoFillOutput()
  }
}

async function pickOutputFile() {
  if (!output.value) return
  const p = await window.sv.pickOutput(output.value)
  if (p) output.value = p
}

async function submit() {
  if (!inputs.value.length) return message.warning('请先选择视频文件')
  if (!modelId.value) return message.warning('请选择超分模型')
  submitting.value = true
  let ok = 0
  let lastErr = ''
  for (const input of inputs.value) {
    const out = inputs.value.length === 1 ? output.value || undefined : undefined
    const r = await api.createTask({
      input,
      output: out,
      model_id: modelId.value,
      params: {
        scale: nativeScale.value,
        target_scale: targetScale.value,
        codec: codec.value,
        crf: crf.value,
      },
    })
    if (r.ok) ok++
    else lastErr = `${(await r.json()).detail ?? r.status}`
  }
  submitting.value = false
  if (ok) {
    message.success(`已加入队列 ${ok} 个任务${lastErr ? `；失败: ${lastErr}` : ''}`)
    show.value = false
    inputs.value = []
    output.value = ''
    refreshTasks() // 立即刷新，不等轮询/WS
  } else {
    message.error(`创建失败: ${lastErr}`)
  }
}
</script>

<template>
  <n-modal
    v-model:show="show"
    preset="card"
    title="新建超分任务"
    style="width: 620px"
    :mask-closable="!submitting"
  >
    <n-form label-placement="left" label-width="92">
      <n-form-item label="视频文件" path="input">
        <n-space vertical style="width: 100%">
          <n-input
            :value="inputs.length === 1 ? inputs[0] : inputs.length ? `已选 ${inputs.length} 个文件` : ''"
            placeholder="点击右侧按钮选择（可多选批量入队）"
            readonly
            @click="pickInput"
          >
            <template #suffix>
              <n-button size="tiny" @click.stop="pickInput">浏览…</n-button>
            </template>
          </n-input>
        </n-space>
      </n-form-item>

      <n-form-item label="模型">
        <n-select v-model:value="modelId" :options="modelOptions" placeholder="选择超分模型" />
      </n-form-item>

      <n-form-item label="放大倍数">
        <n-select v-model:value="targetScale" :options="targetOptions" style="width: 210px" />
        <n-tag v-if="selectedModel" size="small" :bordered="false" style="margin-left: 10px">
          {{ selectedModel.description }}
        </n-tag>
      </n-form-item>

      <n-form-item label="编码器">
        <n-select v-model:value="codec" :options="codecOptions" style="width: 300px" />
      </n-form-item>

      <n-form-item label="画质 (CRF)">
        <n-slider v-model:value="crf" :min="12" :max="30" :step="1" :marks="{ 14: '近无损', 18: '推荐', 24: '小体积' }" />
      </n-form-item>

      <n-form-item v-if="inputs.length === 1" label="输出到">
        <n-input v-model:value="output" placeholder="默认与输入同目录">
          <template #suffix>
            <n-button size="tiny" @click="pickOutputFile">浏览…</n-button>
          </template>
        </n-input>
      </n-form-item>
    </n-form>

    <template #footer>
      <div style="display: flex; justify-content: flex-end; gap: 10px">
        <n-button :disabled="submitting" @click="show = false">取消</n-button>
        <n-button type="primary" :loading="submitting" @click="submit">加入队列</n-button>
      </div>
    </template>
  </n-modal>
</template>
