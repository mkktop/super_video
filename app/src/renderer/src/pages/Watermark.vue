<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { NButton, NInputNumber, NProgress, NSelect, useMessage } from 'naive-ui'
import { api, type WatermarkJob, type WatermarkMask, type WatermarkPreview, type WatermarkSample } from '../api'

const message = useMessage()
const files = ref<string[]>([])
const folder = ref('')
const outputDir = ref('')
const selected = ref(0)
const mask = reactive<WatermarkMask>({ unit: 'px', width: 175, height: 75, right: 0, bottom: 0 })
const mode = ref<'fixed' | 'smart'>('fixed')
const removal = ref<'white' | 'auto' | 'repair'>('auto')
const sample = ref<WatermarkSample | null>(null)
const smartOptions = computed(() => mode.value === 'smart'
  ? { mode: 'smart' as const, sample: sample.value ?? undefined, removal: removal.value }
  : removal.value === 'repair' && sample.value
    ? { removal: removal.value, sample: sample.value } : { removal: removal.value })
const regionLocked = computed(() => busy.value || (!!sample.value && (mode.value === 'smart' || removal.value === 'repair')))
const preview = ref<WatermarkPreview | null>(null)
const previewError = ref('')
const loading = ref(false)
const scanning = ref(false)
const submitting = ref(false)
const cancelling = ref(false)
const pollError = ref('')
const job = ref<WatermarkJob | null>(null)
const dragging = ref(false)
const active = computed(() => submitting.value || job.value?.status === 'running')
const busy = computed(() => active.value || scanning.value)
const name = (path: string) => path.split(/[\\/]/).pop() ?? path
function imageLabel(path: string) {
  const root = folder.value.replace(/\\/g, '/').replace(/\/$/, '') + '/'
  const normalized = path.replace(/\\/g, '/')
  return folder.value && normalized.toLowerCase().startsWith(root.toLowerCase())
    ? normalized.slice(root.length) : name(path)
}
const currentPath = computed(() => files.value[selected.value] ?? '')
const options = computed(() => files.value.map((path, i) => ({ label: imageLabel(path), value: i })))
const previewKey = computed(() => JSON.stringify([currentPath.value, mask, smartOptions.value]))
const renderedKey = ref('')
const canStart = computed(() => files.value.length > 0 && preview.value && !busy.value
  && !loading.value && !previewError.value && renderedKey.value === previewKey.value
  && preview.value.detected !== false
  && (mode.value !== 'smart' || (sample.value && preview.value.detected === true)))
const canCapture = computed(() => (mode.value === 'smart' || removal.value === 'repair') && !sample.value && !!preview.value
  && !busy.value && !loading.value && !previewError.value && renderedKey.value === previewKey.value)
const rectangle = computed(() => {
  if (!preview.value?.box) return {}
  const [x, y, right, bottom] = preview.value.box
  return { left: `${x / preview.value.width * 100}%`, top: `${y / preview.value.height * 100}%`,
    width: `${(right - x) / preview.value.width * 100}%`, height: `${(bottom - y) / preview.value.height * 100}%` }
})
let debounce: ReturnType<typeof setTimeout> | undefined
let polling: ReturnType<typeof setTimeout> | undefined
let sequence = 0
let disposed = false

async function pickFiles() {
  try {
    const paths = await window.sv.pickImages()
    if (!paths.length) return
    files.value = paths; folder.value = ''; selected.value = 0; sample.value = null
    preview.value = null; renderedKey.value = ''
    await nextTick(); void refreshPreview()
  } catch (e) { message.error(String(e)) }
}
async function pickFolder() {
  scanning.value = true
  try {
    const path = await window.sv.pickDir()
    if (!path) return
    const result = await api.scanImageFolder(path)
    if (!result.total) { message.warning('文件夹里没有支持的图片'); return }
    folder.value = result.folder
    files.value = result.files.map((item) => item.path)
    selected.value = 0
    sample.value = null
    preview.value = null; renderedKey.value = ''; previewError.value = ''
    // Explicitly load the first page, even when reselecting the same folder after
    // a failed preview. Flush the path watcher before clearing its debounce timer.
    await nextTick()
    await refreshPreview()
  } catch (e) { message.error(String(e)) }
  finally { scanning.value = false }
}
async function pickOutput() {
  try { const path = await window.sv.pickDir(); if (path) outputDir.value = path }
  catch (e) { message.error(String(e)) }
}
async function refreshPreview() {
  clearTimeout(debounce)
  const seq = ++sequence
  const key = previewKey.value
  if (!currentPath.value) { preview.value = null; loading.value = false; return }
  loading.value = true; previewError.value = ''
  try {
    const result = await api.watermarkPreview(currentPath.value, { ...mask }, smartOptions.value)
    if (seq === sequence && !disposed && key === previewKey.value) {
      preview.value = result; renderedKey.value = key
    }
  } catch (e) {
    if (seq === sequence && !disposed) { previewError.value = String(e); renderedKey.value = '' }
  } finally { if (seq === sequence && !disposed) loading.value = false }
}
watch(previewKey, () => {
  ++sequence // Invalidate in-flight results immediately, including during debounce.
  loading.value = !!currentPath.value
  clearTimeout(debounce)
  if (!dragging.value) debounce = setTimeout(() => void refreshPreview(), 250)
})
watch(dragging, (value) => { if (!value) void refreshPreview() })

async function captureSample() {
  if (!canCapture.value) return
  sample.value = { path: currentPath.value, mask: { ...mask } }
  await nextTick()
  await refreshPreview()
  if (previewError.value) {
    const error = previewError.value
    sample.value = null
    message.warning(error)
  }
}
async function resetSample() {
  if (busy.value) return
  // Return to the captured source page rather than accidentally sampling another watermark.
  const index = sample.value ? files.value.indexOf(sample.value.path) : 0
  selected.value = Math.max(0, index)
  sample.value = null
  await nextTick(); await refreshPreview()
}

function changeUnit(unit: 'px' | 'percent') {
  if (unit === mask.unit) return
  if (preview.value) {
    const [sx, sy] = [preview.value.width / 100, preview.value.height / 100]
    const toPercent = unit === 'percent'
    const convert = (value: number, scale: number) => toPercent
      ? Math.round(value / scale * 1000) / 1000 : Math.round(value * scale)
    mask.width = Math.max(toPercent ? 0.001 : 1, convert(mask.width, sx))
    mask.height = Math.max(toPercent ? 0.001 : 1, convert(mask.height, sy))
    mask.right = convert(mask.right, sx); mask.bottom = convert(mask.bottom, sy)
  }
  mask.unit = unit
}

let origin: { x: number; y: number } | null = null
function point(e: PointerEvent) {
  const bounds = (e.currentTarget as HTMLElement).getBoundingClientRect()
  return { x: Math.max(0, Math.min(1, (e.clientX - bounds.left) / bounds.width)),
    y: Math.max(0, Math.min(1, (e.clientY - bounds.top) / bounds.height)) }
}
function beginDraw(e: PointerEvent) {
  if (regionLocked.value || !preview.value || e.button !== 0) return
  origin = point(e); dragging.value = true
  ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
}
function draw(e: PointerEvent) {
  if (!origin || !preview.value) return
  const end = point(e)
  const w = preview.value.width, h = preview.value.height
  const x1 = Math.min(w - 1, Math.floor(Math.min(origin.x, end.x) * w))
  const y1 = Math.min(h - 1, Math.floor(Math.min(origin.y, end.y) * h))
  const x2 = Math.min(w, Math.max(x1 + 1, Math.ceil(Math.max(origin.x, end.x) * w)))
  const y2 = Math.min(h, Math.max(y1 + 1, Math.ceil(Math.max(origin.y, end.y) * h)))
  const sx = mask.unit === 'percent' ? 100 / w : 1
  const sy = mask.unit === 'percent' ? 100 / h : 1
  mask.width = (x2 - x1) * sx; mask.height = (y2 - y1) * sy
  mask.right = (w - x2) * sx; mask.bottom = (h - y2) * sy
  // Immediate rectangle feedback; the actual processed preview refreshes on release.
  preview.value.box = [x1, y1, x2, y2]
}
function endDraw(e: PointerEvent) {
  if (!origin) return
  draw(e); origin = null; dragging.value = false
}

async function poll() {
  if (!job.value || disposed) return
  try {
    const result = await api.watermarkJob(job.value.id)
    if (disposed) return
    job.value = result; pollError.value = ''
    if (result.status === 'done') {
      if (result.failed || result.skipped) message.warning(`处理完成：成功 ${result.succeeded} 张，跳过 ${result.skipped ?? 0} 张，失败 ${result.failed} 张`)
      else message.success(`已完成 ${result.succeeded} 张图片`)
    }
  } catch (e) { if (!disposed) pollError.value = String(e) }
  if (!disposed && job.value?.status === 'running') polling = setTimeout(() => void poll(), 500)
}
async function start() {
  if (!canStart.value) return
  submitting.value = true
  try {
    job.value = await api.startWatermark({ paths: [...files.value], folder: folder.value || undefined,
      output_dir: outputDir.value || undefined, mask: { ...mask }, ...smartOptions.value })
    pollError.value = ''; void poll()
  } catch (e) { message.error(String(e)) }
  finally { submitting.value = false }
}
async function cancel() {
  if (!job.value) return
  cancelling.value = true
  try { job.value = await api.cancelWatermark(job.value.id) }
  catch (e) { message.error(String(e)) }
  finally { cancelling.value = false }
}
function openResults() { if (job.value) void window.sv.openPath(job.value.output_dir) }
onBeforeUnmount(() => { disposed = true; ++sequence; clearTimeout(debounce); clearTimeout(polling) })
</script>

<template>
  <div class="watermark-page">
    <header><h2>图片去水印</h2><p>支持白底、黑底水印和局部画面修补。保留原尺寸，结果另存为 PNG。</p></header>
    <section class="panel">
      <div class="row">
        <n-button :disabled="busy" @click="pickFiles">选择图片</n-button>
        <n-button :disabled="busy" :loading="scanning" @click="pickFolder">选择文件夹</n-button>
        <span class="muted">{{ files.length ? `已选择 ${files.length} 张图片` : '支持 JPG、PNG、WebP、BMP、TIFF；文件夹包含子目录' }}</span>
      </div>
      <div v-if="folder" class="path">{{ folder }}</div>
      <div v-if="files.length" class="row preview-select">
        <span>预览图片</span><n-select v-model:value="selected" :options="options" filterable :disabled="busy" />
        <span class="muted" v-if="preview">{{ preview.width }} × {{ preview.height }}</span>
      </div>
      <p v-if="folder && files.length" class="muted">默认以第一张图片作为示例，可切换其他图片检查区域。当前：{{ imageLabel(currentPath) }}</p>
    </section>
    <section class="panel">
      <div class="row mode-select"><strong>水印定位</strong>
        <n-select v-model:value="mode" :disabled="busy" :options="[{ label: '固定区域', value: 'fixed' }, { label: '智能定位水印', value: 'smart' }]" />
      </div>
      <div class="row mode-select"><strong>清除方式</strong>
        <n-select v-model:value="removal" :disabled="busy" :options="[{ label: '自动识别白底 / 黑底', value: 'auto' }, { label: '局部修补（含画面上的水印）', value: 'repair' }, { label: '固定填白', value: 'white' }]" />
      </div>
      <p v-if="removal === 'auto'" class="muted">自动判断纯色页边并填白或填黑。背景不均匀或碰到画面时跳过，可切换局部修补。</p>
      <p v-if="removal === 'repair'" class="muted">纯色页边直接清除，画面上的水印参考周围像素修补，可能留下模糊。智能定位按样本文字生成遮罩；固定区域会修补整个框，请尽量紧贴水印框选。</p>
      <div v-if="mode === 'smart' || removal === 'repair'" class="sample-panel">
        <p v-if="mode === 'smart'" class="muted">先从纯色页边框选完整水印及少量空白，设为样本。每张图自动在右下角搜索位置和大小；不同样式的水印需分批设置样本。匹配不可靠时跳过。</p>
        <p v-else class="muted">可选：从纯色页边上的同款水印设置样本，按文字形状生成遮罩，再切换到水印叠在画面上的图片检查。固定区域沿用框选位置；尺寸不同可先设置百分比。没有样本时修补整个框。</p>
        <div class="row">
          <n-button v-if="!sample" :disabled="!canCapture" @click="captureSample">设为水印样本</n-button>
          <template v-else><span class="muted">水印样本：{{ imageLabel(sample.path) }}</span><n-button size="small" :disabled="busy" @click="resetSample">重新框选样本</n-button></template>
        </div>
        <p v-if="preview?.detected === true && sample && mode === 'smart'" class="matched">已定位水印 · 匹配度 {{ ((preview.score ?? 0) * 100).toFixed(1) }}%</p>
        <p v-if="sample && mode === 'fixed'" class="matched">固定位置 · 使用样本文字遮罩</p>
        <p v-if="preview?.detected === false" class="error">{{ preview.reason }}。该张不会处理，可切换其他图片检查。</p>
      </div>
      <div class="row"><strong>{{ regionLocked && sample ? '样本框选参数' : '处理区域' }}</strong>
        <span class="muted">{{ regionLocked && sample ? '修改样本或区域请点击「重新框选样本」' : '以图片右下角为基准，也可在原图上拖动框选' }}</span></div>
      <div class="fields">
        <label>单位<n-select :value="mask.unit" :disabled="regionLocked" :options="[{ label: '像素（相同尺寸）', value: 'px' }, { label: '百分比（不同尺寸）', value: 'percent' }]" @update:value="changeUnit" /></label>
        <label>宽度<n-input-number v-model:value="mask.width" :min="mask.unit === 'px' ? 1 : 0.001" :max="mask.unit === 'percent' ? 100 : 100000" :precision="mask.unit === 'px' ? 0 : 3" :disabled="regionLocked" :update-value-on-input="false" /></label>
        <label>高度<n-input-number v-model:value="mask.height" :min="mask.unit === 'px' ? 1 : 0.001" :max="mask.unit === 'percent' ? 100 : 100000" :precision="mask.unit === 'px' ? 0 : 3" :disabled="regionLocked" :update-value-on-input="false" /></label>
        <label>距右边<n-input-number v-model:value="mask.right" :min="0" :max="mask.unit === 'percent' ? 100 : 100000" :precision="mask.unit === 'px' ? 0 : 3" :disabled="regionLocked" :update-value-on-input="false" /></label>
        <label>距底边<n-input-number v-model:value="mask.bottom" :min="0" :max="mask.unit === 'percent' ? 100 : 100000" :precision="mask.unit === 'px' ? 0 : 3" :disabled="regionLocked" :update-value-on-input="false" /></label>
      </div>
      <p class="muted">请在预览中检查水印是否清除，以及线稿和文字是否受到影响。默认区域 175 × 75 像素。</p>
      <p v-if="previewError" class="error">{{ previewError }}</p>
      <p v-if="mode === 'fixed' && preview?.detected === false" class="error">{{ preview.reason }}</p>
      <p v-if="loading" class="muted">正在生成预览…</p>
      <div v-if="preview" class="previews">
        <div><div class="caption">原图 · {{ mode === 'smart' && sample ? '自动定位结果' : '框内为处理区域' }}</div>
          <div class="image-stage" :class="{ locked: regionLocked }" @pointerdown="beginDraw" @pointermove="draw" @pointerup="endDraw" @pointercancel="endDraw">
            <img :src="preview.original" alt="去水印前" draggable="false" />
            <div v-if="preview.box" class="mask-box" :style="rectangle" />
          </div>
        </div>
        <div><div class="caption">处理后预览<span v-if="preview.method"> · {{ { white: '填白', black: '填黑', inpaint: '局部修补' }[preview.method] }}</span></div><div class="image-stage result"><img :src="preview.processed" alt="去水印后" draggable="false" /></div></div>
      </div>
      <div v-else-if="!loading" class="empty">选择图片后，查看前后效果并调整区域</div>
    </section>
    <section class="panel">
      <div class="row"><strong>输出位置</strong><n-button size="small" :disabled="busy" @click="pickOutput">选择目录</n-button>
        <n-button v-if="outputDir" size="small" :disabled="busy" @click="outputDir = ''">恢复默认</n-button></div>
      <p class="path">{{ outputDir || (folder ? '源文件夹旁边' : '第一张图片所在目录') }} · 自动创建去水印结果文件夹</p>
      <div class="row"><n-button type="primary" :disabled="!canStart" :loading="submitting" @click="start">批量去水印（{{ files.length }} 张）</n-button>
        <n-button v-if="active && job" :loading="cancelling" :disabled="cancelling" @click="cancel">停止处理</n-button>
        <span class="muted">原图保留；文件夹结构保留；同名结果自动避让</span></div>
    </section>
    <section v-if="job" class="panel">
      <div class="row"><strong>{{ job.status === 'running' ? '正在处理' : job.status === 'cancelled' ? '已停止' : '处理完成' }}</strong>
        <span>{{ job.completed }} / {{ job.total }} · 成功 {{ job.succeeded }} · 跳过 {{ job.skipped ?? 0 }} · 失败 {{ job.failed }} · {{ job.elapsed_s }} 秒</span>
        <n-button size="small" @click="openResults">打开结果文件夹</n-button></div>
      <n-progress type="line" :percentage="Math.round(job.completed / job.total * 100)" :show-indicator="false" />
      <p v-if="job.current" class="path">{{ job.current }}</p>
      <p v-if="pollError" class="error">进度获取失败，正在重试：{{ pollError }}</p>
      <details v-if="job.errors.length"><summary>查看跳过或失败原因（{{ job.errors.length }} 条）</summary><p v-for="item in job.errors" :key="item.path" class="error">{{ item.kind === 'skipped' ? '已跳过' : '失败' }} · {{ item.path }}：{{ item.error }}</p></details>
      <p v-if="job.status !== 'running' && (job.mode === 'smart' || (job.removal && job.removal !== 'white'))" class="muted">处理报告 watermark-report.json 已保存到结果文件夹。跳过的图片保留在源目录，不生成处理结果。</p>
    </section>
  </div>
</template>

<style scoped>
.watermark-page { max-width: 1200px; margin: 0 auto; padding: 24px; display: grid; gap: 18px; }
h2 { margin: 0 0 8px; font-size: 24px; } header p { margin: 0; color: var(--sv-text-dim); }
.panel { background: var(--sv-panel-grad); border: 1px solid var(--sv-border-soft); border-radius: 14px; padding: 20px; }
.row { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; }
.mode-select { margin-bottom: 18px; } .mode-select .n-select { width: 220px; }
.sample-panel { padding: 0 0 16px; margin-bottom: 18px; border-bottom: 1px solid var(--sv-border-soft); }
.matched { color: var(--sv-success); font-size: 13px; }
.muted { color: var(--sv-text-dim); font-size: 13px; } .path { font-size: 13px; color: var(--sv-text-dim); overflow-wrap: anywhere; margin-top: 12px; }
.preview-select { margin-top: 16px; } .preview-select .n-select { width: 320px; }
.fields { display: grid; grid-template-columns: 1.6fr repeat(4, 1fr); gap: 12px; margin-top: 18px; }
label { display: grid; gap: 8px; font-size: 13px; color: var(--sv-text-dim); min-width: 0; }
.previews { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; margin-top: 16px; }
.caption { color: var(--sv-text-dim); font-size: 13px; margin-bottom: 8px; }
.image-stage { position: relative; width: 100%; cursor: crosshair; touch-action: none; user-select: none; }
.image-stage.locked, .image-stage.result { cursor: default; }
.image-stage img { display: block; width: 100%; pointer-events: none; }
.mask-box { position: absolute; border: 2px solid var(--sv-accent); background: rgba(var(--sv-accent-rgb), .22); box-sizing: border-box; pointer-events: none; }
.empty { padding: 40px; text-align: center; color: var(--sv-text-faint); }
.error { color: var(--sv-danger); font-size: 13px; overflow-wrap: anywhere; }
.n-progress { margin-top: 16px; } details { margin-top: 12px; } summary { cursor: pointer; }
@media (max-width: 1050px) { .fields { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 700px) { .previews { grid-template-columns: 1fr; } }
</style>
