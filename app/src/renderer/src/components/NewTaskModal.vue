<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
  NButton,
  NCard,
  NForm,
  NFormItem,
  NInput,
  NModal,
  NSelect,
  NSlider,
  NSpace,
  NStep,
  NSteps,
  NTag,
  useMessage,
} from 'naive-ui'
import { api, type ProbeInfo } from '../api'
import { refreshTasks, store } from '../store'

const show = defineModel<boolean>('show', { default: false })
const message = useMessage()

const step = ref(1)
const inputs = ref<string[]>([])
const probeInfo = ref<ProbeInfo | null>(null)
const probing = ref(false)
const modelId = ref('')
const targetScale = ref(2)
const codec = ref('h264')
const crf = ref(18)
const interp = ref<'off' | 'rife2x'>('off')
const denoise = ref<number | null>(null)
const output = ref('')
const submitting = ref(false)

// ---- 模型 ----
const srModels = computed(() => store.models.filter((m) => m.kind !== 'interp'))
const selectedModel = computed(() => store.models.find((m) => m.id === modelId.value))
const scaleOptions = computed(() =>
  (selectedModel.value?.scale ?? []).map((s) => ({ label: `x${s}`, value: s })),
)
const interpOptions = computed(() => [
  { label: '关闭', value: 'off' },
  { label: 'RIFE 2×（帧率翻倍，需下载 23MB 模型）', value: 'rife2x' },
])
const denoiseOptions = [
  { label: '关闭（保守模式，不降噪）', value: 0 },
  { label: 'denoise 3（去压缩噪，适合老片）', value: 3 },
]
const nvencOk = computed(() => store.hardware?.nvenc ?? false)
const codecOptions = computed(() => [
  { label: 'H.264 · 软编（兼容性好）', value: 'h264' },
  { label: nvencOk.value ? 'H.264 · 硬编 NVENC（快）' : 'H.264 · 硬编（本机不可用）', value: 'h264_nvenc', disabled: !nvencOk.value },
  { label: 'H.265 · 软编（体积小）', value: 'h265' },
  { label: nvencOk.value ? 'H.265 · 硬编 NVENC（快且小）' : 'H.265 · 硬编（本机不可用）', value: 'hevc_nvenc', disabled: !nvencOk.value },
])

const speedLabel = { fast: '⚡', balanced: '⚖', slow: '🐢' } as Record<string, string>

// ---- 步骤校验 ----
const step1Ok = computed(() => inputs.value.length > 0 && !!probeInfo.value?.ok)
const step2Ok = computed(() => !!modelId.value && !!selectedModel.value?.vram_ok)

function canNext(): boolean {
  if (step.value === 1) return step1Ok.value
  if (step.value === 2) return step2Ok.value
  return false
}

// ---- 文件选择 ----
async function pickInput() {
  const files = await window.sv.pickVideo()
  if (!files.length) return
  inputs.value = files
  probeInfo.value = null
  if (files.length === 1) {
    probing.value = true
    const r = await api.probe(files[0])
    probing.value = false
    if (r.ok) {
      probeInfo.value = (await r.json()) as ProbeInfo
    } else {
      const e = await r.json()
      probeInfo.value = {
        ok: false, error: e.detail ?? `HTTP ${r.status}`,
        width: 0, height: 0, fps: 0, duration_s: 0, total_frames: 0,
        codec: '', pix_fmt: '', has_audio: false,
      }
    }
    autoFillOutput()
  }
}

function autoFillOutput() {
  if (inputs.value.length === 1) {
    const p = inputs.value[0]
    const m = p.match(/^(.*?)(\.[^.]+)?$/)
    output.value = `${m?.[1]}_${targetScale.value}x.mp4`
  }
}

watch(targetScale, autoFillOutput)

async function pickOutputFile() {
  const p = await window.sv.pickOutput(output.value || 'output.mp4')
  if (p) output.value = p
}

// ---- 预设 ----
function applyPreset(pid: string) {
  const p = store.presets.find((x) => x.id === pid)
  if (!p) return
  modelId.value = p.model_id
  targetScale.value = p.target_scale
  codec.value = p.codec
  crf.value = p.crf
  interp.value = (p as { interp?: 'off' | 'rife2x' }).interp ?? 'off'
  denoise.value = null
  autoFillOutput()
  message.success(`已应用预设「${p.name}」`)
  if (step.value === 1 && inputs.value.length) step.value = 3
}

// ---- 提交 ----
async function submit() {
  if (!inputs.value.length || !modelId.value) return
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
        scale: targetScale.value,
        target_scale: targetScale.value,
        codec: codec.value,
        crf: crf.value,
        interp: interp.value,
        ...(denoise.value !== null ? { denoise: denoise.value } : {}),
      },
    })
    if (r.ok) ok++
    else lastErr = `${(await r.json()).detail ?? r.status}`
  }
  submitting.value = false
  if (ok) {
    message.success(
      `已加入队列 ${ok} 个任务${selectedModel.value && !selectedModel.value.installed ? '（模型将自动下载）' : ''}${lastErr ? `；失败: ${lastErr}` : ''}`,
    )
    show.value = false
    reset()
    refreshTasks()
  } else {
    message.error(`创建失败: ${lastErr}`)
  }
}

function reset() {
  step.value = 1
  inputs.value = []
  probeInfo.value = null
  modelId.value = ''
  output.value = ''
}

const fmtDur = (s: number) => `${Math.floor(s / 60)}分${Math.round(s % 60)}秒`
</script>

<template>
  <NModal
    v-model:show="show"
    preset="card"
    title="新建超分任务"
    style="width: 720px"
    :mask-closable="!submitting"
    @after-leave="reset"
  >
    <!-- 预设条 -->
    <div class="presets">
      <span class="presets-label">一键预设</span>
      <button
        v-for="p in store.presets"
        :key="p.id"
        class="preset"
        :title="p.desc"
        @click="applyPreset(p.id)"
      >
        <span class="p-icon">{{ p.icon }}</span>{{ p.name }}
      </button>
    </div>

    <NSteps :current="step" size="small" class="steps">
      <NStep title="选择视频" />
      <NStep title="选择模型" />
      <NStep title="输出设置" />
    </NSteps>

    <!-- Step 1: 输入 -->
    <div v-if="step === 1" class="step-body">
      <NButton dashed block size="large" @click="pickInput">
        {{ inputs.length ? `已选 ${inputs.length} 个文件（点击重选）` : '点击选择视频文件（可多选批量入队）' }}
      </NButton>
      <NCard v-if="probeInfo" size="small" class="probe-card" :bordered="true">
        <div v-if="probeInfo.ok" class="probe-grid">
          <span>分辨率 <b>{{ probeInfo.width }}x{{ probeInfo.height }}</b></span>
          <span>帧率 <b>{{ probeInfo.fps }}</b></span>
          <span>时长 <b>{{ fmtDur(probeInfo.duration_s) }}</b></span>
          <span>帧数 <b>{{ probeInfo.total_frames }}</b></span>
          <span>编码 <b>{{ probeInfo.codec }} / {{ probeInfo.pix_fmt }}</b></span>
          <span>音轨 <b>{{ probeInfo.has_audio ? '有' : '无' }}</b></span>
        </div>
        <div v-else class="probe-err">{{ probeInfo.error || '文件不可用' }}</div>
      </NCard>
      <div v-else-if="probing" class="probe-hint">正在读取视频信息…</div>
    </div>

    <!-- Step 2: 模型 -->
    <div v-else-if="step === 2" class="step-body">
      <div class="model-grid">
        <div
          v-for="m in srModels"
          :key="m.id"
          class="model-card"
          :class="{ selected: modelId === m.id, disabled: !m.vram_ok }"
          @click="m.vram_ok && ((modelId = m.id), (targetScale = Math.min(...m.scale)))"
        >
          <div class="m-head">
            <span class="m-name">{{ m.name }}</span>
            <NTag v-if="!m.installed && !m.bundled" size="tiny" :bordered="false" type="warning">需下载 {{ m.size_mb }}MB</NTag>
            <NTag v-if="!m.vram_ok" size="tiny" :bordered="false" type="error">显存不足</NTag>
          </div>
          <div class="m-desc">{{ m.description }}</div>
          <div class="m-tags">
            <span>{{ speedLabel[m.speed] ?? '⚖' }}</span>
            <span>x{{ m.scale.join('/x') }}</span>
            <span>{{ m.vram_gb }}GB 显存</span>
          </div>
          <div v-if="m.vram_note" class="m-warn">{{ m.vram_note }}</div>
        </div>
      </div>
    </div>

    <!-- Step 3: 输出 -->
    <div v-else class="step-body">
      <NForm label-placement="left" label-width="92">
        <NFormItem label="放大倍数">
          <NSelect v-model:value="targetScale" :options="scaleOptions" style="width: 140px" />
          <NTag v-if="probeInfo && selectedModel" size="small" :bordered="false" style="margin-left: 10px">
            {{ probeInfo.width }}x{{ probeInfo.height }} →
            {{ probeInfo.width * targetScale }}x{{ probeInfo.height * targetScale }}
          </NTag>
        </NFormItem>
        <NFormItem label="编码器">
          <NSelect v-model:value="codec" :options="codecOptions" style="width: 300px" />
        </NFormItem>
        <NFormItem label="画质 (CRF)">
          <NSlider v-model:value="crf" :min="12" :max="30" :step="1" :marks="{ 14: '近无损', 18: '推荐', 24: '小体积' }" />
        </NFormItem>
        <NFormItem label="补帧">
          <NSelect v-model:value="interp" :options="interpOptions" style="width: 320px" />
          <NTag v-if="interp === 'rife2x' && probeInfo" size="small" :bordered="false" type="info" style="margin-left: 10px">
            {{ probeInfo.fps }} → {{ probeInfo.fps * 2 }} fps
          </NTag>
        </NFormItem>
        <NFormItem v-if="modelId === 'real-cugan'" label="降噪">
          <NSelect v-model:value="denoise" :options="denoiseOptions" style="width: 320px" placeholder="选择降噪档位" />
        </NFormItem>
        <NFormItem v-if="inputs.length === 1" label="输出到">
          <NInput v-model:value="output" placeholder="默认与输入同目录">
            <template #suffix>
              <NButton size="tiny" @click="pickOutputFile">浏览…</NButton>
            </template>
          </NInput>
        </NFormItem>
        <NFormItem v-else label="批量说明">
          <span class="batch-note">{{ inputs.length }} 个文件将使用以上相同参数依次入队（串行处理）</span>
        </NFormItem>
      </NForm>
    </div>

    <template #footer>
      <div class="footer">
        <NButton :disabled="step === 1 || submitting" @click="step--">上一步</NButton>
        <NSpace>
          <NButton v-if="step < 3" type="primary" :disabled="!canNext()" @click="step++">下一步</NButton>
          <NButton v-else type="primary" :loading="submitting" @click="submit">
            加入队列（{{ inputs.length || 0 }} 个）
          </NButton>
        </NSpace>
      </div>
    </template>
  </NModal>
</template>

<style scoped>
.presets {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  border: 1px solid #2c3138;
  border-radius: 8px;
  background: linear-gradient(90deg, rgba(79, 140, 255, 0.06), rgba(139, 92, 246, 0.05));
  margin-bottom: 16px;
}
.presets-label { font-size: 12px; color: #9aa0a6; flex-shrink: 0; }
.preset {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 7px 14px;
  border-radius: 8px;
  border: 1px solid #33373d;
  background: #23262b;
  color: #e8eaed;
  font-size: 13px;
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s;
}
.preset:hover {
  border-color: #4f8cff;
  background: #26303c;
}
.p-icon { font-size: 14px; }

.steps { margin-bottom: 18px; }
.step-body { min-height: 220px; display: flex; flex-direction: column; gap: 14px; }

.probe-card { background: #1a1c1f; }
.probe-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px 16px;
  font-size: 13px;
  color: #9aa0a6;
}
.probe-grid b { color: #e8eaed; font-weight: 600; margin-left: 4px; }
.probe-err { color: #f87171; font-size: 13px; }
.probe-hint { color: #9aa0a6; font-size: 13px; text-align: center; margin-top: 30px; }

.model-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px; }
.model-card {
  border: 1.5px solid #2a2d31;
  border-radius: 10px;
  padding: 14px;
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s;
}
.model-card:hover { border-color: #4a4f55; }
.model-card.selected {
  border-color: #4f8cff;
  background: rgba(79, 140, 255, 0.08);
}
.model-card.disabled { opacity: 0.45; cursor: not-allowed; }
.m-head { display: flex; align-items: center; gap: 8px; }
.m-name { font-weight: 600; font-size: 14px; }
.m-desc { color: #9aa0a6; font-size: 12px; margin: 6px 0; }
.m-tags { display: flex; gap: 10px; font-size: 12px; color: #7c838c; }
.m-warn { margin-top: 6px; font-size: 11.5px; color: #f87171; }

.batch-note { font-size: 12.5px; color: #9aa0a6; }
.footer { display: flex; justify-content: space-between; }
</style>
