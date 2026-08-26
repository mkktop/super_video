<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
  NButton,
  NCard,
  NCollapse,
  NCollapseItem,
  NForm,
  NFormItem,
  NInput,
  NInputNumber,
  NRadio,
  NRadioButton,
  NRadioGroup,
  NSelect,
  NSlider,
  NSwitch,
  NTag,
  useMessage,
} from 'naive-ui'
import { api, type ProbeInfo } from '../api'
import { refreshTasks, store, ui } from '../store'

const message = useMessage()

const inputs = ref<string[]>([])
const probeInfo = ref<ProbeInfo | null>(null)
const probing = ref(false)
let probeSeq = 0 // 快速连续重选文件时丢弃迟到的旧 probe 响应（旧数据覆盖新文件）
const modelId = ref('')
const targetScale = ref(2)
const resMode = ref<'scale' | 'custom'>('scale')
const targetW = ref(0)
const targetH = ref(0)
const tileChoice = ref(0) // 0 = 模型默认
const outKind = ref<'video' | 'png' | 'jpg'>('video')
const codec = ref('h264')
const crf = ref(18)
const container = ref<'mp4' | 'mkv' | 'mov'>('mp4')
const audioMode = ref('auto')
const keepSubtitles = ref(false)
const interp = ref<'off' | 'rife2x'>('off')
const denoise = ref<number | null>(null)
const output = ref('')
const outputTouched = ref(false) // 用户手动改过路径后，自动填充不再覆盖（换文件时重置）
const submitting = ref(false)
const modelSec = ref<HTMLElement | null>(null)

/** 设置里的全局输出目录；空 = 保存到源视频同目录 */
const globalOutDir = computed(() => String(store.settings.output_dir ?? '').trim())
const outPlaceholder = computed(() =>
  globalOutDir.value ? globalOutDir.value : '默认与输入同目录',
)
// 批量任务不逐个填路径，由后端落到同一目录——界面上如实说明去向
const batchDest = computed(() => globalOutDir.value || '源视频所在目录')

function joinDefault(dir: string, name: string): string {
  return `${dir.replace(/[\\/]+$/, '')}\\${name}`
}

/** 后端 _sr_output_name 的镜像：目录无同名 → 沿用原名；同名（含源文件本身）
 *  → _倍率 后缀。异步查存在性，表单预填与实际创建保持一致。 */
async function defaultOutputName(stem: string, fmt: string, srcPath: string): Promise<string> {
  const suffix =
    resMode.value === 'custom' ? `${effW.value}x${effH.value}` : `${targetScale.value}x`
  const suffixed = `${stem}_${suffix}.${fmt}`
  const outDir = globalOutDir.value || srcPath.replace(/[\\/][^\\/]*$/, '')
  const plain = joinDefault(outDir, `${stem}.${fmt}`)
  // 与源同路径（同目录同扩展名）时"同名文件"就是源本身，必须保后缀
  if (plain.toLowerCase() !== srcPath.toLowerCase() && !(await window.sv.fsExists(plain))) {
    return plain
  }
  return joinDefault(outDir, suffixed)
}

async function autoFillOutput() {
  if (inputs.value.length !== 1 || outputTouched.value) return
  const p = inputs.value[0]
  const m = p.match(/^(.*?)(\.[^.]+)?$/)
  const stem = (m?.[1] ?? p).split(/[\\/]/).pop() ?? p
  if (isImage.value) {
    output.value = joinDefault(
      globalOutDir.value || p.replace(/[\\/][^\\/]*$/, ''),
      `${stem}_${resMode.value === 'custom' ? `${effW.value}x${effH.value}` : targetScale.value}_frames`,
    )
    return
  }
  output.value = await defaultOutputName(stem, container.value, p)
}

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
// 降噪档位随模型注册表动态出（real-cugan 专属；缺省回退保守/3 两档）
const denoiseLabel = {
  0: '不降噪（no-denoise）',
  1: '轻度 denoise 1',
  2: '中度 denoise 2',
  3: 'denoise 3（去压缩噪，适合老片）',
} as Record<number, string>
const denoiseOptions = computed(() => {
  const levels = selectedModel.value?.denoise_levels ?? [0, 3]
  return levels.map((n) => ({ label: denoiseLabel[n] ?? `denoise ${n}`, value: n }))
})
const hasDenoiseVariants = computed(
  () => (selectedModel.value?.denoise_levels?.length ?? 0) > 0,
)
const nvencOk = computed(() => store.hardware?.nvenc ?? false)
const av1NvencOk = computed(() => store.hardware?.av1_nvenc ?? false)
const amfOk = computed(() => store.hardware?.amf ?? false)
const svtOk = computed(() => store.hardware?.svt_av1 ?? false)
const codecOptions = computed(() => [
  { label: 'H.264 · 软件编码（兼容性好）', value: 'h264' },
  { label: nvencOk.value ? 'H.264 · 硬件编码 NVENC（速度优先）' : 'H.264 · 硬件编码（当前设备不支持）', value: 'h264_nvenc', disabled: !nvencOk.value },
  { label: 'H.265 · 软件编码（体积较小）', value: 'h265' },
  { label: nvencOk.value ? 'H.265 · 硬件编码 NVENC（速度与体积均衡）' : 'H.265 · 硬件编码（当前设备不支持）', value: 'hevc_nvenc', disabled: !nvencOk.value },
  { label: av1NvencOk.value ? 'AV1 · 硬件编码 NVENC（RTX 40 系及以上，体积最小）' : 'AV1 · 硬件编码（当前设备不支持）', value: 'av1_nvenc', disabled: !av1NvencOk.value },
  { label: amfOk.value ? 'H.264 · 硬件编码 AMF（AMD 显卡）' : 'H.264 · 硬件编码 AMF（需 AMD 显卡）', value: 'h264_amf', disabled: !amfOk.value },
  { label: amfOk.value ? 'H.265 · 硬件编码 AMF（AMD 显卡）' : 'H.265 · 硬件编码 AMF（需 AMD 显卡）', value: 'hevc_amf', disabled: !amfOk.value },
  { label: svtOk.value ? 'AV1 · 软件编码 SVT（体积小，速度中等）' : 'AV1 · 软件编码 SVT（当前环境不支持）', value: 'av1_svt', disabled: !svtOk.value },
])
const containerOptions = [
  { label: 'MP4（兼容性最好）', value: 'mp4' },
  { label: 'MKV（字幕/音频全兼容）', value: 'mkv' },
  { label: 'MOV（QuickTime）', value: 'mov' },
]
const audioOptions = computed(() => [
  { label: '自动（兼容则原样保留）', value: 'auto' },
  { label: '原样保留（copy，需容器兼容）', value: 'copy' },
  { label: '转 AAC 192k（最兼容）', value: 'aac' },
  { label: container.value === 'mkv' ? 'FLAC 无损（仅 MKV）' : 'FLAC 无损（切到 MKV 可用）', value: 'flac', disabled: container.value !== 'mkv' },
  { label: '不保留音轨', value: 'none' },
])
watch(container, (c) => {
  if (c !== 'mkv' && audioMode.value === 'flac') audioMode.value = 'auto'
})
const srcSubs = computed(() => probeInfo.value?.subtitles ?? [])
const subHint = computed(() => {
  if (!srcSubs.value.length) return ''
  if (container.value === 'mkv') return `MKV 将原样保留全部 ${srcSubs.value.length} 条字幕轨`
  return 'MP4/MOV 仅支持文本字幕（转 mov_text），图形字幕（PGS 等）会被丢弃'
})

const speedLabel = { fast: '⚡', balanced: '⚖', slow: '🐢' } as Record<string, string>

const isImage = computed(() => outKind.value !== 'video')
const imgFrameHint = computed(() => {
  if (!isImage.value || !probeInfo.value?.ok) return ''
  const n = probeInfo.value.total_frames * (interp.value === 'rife2x' ? 2 : 1)
  return `将导出全部 ${n} 帧，按 000001.${outKind.value} 顺序编号`
})

// ---- 自定义分辨率 ----
const customAvailable = computed(
  () => inputs.value.length === 1 && !!probeInfo.value?.ok,
)
const srcW = computed(() => probeInfo.value?.width ?? 0)
const srcH = computed(() => probeInfo.value?.height ?? 0)
const maxScale = computed(() => Math.max(...(selectedModel.value?.scale ?? [1])))
// yuv420 编码要求偶数，奇数向下取整（后端同规则）
const effW = computed(() => Math.max(2, Math.floor((targetW.value || 0) / 2) * 2))
const effH = computed(() => Math.max(2, Math.floor((targetH.value || 0) / 2) * 2))
// 纪律：只允许"原生超分后缩小"——取能覆盖目标的最小原生倍率（省算力）
const customScale = computed(() =>
  (selectedModel.value?.scale ?? []).find(
    (s) => srcW.value * s >= effW.value && srcH.value * s >= effH.value,
  ) ?? null,
)
const belowSrc = computed(() => effW.value < srcW.value || effH.value < srcH.value)
const customOk = computed(
  () => customAvailable.value && !belowSrc.value && customScale.value !== null,
)
const aspectNote = computed(() => {
  if (!srcW.value || !srcH.value || !effW.value || !effH.value) return ''
  const a1 = srcW.value / srcH.value
  const a2 = effW.value / effH.value
  return Math.abs(a1 - a2) / a1 > 0.01
    ? `宽高比将由 ${a1.toFixed(2)} 变为 ${a2.toFixed(2)}（轻微拉伸）`
    : ''
})

watch(resMode, (m) => {
  if (m === 'custom' && probeInfo.value?.ok) {
    targetW.value = probeInfo.value.width * targetScale.value
    targetH.value = probeInfo.value.height * targetScale.value
  }
})
watch(
  () => inputs.value.length,
  (n) => {
    if (n !== 1) resMode.value = 'scale' // 批量无逐文件探测，只支持倍数模式
  },
)

const tileOptions = [
  { label: '自动（模型默认）', value: 0 },
  ...[128, 192, 256, 384, 512, 768, 1024].map((v) => ({ label: `${v} px`, value: v })),
]

// ---- 提交校验：单文件需探测成功，多文件批量直接放行（无逐文件探测） ----
const canSubmit = computed(
  () =>
    inputs.value.length > 0 &&
    (inputs.value.length > 1 || !!probeInfo.value?.ok) &&
    !!modelId.value &&
    !!selectedModel.value?.vram_ok &&
    !(resMode.value === 'custom' && !customOk.value),
)

// ---- 文件选择 ----
async function setInput(files: string[]) {
  const seq = ++probeSeq
  inputs.value = files
  probeInfo.value = null
  outputTouched.value = false // 新一轮选文件：恢复自动填充
  if (files.length === 1) {
    probing.value = true
    const r = await api.probe(files[0])
    if (seq !== probeSeq) return // 已重选其他文件：丢弃过期响应
    probing.value = false
    if (r.ok) {
      probeInfo.value = (await r.json()) as ProbeInfo
    } else {
      const e = await r.json()
      probeInfo.value = {
        ok: false, error: e.detail ?? `HTTP ${r.status}`,
        width: 0, height: 0, fps: 0, duration_s: 0, total_frames: 0,
        codec: '', pix_fmt: '', has_audio: false, subtitles: [],
      }
    }
    void autoFillOutput()
  }
}

async function pickInput() {
  const files = await window.sv.pickVideo()
  if (!files.length) return
  await setInput(files)
}

// 剪切页"去超分"入口：跳到本页时预填输入
watch(
  () => ui.page,
  async (p) => {
    if (p === 'newtask' && ui.pendingInput) {
      const path = ui.pendingInput
      ui.pendingInput = null
      await setInput([path])
    }
  },
)

// 模型对比页"用此模型"入口：预填输入后再预选模型与倍率
// （pendingModel 先于 page 设置，故同时盯两个信号）
watch([() => ui.page, () => ui.pendingModel], () => {
  if (ui.page !== 'newtask' || !ui.pendingModel) return
  const spec = store.models.find((m) => m.id === ui.pendingModel)
  if (spec?.vram_ok) {
    modelId.value = spec.id
    const want = ui.pendingScale
    targetScale.value =
      want && spec.scale.includes(want) ? want : Math.min(...spec.scale)
  }
  ui.pendingModel = null
  ui.pendingScale = null
})

watch([targetScale, resMode, effW, effH, outKind, container], () => void autoFillOutput())

async function pickOutputFile() {
  if (isImage.value) {
    const p = await window.sv.pickDir()
    if (p) {
      output.value = p
      outputTouched.value = true // 用户显式选择的位置不再被自动填充覆盖
    }
    return
  }
  const p = await window.sv.pickOutput(output.value || 'output.mp4')
  if (p) {
    output.value = p
    outputTouched.value = true
  }
}

// ---- 预设 ----
function applyPreset(pid: string) {
  const p = store.presets.find((x) => x.id === pid)
  if (!p) return
  modelId.value = p.model_id
  targetScale.value = p.target_scale
  resMode.value = 'scale'
  tileChoice.value = 0
  outKind.value = 'video'
  codec.value = p.codec
  crf.value = p.crf
  container.value = 'mp4'
  audioMode.value = 'auto'
  keepSubtitles.value = false
  interp.value = (p as { interp?: 'off' | 'rife2x' }).interp ?? 'off'
  denoise.value = null
  autoFillOutput()
  message.success(
    `已应用「${p.name}」：${selectedModel.value?.name ?? p.model_id} · x${p.target_scale}`,
  )
  // 原弹窗点预设会跳步骤给出反馈；整页后改为滚到模型区，让选中的卡片看得见
  modelSec.value?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

// ---- 提交 ----
async function submit() {
  if (!canSubmit.value) return
  if (resMode.value === 'custom' && !customOk.value) {
    message.error('自定义分辨率参数无效，请检查目标宽高')
    return
  }
  submitting.value = true
  let ok = 0
  let lastErr = ''
  const scaleToSend = resMode.value === 'custom' ? customScale.value! : targetScale.value
  for (const input of inputs.value) {
    const out = inputs.value.length === 1 ? output.value || undefined : undefined
    const r = await api.createTask({
      input,
      output: out,
      model_id: modelId.value,
      params: {
        scale: scaleToSend,
        target_scale: scaleToSend,
        ...(resMode.value === 'custom' ? { target_w: effW.value, target_h: effH.value } : {}),
        ...(isImage.value
          ? { out_kind: outKind.value }
          : {
              codec: codec.value,
              crf: crf.value,
              container: container.value,
              audio_mode: audioMode.value,
              subtitle_mode: keepSubtitles.value ? 'auto' : 'none',
            }),
        interp: interp.value,
        ...(denoise.value !== null ? { denoise: denoise.value } : {}),
        ...(tileChoice.value ? { tile: tileChoice.value } : {}),
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
    reset()
    ui.page = 'tasks'
    refreshTasks()
  } else {
    message.error(`创建失败: ${lastErr}`)
  }
}

// 清空本轮选择；编码/画质等输出偏好保留上次取值，连续建任务不用重设
function reset() {
  inputs.value = []
  probeInfo.value = null
  modelId.value = ''
  output.value = ''
  outputTouched.value = false
}

const fmtDur = (s: number) => `${Math.floor(s / 60)}分${Math.round(s % 60)}秒`
</script>

<template>
  <div class="newtask-page">
    <div class="page-head">
      <div>
        <h1>新建超分任务</h1>
        <p class="sub">选择视频与模型 → 配置输出 → 加入队列串行处理</p>
      </div>
    </div>

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

    <!-- ① 选择视频 -->
    <section class="sec">
      <h2 class="sec-title"><span class="sec-num">1</span>选择视频</h2>
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
    </section>

    <!-- ② 选择模型 -->
    <section ref="modelSec" class="sec">
      <h2 class="sec-title">
        <span class="sec-num">2</span>选择模型
        <span class="sel-chip" :class="{ on: !!selectedModel }">
          {{ selectedModel ? `已选 ${selectedModel.name} · x${targetScale}` : '点击卡片选择' }}
        </span>
      </h2>
      <div class="model-grid">
        <div
          v-for="m in srModels"
          :key="m.id"
          class="model-card"
          :class="{ selected: modelId === m.id, disabled: !m.vram_ok }"
          @click="m.vram_ok && ((modelId = m.id), (targetScale = Math.min(...m.scale)))"
        >
          <span v-if="modelId === m.id" class="m-check">✓</span>
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
    </section>

    <!-- ③ 输出设置 -->
    <section class="sec">
      <h2 class="sec-title"><span class="sec-num">3</span>输出设置</h2>
      <NForm label-placement="left" label-width="92">
        <div class="out-cols">
          <div class="out-col">
            <NFormItem label="输出格式">
              <NRadioGroup v-model:value="outKind" size="small">
                <NRadioButton value="video">视频</NRadioButton>
                <NRadioButton value="png">PNG 图片序列</NRadioButton>
                <NRadioButton value="jpg">JPG 图片序列</NRadioButton>
              </NRadioGroup>
              <span v-if="imgFrameHint" class="img-hint">{{ imgFrameHint }}</span>
            </NFormItem>
            <NFormItem label="输出分辨率">
              <div class="res-row">
                <NRadioGroup v-model:value="resMode" size="small">
                  <NRadio value="scale">按倍数</NRadio>
                  <NRadio value="custom" :disabled="!customAvailable">自定义</NRadio>
                </NRadioGroup>
                <template v-if="resMode === 'scale'">
                  <NSelect v-model:value="targetScale" :options="scaleOptions" style="width: 110px" />
                  <NTag v-if="probeInfo && selectedModel" size="small" :bordered="false">
                    {{ probeInfo.width }}x{{ probeInfo.height }} →
                    {{ probeInfo.width * targetScale }}x{{ probeInfo.height * targetScale }}
                  </NTag>
                </template>
                <template v-else>
                  <NInputNumber v-model:value="targetW" :min="16" :max="7680" :step="2" size="small" style="width: 118px" />
                  <span class="res-x">×</span>
                  <NInputNumber v-model:value="targetH" :min="16" :max="4320" :step="2" size="small" style="width: 118px" />
                  <NTag v-if="customScale" size="small" :bordered="false" type="info">
                    x{{ customScale }} 超分后缩放
                  </NTag>
                </template>
              </div>
            </NFormItem>
            <div v-if="resMode === 'custom'" class="res-hints">
              <span v-if="belowSrc" class="res-err">
                目标分辨率不能低于源分辨率（{{ srcW }}x{{ srcH }}）
              </span>
              <span v-else-if="!customScale" class="res-err">
                超出该模型 x{{ maxScale }} 上限（{{ srcW * maxScale }}x{{ srcH * maxScale }}），请减小目标或换更高倍率模型
              </span>
              <span v-else-if="aspectNote" class="res-warn">{{ aspectNote }}</span>
              <span v-else class="res-ok">
                先以 x{{ customScale }} 原生超分，再 lanczos 缩放至 {{ effW }}x{{ effH }}（宽高自动取偶数）
              </span>
            </div>
            <NFormItem v-if="outKind === 'video'" label="编码器">
              <NSelect v-model:value="codec" :options="codecOptions" style="width: 300px" />
            </NFormItem>
            <NFormItem v-if="outKind === 'video'" label="封装容器">
              <NSelect v-model:value="container" :options="containerOptions" style="width: 300px" />
            </NFormItem>
            <NFormItem v-if="outKind === 'video'" label="音轨">
              <NSelect v-model:value="audioMode" :options="audioOptions" style="width: 300px" />
            </NFormItem>
            <NFormItem v-if="outKind === 'video' && srcSubs.length" label="字幕">
              <div class="sub-row">
                <NSwitch v-model:value="keepSubtitles" size="small" />
                <span class="sub-hint">{{ subHint }}</span>
              </div>
            </NFormItem>
          </div>
          <div class="out-col">
            <NFormItem v-if="outKind === 'video'" label="画质 (CRF)">
              <NSlider v-model:value="crf" :min="12" :max="30" :step="1" :marks="{ 14: '近无损', 18: '推荐', 24: '小体积' }" />
            </NFormItem>
            <NFormItem label="补帧">
              <NSelect v-model:value="interp" :options="interpOptions" style="width: 320px" />
              <NTag v-if="interp === 'rife2x' && probeInfo" size="small" :bordered="false" type="info" style="margin-left: 10px">
                {{ probeInfo.fps }} → {{ probeInfo.fps * 2 }} fps
              </NTag>
            </NFormItem>
            <NFormItem v-if="hasDenoiseVariants" label="降噪">
              <NSelect v-model:value="denoise" :options="denoiseOptions" style="width: 320px" placeholder="选择降噪档位（默认保守模式）" />
            </NFormItem>
            <NCollapse class="adv-collapse" :default-expanded-names="[]">
              <NCollapseItem title="高级选项" name="adv">
                <NFormItem label="分块大小" :show-feedback="false">
                  <NSelect v-model:value="tileChoice" :options="tileOptions" style="width: 200px" />
                </NFormItem>
                <div class="adv-note">
                  自动=按模型默认。显存不足或大分辨率卡顿时调小分块；分块越小越省显存但速度越慢。
                </div>
              </NCollapseItem>
            </NCollapse>
            <NFormItem v-if="inputs.length === 1" label="输出到">
              <NInput
                v-model:value="output"
                :placeholder="outPlaceholder"
                @update:value="() => (outputTouched = true)"
              >
                <template #suffix>
                  <NButton size="tiny" @click="pickOutputFile">浏览…</NButton>
                </template>
              </NInput>
            </NFormItem>
            <NFormItem v-else-if="inputs.length > 1" label="批量说明">
              <span class="batch-note">
                {{ inputs.length }} 个文件将使用以上相同参数依次入队（串行处理），
                输出到「{{ batchDest }}」；可在 设置 → 输出位置 修改默认目录
              </span>
            </NFormItem>
          </div>
        </div>
      </NForm>
    </section>

    <!-- 吸底操作条 -->
    <div class="footer-bar">
      <NButton :disabled="submitting" @click="reset">清空</NButton>
      <NButton
        type="primary"
        :loading="submitting"
        :disabled="!canSubmit"
        @click="submit"
      >
        加入队列（{{ inputs.length || 0 }} 个）
      </NButton>
    </div>
  </div>
</template>

<style scoped>
.newtask-page {
  display: flex;
  flex-direction: column;
  gap: 18px;
  min-height: 100%;
}
h1 { font-size: 20px; font-weight: 700; }
.sub { font-size: 12.5px; color: #9aa0a6; margin-top: 4px; }

.presets {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  border: 1px solid #2c3138;
  border-radius: 8px;
  background: linear-gradient(90deg, rgba(79, 140, 255, 0.06), rgba(139, 92, 246, 0.05));
  flex-wrap: wrap;
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

.sec { display: flex; flex-direction: column; gap: 12px; }
.sec-title {
  font-size: 15px;
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: 8px;
}
.sel-chip {
  margin-left: auto;
  font-size: 12px;
  font-weight: 400;
  color: #9aa0a6;
}
.sel-chip.on { color: #4f8cff; }
.sec-num {
  width: 20px;
  height: 20px;
  border-radius: 6px;
  background: linear-gradient(135deg, #4f8cff, #8b5cf6);
  color: #fff;
  font-size: 12px;
  font-weight: 600;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.probe-card { background: #1a1c1f; }
.probe-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 8px 16px;
  font-size: 13px;
  color: #9aa0a6;
}
.probe-grid b { color: #e8eaed; font-weight: 600; margin-left: 4px; }
.probe-err { color: #f87171; font-size: 13px; }
.probe-hint { color: #9aa0a6; font-size: 13px; text-align: center; }

.model-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px; }
.model-card {
  position: relative;
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
.m-check {
  position: absolute;
  top: 10px;
  right: 10px;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: #4f8cff;
  color: #fff;
  font-size: 11px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.m-head { display: flex; align-items: center; gap: 8px; }
.m-name { font-weight: 600; font-size: 14px; }
.m-desc { color: #9aa0a6; font-size: 12px; margin: 6px 0; }
.m-tags { display: flex; gap: 10px; font-size: 12px; color: #7c838c; }
.m-warn { margin-top: 6px; font-size: 11.5px; color: #f87171; }

/* 输出设置两列；窄窗口（内容宽 <752px）自动退化单列 */
.out-cols {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
  column-gap: 36px;
}
.out-col { display: flex; flex-direction: column; }

.batch-note { font-size: 12.5px; color: #9aa0a6; }
.res-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.sub-row { display: flex; align-items: center; gap: 10px; }
.sub-hint { font-size: 12px; color: #9aa0a6; }
.img-hint { margin-left: 12px; font-size: 12px; color: #9aa0a6; }
.res-x { color: #9aa0a6; }
.res-hints { font-size: 12px; margin: -6px 0 2px 102px; min-height: 16px; }
.res-err { color: #f87171; }
.res-warn { color: #fbbf24; }
.res-ok { color: #9aa0a6; }
.adv-collapse { border: none; }
.adv-collapse :deep(.n-collapse-item__header) { padding: 8px 0 0; }
.adv-note { font-size: 12px; color: #9aa0a6; margin-top: 6px; }

/* 吸底操作条：滚动时贴住可视区底部，内容不足一屏时沉到页底 */
.footer-bar {
  position: sticky;
  bottom: 0;
  margin-top: auto;
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 4px 4px;
  background: #141517;
  border-top: 1px solid #232629;
  z-index: 5;
}
</style>
