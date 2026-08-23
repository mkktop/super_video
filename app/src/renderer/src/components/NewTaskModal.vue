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
const scale = ref<number>(4)
const codec = ref<'h264' | 'h265'>('h264')
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
const scaleOptions = computed(() =>
  (selectedModel.value?.scale ?? [4]).map((s) => ({ label: `x${s}`, value: s })),
)

watch(modelId, () => {
  const scales = selectedModel.value?.scale ?? []
  if (scales.length) scale.value = Math.max(...scales)
  autoFillOutput()
})
watch(scale, autoFillOutput)

function autoFillOutput() {
  if (inputs.value.length === 1) {
    const p = inputs.value[0]
    const m = p.match(/^(.*?)(\.[^.]+)?$/)
    output.value = `${m?.[1]}_${scale.value}x.mp4`
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
      params: { scale: scale.value, codec: codec.value, crf: crf.value },
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
        <n-select v-model:value="scale" :options="scaleOptions" style="width: 140px" />
        <n-tag v-if="selectedModel" size="small" :bordered="false" style="margin-left: 10px">
          {{ selectedModel.description }}
        </n-tag>
      </n-form-item>

      <n-form-item label="编码器">
        <n-radio-group v-model:value="codec">
          <n-radio-button value="h264">H.264（兼容性好）</n-radio-button>
          <n-radio-button value="h265">H.265（体积小）</n-radio-button>
        </n-radio-group>
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
