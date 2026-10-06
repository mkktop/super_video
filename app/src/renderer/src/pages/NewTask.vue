<script setup lang="ts">
import { modelCategoryLabel } from '../composables/modelCategories'
import { recommendationChoice } from '../composables/recommendationChoice'
import { computed, onActivated, onMounted, ref, watch } from 'vue'
import {
  NButton,
  NCard,
  NCollapse,
  NCollapseItem,
  NForm,
  NFormItem,
  NInput,
  NInputNumber,
  NPopover,
  NPopconfirm,
  NRadio,
  NRadioButton,
  NRadioGroup,
  NSelect,
  NSlider,
  NSwitch,
  NTag,
  useDialog,
  useMessage,
} from 'naive-ui'
import { api, mediaSrc, type ModelInfo, type ProbeInfo } from '../api'
import { refreshTasks, store, ui } from '../store'
import { useFileDrop, useRecentVideos } from '../composables/videoPicks'
import { DEFAULT_ANIME_MODEL } from '../composables/modelDefaults'
import { useModelOptions } from '../composables/useModelOptions'
import { useEncoderOptions } from '../composables/useEncoderOptions'
import { tileOptions, useCustomResolution } from '../composables/useCustomResolution'
import { SCENES, hasScene, sceneLabel } from '../composables/useModelOptions'
import { denoiseLabel } from '../composables/useModelOptions'

const message = useMessage()
const dialog = useDialog()

const inputs = ref<string[]>([])
const probeInfo = ref<ProbeInfo | null>(null)
const probing = ref(false)
let probeSeq = 0 // 快速连续重选文件时丢弃迟到的旧 probe 响应（旧数据覆盖新文件）
const thumbBroken = ref(false) // 首帧缩略图：浏览器解不了的编码（AVI/WMV/HEVC）时隐藏，参数条不受影响
const modelId = ref(DEFAULT_ANIME_MODEL)
const targetScale = ref(2)
const resMode = ref<'scale' | 'custom'>('scale')
const targetW = ref(0)
const targetH = ref(0)
const tileChoice = ref(0) // 0 = 模型默认
const outKind = ref<'video' | 'png' | 'jpg'>('video')
const codec = ref('h264')
const decoder = ref<'sw' | 'nvdec' | 'd3d11va'>('sw')
const crf = ref(18)
const container = ref<'mp4' | 'mkv' | 'mov'>('mp4')
const audioMode = ref('auto')
// 字幕默认保留（与音轨 auto 对齐）：批量模式不逐文件探测，开关根本不显示，
// 默认关会让 MKV→MKV 任务静默丢字幕（实测 BDRemux 双 PGS 轨全丢）
const keepSubtitles = ref(true)
const interp = ref<'off' | 'rife2x'>('off')
const denoise = ref<number | null>(null)
const deinterlace = ref(false) // 反交错（老 DVD/1080i 源；帧数不变，checkpoint 语义安全）
const deband = ref(false) // 去色带（动画夜空渐变常见）
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

let outputSeq = 0
async function autoFillOutput() {
  const seq = ++outputSeq
  if (inputs.value.length !== 1 || outputTouched.value || !probeInfo.value?.ok) return
  try {
    const result = await api.suggestOutput({
      input: inputs.value[0], model_id: modelId.value,
      params: buildCreateBody(inputs.value[0], false).params,
    })
    if (seq === outputSeq && !outputTouched.value) output.value = result.output
  } catch {
    // 预览失败不阻断创建：提交时后端会重新计算默认路径。
    if (seq === outputSeq && !outputTouched.value) output.value = ''
  }
}

// ---- 模型选择（场景筛选/已装优先/倍率与降噪选项，见 composables/useModelOptions） ----
const { scene, srModels, selectedModel, scaleOptions, interpOptions,
        denoiseOptions, hasDenoiseVariants, selectModel } = useModelOptions(modelId, targetScale)
watch([denoiseOptions, denoise], ([options, value]) => {
  if (value !== null && !options.some((option) => option.value === value)) {
    denoise.value = null
    message.info('当前模型或倍率不支持原降噪档位，已恢复默认配置')
  }
})

// ---- 智能推荐（probe 附带；源分析失败时无此块，卡片整张不出现） ----
const recommend = computed(() => probeInfo.value?.recommend ?? null)
const recommendationState = computed(() => recommendationChoice(recommend.value, store.models))
// 仅控制模型列表的呈现；推荐配置仍由用户显式应用。
const showAllModels = ref(false)
const featuredModels = computed(() => {
  const candidates = store.models.filter((m) => m.kind !== 'interp' && hasScene(m, 'video'))
  const priority = [recommend.value?.model_id, modelId.value]
  candidates.sort((a, b) => {
    const rank = (m: ModelInfo) => {
      const index = priority.indexOf(m.id)
      return index < 0 ? priority.length : index
    }
    return rank(a) - rank(b) || Number(b.vram_ok) - Number(a.vram_ok)
      || Number(!!(b.installed || b.bundled)) - Number(!!(a.installed || a.bundled))
  })
  // 保留通过预设选中的跨场景模型，让当前选择始终可见。
  const current = selectedModel.value
  if (current && !candidates.some((m) => m.id === current.id)) candidates.unshift(current)
  return candidates.slice(0, 3)
})
const visibleModels = computed(() => showAllModels.value ? srModels.value : featuredModels.value)
const recommendFlags = computed(() => {
  const r = recommend.value
  if (!r) return []
  const flags: string[] = []
  if (r.deinterlace) flags.push('反交错')
  if (r.deband) flags.push('去色带')
  return flags
})
function applyRecommendation() {
  const r = recommend.value
  if (!r) return
  const { model, error } = recommendationState.value
  if (!model || error || r.target_scale === null) {
    message.warning(error)
    return
  }
  if (!selectModel(model.id)) return
  targetScale.value = r.target_scale
  deinterlace.value = r.deinterlace
  deband.value = r.deband
  resMode.value = 'scale'
  message.success(`已应用推荐配置：${model.name} · x${targetScale.value}`)
  modelSec.value?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}
// ---- 编码/解码/音轨/字幕选项（设备能力与探测实测，见 composables/useEncoderOptions） ----
const { codecOptions, containerOptions, decoderOptions, audioOptions,
        srcSubs, subHint, audioHint, mediaFlags } = useEncoderOptions(
  probeInfo, container, audioMode, outKind)

const speedLabel = { fastest: '⚡', fast: '⚡', balanced: '⚖', slow: '🐢' } as Record<string, string>

const isImage = computed(() => outKind.value !== 'video')
const imgFrameHint = computed(() => {
  if (!isImage.value || !probeInfo.value?.ok) return ''
  const n = probeInfo.value.total_frames * (interp.value === 'rife2x' ? 2 : 1)
  return `将导出全部 ${n} 帧，按 000001.${outKind.value} 顺序编号`
})

// ---- 自定义分辨率（只缩不放纪律，见 composables/useCustomResolution） ----
const { customAvailable, srcW, srcH, maxScale, effW, effH,
        customScale, belowSrc, customOk, aspectNote } = useCustomResolution(
  inputs, probeInfo, selectedModel, targetScale, resMode, targetW, targetH)

// ---- 提交校验：单文件需探测成功，多文件批量直接放行（无逐文件探测） ----
const canSubmit = computed(
  () =>
    inputs.value.length > 0 &&
    (inputs.value.length > 1 || !!probeInfo.value?.ok) &&
    !!modelId.value &&
    !!selectedModel.value?.vram_ok &&
    !submitting.value &&
    !(resMode.value === 'custom' && !customOk.value),
)

// ---- 文件选择：最近输入与整页拖拽（与剪切页共用，见 composables/videoPicks） ----
const { recents, pushRecent } = useRecentVideos()
const { dragDepth, onDragEnter, onDragLeave, onDropFiles } = useFileDrop((vids) => setInput(vids))

async function setInput(files: string[]) {
  const seq = ++probeSeq
  ++outputSeq
  probing.value = false
  inputs.value = files
  probeInfo.value = null
  output.value = ''
  thumbBroken.value = false
  outputTouched.value = false // 新一轮选文件：恢复自动填充
  pushRecent(files)
  // 封装容器默认跟随源文件（.mkv→mkv 等；不认识的扩展名保持 mp4）
  const byExt: Record<string, 'mp4' | 'mkv' | 'mov'> = {
    '.mkv': 'mkv', '.webm': 'mkv', '.mp4': 'mp4', '.m4v': 'mp4', '.mov': 'mov',
  }
  container.value = byExt[files[0].slice(files[0].lastIndexOf('.')).toLowerCase()] ?? 'mp4'
  if (files.length === 1) {
    probing.value = true
    try {
      const r = await api.probe(files[0], true, true)
      if (seq !== probeSeq) return // 已重选其他文件：丢弃过期响应
      probing.value = false
      if (r.ok) {
        const info = (await r.json()) as ProbeInfo
        if (seq !== probeSeq) return
        probeInfo.value = info
        // 换文件后当前选的硬解可能不再支持（老编码/设备差异）：回落软解，避免带着无效值提交
        const d = probeInfo.value.decoder
        if (d && decoder.value !== 'sw' && !d[decoder.value]) decoder.value = 'sw'
      } else {
        const e = await r.json()
        if (seq !== probeSeq) return
        probeInfo.value = {
          ok: false, error: e.detail ?? `HTTP ${r.status}`,
          width: 0, height: 0, fps: 0, duration_s: 0, total_frames: 0,
          codec: '', pix_fmt: '', has_audio: false, audio_tracks: [], subtitles: [],
        }
      }
    } catch (e) {
      if (seq !== probeSeq) return
      message.error(`无法读取视频: ${e instanceof Error ? e.message : e}`)
    } finally {
      if (seq === probeSeq) probing.value = false
    }
    void autoFillOutput()
  }
}

async function pickInput() {
  const files = await window.sv.pickVideo()
  if (!files.length) return
  await setInput(files)
}

// 先消费跳转参数，再异步探测。挂载/激活与 watch 共用入口，首次直达也能预填。
function consumePendingWizard() {
  if (ui.page !== 'newtask') return
  const path = ui.pendingInput
  const mid = ui.pendingModel
  const want = ui.pendingScale
  ui.pendingInput = null
  ui.pendingModel = null
  ui.pendingScale = null
  if (mid) {
    const spec = store.models.find((m) => m.id === mid)
    if (spec?.vram_ok) {
      modelId.value = spec.id
      targetScale.value = want && spec.scale.includes(want) ? want : Math.min(...spec.scale)
    }
  }
  if (path) void setInput([path])
}
onMounted(consumePendingWizard)
onActivated(consumePendingWizard)
watch([() => ui.page, () => ui.pendingInput, () => ui.pendingModel], consumePendingWizard)

watch([targetScale, resMode, effW, effH, outKind, container, modelId,
       globalOutDir, () => store.settings.output_name_template], () => void autoFillOutput())

// 任务页「改参数重试」入口：带原任务全部参数进本页，调完重新入队
// （图片/漫画任务已分流到图片超分/漫画超分页，本页只接视频任务）。
// 双钩子+watch 靠置空幂等：首次直达只触发 onMounted，缓存页只触发
// onActivated——单挂 watch 会漏掉组件尚未创建的首次直达
async function consumeRetryParams() {
  if (ui.page !== 'newtask' || !ui.pendingTaskParams) return
  const t = ui.pendingTaskParams
  ui.pendingTaskParams = null
  const p = (t.params ?? {}) as Record<string, unknown>
  await setInput([t.input_path])
  // 模型与倍率（模型可能已被删除：找不到就保持空，用户手选）
  const spec = store.models.find((m) => m.id === t.model_id)
  if (spec) {
    modelId.value = spec.id
    const want = Number(p.target_scale ?? p.scale ?? 0)
    targetScale.value = want && spec.scale.includes(want) ? want : Math.min(...spec.scale)
  }
  const tw = p.target_w as number | undefined
  const th = p.target_h as number | undefined
  if (tw && th) {
    resMode.value = 'custom'
    targetW.value = tw
    targetH.value = th
  } else {
    resMode.value = 'scale'
  }
  if (p.out_kind === 'png' || p.out_kind === 'jpg') outKind.value = p.out_kind
  if (typeof p.codec === 'string') codec.value = p.codec
  if (p.decoder === 'sw' || p.decoder === 'nvdec' || p.decoder === 'd3d11va') {
    decoder.value = p.decoder
  }
  if (typeof p.crf === 'number') crf.value = Math.min(30, Math.max(12, p.crf))
  if (p.container === 'mp4' || p.container === 'mkv' || p.container === 'mov') {
    container.value = p.container
  }
  if (typeof p.audio_mode === 'string') audioMode.value = p.audio_mode
  keepSubtitles.value = p.subtitle_mode === 'auto'
  interp.value = p.interp === 'rife2x' ? 'rife2x' : 'off'
  denoise.value = typeof p.denoise === 'number' ? p.denoise : null
  deinterlace.value = p.deinterlace === true
  deband.value = p.deband === true
  tileChoice.value = typeof p.tile === 'number' ? p.tile : 0
  message.info('已带入原任务参数，调整后点「加入处理队列」')
}
onMounted(consumeRetryParams)
onActivated(consumeRetryParams)
watch([() => ui.page, () => ui.pendingTaskParams], () => void consumeRetryParams())

async function pickOutputFile() {
  if (isImage.value) {
    const p = await window.sv.pickDir()
    if (p) {
      output.value = p
      outputTouched.value = true // 用户显式选择的位置不再被自动填充覆盖
    }
    return
  }
  const p = await window.sv.pickOutput(output.value || `output.${container.value}`)
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
  container.value = p.container ?? 'mp4'
  audioMode.value = p.audio_mode ?? 'auto'
  // 旧预设不含字幕偏好：保持用户当前选择，不静默重置为关
  if (p.subtitle_mode === 'auto' || p.subtitle_mode === 'none') {
    keepSubtitles.value = p.subtitle_mode === 'auto'
  }
  interp.value = p.interp === 'rife2x' ? 'rife2x' : 'off'
  denoise.value = typeof p.denoise === 'number' ? p.denoise : null
  deinterlace.value = p.deinterlace === true
  deband.value = p.deband === true
  autoFillOutput()
  message.success(
    `已应用「${p.name}」：${selectedModel.value?.name ?? p.model_id} · x${p.target_scale}`,
  )
  // 原弹窗点预设会跳步骤给出反馈；整页后改为滚到模型区，让选中的卡片看得见
  modelSec.value?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

// ---- 用户自定义预设：把当前参数快照成一条可复用配置 ----
const savingPreset = ref(false)
const presetName = ref('')
const presetPopShow = ref(false)
async function saveAsPreset() {
  const name = presetName.value.trim()
  if (!name) {
    message.error('请先填写预设名称')
    return
  }
  if (!modelId.value || !selectedModel.value) {
    message.error('请先选择模型')
    return
  }
  savingPreset.value = true
  const r = await api.createPreset({
    name,
    model_id: modelId.value,
    target_scale: targetScale.value,
    codec: codec.value,
    crf: crf.value,
    container: container.value,
    audio_mode: audioMode.value,
    subtitle_mode: keepSubtitles.value ? 'auto' : 'none',
    interp: interp.value,
    denoise: denoise.value,
    deinterlace: deinterlace.value,
    deband: deband.value,
  })
  savingPreset.value = false
  if (r.ok) {
    store.presets = await api.presets().catch(() => store.presets)
    presetName.value = ''
    presetPopShow.value = false
    message.success(`已保存预设「${name}」，下次直接点选`)
  } else {
    message.error(`保存失败: ${(await r.json()).detail ?? r.status}`)
  }
}
async function deleteUserPreset(pid: string) {
  const p = store.presets.find((x) => x.id === pid)
  const r = await api.deletePreset(pid)
  if (r.ok) {
    store.presets = await api.presets().catch(() => store.presets)
    message.success(`已删除预设「${p?.name ?? pid}」`)
  } else {
    message.error(`删除失败: ${(await r.json()).detail ?? r.status}`)
  }
}

// ---- 提交 ----
/** 组装单个输入的创建请求体（overwrite 用于撞名确认后的重交） */
function buildCreateBody(input: string, overwrite: boolean) {
  const out = inputs.value.length === 1 && outputTouched.value ? output.value || undefined : undefined
  const scaleToSend = resMode.value === 'custom' ? customScale.value! : targetScale.value
  return {
    input,
    output: out,
    model_id: modelId.value,
    overwrite,
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
      decoder: decoder.value,
      ...(deinterlace.value ? { deinterlace: true } : {}),
      ...(deband.value ? { deband: true } : {}),
      ...(denoise.value !== null ? { denoise: denoise.value } : {}),
      ...(tileChoice.value ? { tile: tileChoice.value } : {}),
    },
  }
}

/** 已有非活动文件的 409 → 确认覆盖弹窗；活动任务冲突只提示改路径。 */
function confirmOverwrite(detail: string): Promise<boolean> {
  return new Promise((resolve) => {
    dialog.warning({
      title: '输出路径冲突',
      content: `${detail}。继续将覆盖该文件，确定吗？`,
      positiveText: '覆盖并继续',
      negativeText: '返回修改',
      onPositiveClick: () => resolve(true),
      onNegativeClick: () => resolve(false),
      onClose: () => resolve(false),
    })
  })
}

async function submit() {
  if (!canSubmit.value) return
  if (resMode.value === 'custom' && !customOk.value) {
    message.error('自定义分辨率参数无效，请检查目标宽高')
    return
  }
  submitting.value = true
  const submitted = [...inputs.value]
  const succeeded = new Set<string>()
  let lastErr = ''
  try {
    for (const input of submitted) {
      let r = await api.createTask(buildCreateBody(input, false))
      if (r.status === 409) {
        const detail = String((await r.json().catch(() => ({}))).detail ?? '输出路径冲突')
        if (detail.includes('已有排队/运行中的任务')) {
          message.error(detail)
          return
        }
        if (!(await confirmOverwrite(detail))) return
        r = await api.createTask(buildCreateBody(input, true))
      }
      if (r.ok) succeeded.add(input)
      else lastErr = `${(await r.json().catch(() => ({}))).detail ?? r.status}`
    }
    if (succeeded.size === submitted.length) {
      message.success(`已加入处理队列 ${succeeded.size} 个任务${selectedModel.value && !selectedModel.value.installed ? '（未安装的模型将在任务开始时下载）' : ''}`)
      reset()
      ui.page = 'tasks'
    } else {
      message.error(`创建失败: ${lastErr}${succeeded.size ? `（已有 ${succeeded.size} 个任务成功入队）` : ''}`)
    }
  } catch (e) {
    message.error(`创建失败: ${e instanceof Error ? e.message : e}${succeeded.size ? `（已有 ${succeeded.size} 个任务成功入队）` : ''}`)
  } finally {
    // 部分入队后仍保留失败项；再次提交不会重复创建已经成功的任务。
    if (succeeded.size) {
      inputs.value = inputs.value.filter((input) => !succeeded.has(input))
      void refreshTasks()
    }
    submitting.value = false
  }
}

// 清空本轮选择；编码/画质等输出偏好保留上次取值，连续建任务不用重设
function reset() {
  ++probeSeq
  ++outputSeq
  probing.value = false
  inputs.value = []
  probeInfo.value = null
  modelId.value = DEFAULT_ANIME_MODEL
  output.value = ''
  outputTouched.value = false
}

// ---- 效果预览（复用模型对比基建：片头 20s + 当前模型跑一版，看效果再决定入队） ----
const canTryRun = computed(
  () =>
    inputs.value.length === 1 &&
    !!probeInfo.value?.ok &&
    probeInfo.value.duration_s > 1 &&
    !!modelId.value &&
    !!selectedModel.value?.vram_ok &&
    (!!selectedModel.value?.installed || !!selectedModel.value?.bundled),
)
const tryRunHint = computed(() => {
  if (!inputs.value.length) return ''
  if (inputs.value.length > 1) return '效果预览仅支持单个视频；批量任务将统一使用当前参数处理'
  if (!probeInfo.value?.ok) return '读取视频信息后，可生成片头 20 秒效果预览'
  if (!selectedModel.value) return '选择模型后，可生成片头 20 秒效果预览'
  if (!selectedModel.value.installed && !selectedModel.value.bundled) {
    return '当前模型尚未下载；下载后可预览效果，加入队列时也会自动下载'
  }
  return `使用「${selectedModel.value.name}」生成片头 20 秒效果预览，检查画质与速度`
})
const tryRunTitle = computed(() => {
  if (!inputs.value.length) return ''
  if (inputs.value.length > 1) return '批量文件不支持效果预览'
  if (!probeInfo.value?.ok) return '等待视频信息读取完成'
  if (probeInfo.value.duration_s <= 1) return '视频过短，无需预览，可直接加入队列'
  if (!modelId.value || !selectedModel.value) return '请先选择模型'
  if (!selectedModel.value.vram_ok) return '当前模型超出显存，不可用'
  if (!selectedModel.value.installed && !selectedModel.value.bundled) return '模型未下载：下载后可预览（加入队列也会自动下载）'
  return '生成片头 20 秒效果预览，可拖动分割线对比原片'
})
function tryRun() {
  if (!canTryRun.value || !probeInfo.value || !modelId.value) return
  const dur = probeInfo.value.duration_s
  ui.pendingCompare = { input: inputs.value[0], start_s: 0, end_s: Math.min(20, dur) }
  ui.pendingModel = modelId.value
  ui.pendingScale = resMode.value === 'custom' ? customScale.value! : targetScale.value
  ui.page = 'mcompare'
}

const fmtDur = (s: number) => {
  // 先整体取整再拆分：避免 59.6s 显示成 "0分60秒"
  const t = Math.round(s)
  return `${Math.floor(t / 60)}分${t % 60}秒`
}
</script>

<script lang="ts">
// KeepAlive include 按名匹配：常驻保草稿
export default { name: 'NewTask' }
</script>

<template>
  <div
    class="newtask-page"
    @dragenter="onDragEnter"
    @dragover.prevent
    @dragleave="onDragLeave"
    @drop.prevent="onDropFiles"
  >
    <div v-if="dragDepth" class="drop-mask">
      <div class="drop-tip">松开鼠标即可导入视频</div>
    </div>
    <div class="page-head">
      <div>
        <h1>新建超分任务</h1>
        <p class="sub">导入视频，选择模型和输出方式，然后加入处理队列</p>
      </div>
    </div>

    <!-- 预设条：内置 + 用户自定义；当前参数可存为新预设 -->
    <div class="presets">
      <span class="presets-label">一键预设</span>
      <button
        v-for="p in store.presets"
        :key="p.id"
        class="preset"
        :class="{ 'preset-user': p.user }"
        :title="p.desc || p.name"
        @click="applyPreset(p.id)"
      >
        <span class="p-icon">{{ p.icon }}</span>{{ p.name }}
        <!-- v-if 放整个 NPopconfirm 上：trigger 槽留空会让 VBinder patch 崩溃（dev 必现） -->
        <NPopconfirm v-if="p.user" @positive-click="deleteUserPreset(p.id)">
          <template #trigger>
            <span
              class="p-del"
              title="删除此预设"
              @click.stop
            >×</span>
          </template>
          删除预设「{{ p.name }}」？
        </NPopconfirm>
      </button>
      <NPopover v-model:show="presetPopShow" trigger="click" placement="bottom-end">
        <template #trigger>
          <button class="preset preset-save" :disabled="!selectedModel" title="把当前参数保存为我的预设">＋ 存为预设</button>
        </template>
        <div class="preset-save-form">
          <NInput
            v-model:value="presetName"
            size="small"
            placeholder="预设名称"
            maxlength="24"
            style="width: 200px"
            @keyup.enter="saveAsPreset"
          />
          <NButton size="small" type="primary" :loading="savingPreset" @click="saveAsPreset">保存</NButton>
        </div>
        <div class="preset-save-hint">快照当前模型 / 倍率 / 编码画质 / 预处理选项</div>
      </NPopover>
    </div>

    <!-- ① 选择视频 -->
    <section class="sec sv-card input-sec" :class="{ 'input-empty': !inputs.length }">
      <h2 class="sec-title"><span class="sec-num">1</span>选择视频</h2>
      <button class="input-drop" @click="pickInput">
        <svg v-if="!inputs.length" aria-hidden="true" width="32" height="32" viewBox="0 0 32 32" fill="none">
          <rect x="4" y="5" width="24" height="22" rx="5" stroke="currentColor" stroke-width="1.5" />
          <path d="m13 11 8 5-8 5V11Z" fill="currentColor" />
        </svg>
        <strong>{{ inputs.length ? `已选 ${inputs.length} 个视频 · 点击重选` : '点击导入视频，或拖放到这里' }}</strong>
        <span v-if="!inputs.length">支持多选批量处理 · 导入后可查看素材信息与推荐配置</span>
      </button>
      <div v-if="!inputs.length && recents.length" class="recents">
        <span class="recents-label">最近：</span>
        <button
          v-for="p in recents"
          :key="p"
          class="recent-chip"
          :title="p"
          @click="setInput([p])"
        >
          {{ p.split(/[\\/]/).pop() }}
        </button>
      </div>
      <NCard v-if="probeInfo" size="small" class="probe-card" :bordered="true">
        <div v-if="probeInfo.ok" class="probe-flex">
          <video
            v-if="inputs.length === 1 && !thumbBroken"
            :src="mediaSrc(inputs[0]) + '#t=0.5'"
            preload="metadata"
            muted
            class="probe-thumb"
            @error="thumbBroken = true"
          />
          <div class="probe-grid">
            <span>分辨率 <b>{{ probeInfo.width }}x{{ probeInfo.height }}</b></span>
            <span>帧率 <b>{{ probeInfo.fps }}</b></span>
            <span>时长 <b>{{ fmtDur(probeInfo.duration_s) }}</b></span>
            <span>帧数 <b>{{ probeInfo.total_frames }}</b></span>
            <span>编码 <b>{{ probeInfo.codec }} / {{ probeInfo.pix_fmt }}</b></span>
            <span>音轨 <b>{{ probeInfo.has_audio ? '有' : '无' }}</b></span>
          </div>
        </div>
        <div v-if="mediaFlags.length" class="probe-flags">
          <span v-for="f in mediaFlags" :key="f" class="flag">{{ f }}</span>
        </div>
        <div v-if="!probeInfo.ok" class="probe-err">{{ probeInfo.error || '文件不可用' }}</div>
      </NCard>
      <!-- 智能推荐：源内容分析（动画/真人、隔行、老编码）→ 一键配好参数 -->
      <NCard v-if="recommend && probeInfo?.ok" size="small" class="rec-card" :bordered="true">
        <div class="rec-flex">
          <span class="rec-badge">素材推荐</span>
          <div class="rec-main">
            <div class="rec-title">
              <template v-if="recommend.animated !== null">
                {{ recommend.animated ? '素材倾向：动画' : '素材倾向：真人 / 实拍' }} ·
              </template>
              {{ recommend.model_name || '无可用推荐模型' }}<template v-if="recommend.model_id"> · ×{{ recommend.target_scale }}</template>
              <template v-if="recommendFlags.length">
                · 建议开启 {{ recommendFlags.join(' + ') }}
              </template>
            </div>
            <div class="rec-reasons">{{ recommend.reasons.join('；') }}</div>
            <div v-if="recommendationState.error" class="rec-unavailable" role="status">{{ recommendationState.error }}</div>
          </div>
          <NButton
            size="small"
            type="primary"
            secondary
            :disabled="!!recommendationState.error"
            :title="recommendationState.error || undefined"
            @click="applyRecommendation"
          >
            一键应用
          </NButton>
        </div>
      </NCard>
      <div v-else-if="probing" class="probe-skel">
        <div class="sv-skeleton skel-thumb" />
        <div class="skel-rows">
          <div class="sv-skeleton" style="height: 14px" />
          <div class="sv-skeleton" style="height: 14px" />
          <div class="sv-skeleton" style="height: 14px; width: 70%" />
        </div>
      </div>
    </section>

    <!-- ② 选择模型 -->
    <section ref="modelSec" class="sec sv-card">
      <h2 class="sec-title">
        <span class="sec-num">2</span>选择模型
        <span class="sel-chip" :class="{ on: !!selectedModel }">
          {{ selectedModel ? `已选 ${selectedModel.name} · x${targetScale}` : '点击卡片选择' }}
        </span>
      </h2>
      <div v-if="!inputs.length && !showAllModels" class="model-intro">
        <div><strong>先导入素材，再选择适合的模型</strong><p>导入后优先展示视频模型；单个视频分析完成后，可参考推荐配置。</p></div>
        <NButton size="small" @click="showAllModels = true">浏览全部模型</NButton>
      </div>
      <template v-else>
      <div class="model-toolbar">
        <p class="model-hint">{{ showAllModels ? '按场景筛选，点击卡片选择模型' : '优先展示视频模型与当前选择，可随时查看全部' }}</p>
        <NButton size="small" :aria-expanded="showAllModels" @click="showAllModels = !showAllModels">
          {{ showAllModels ? '收起全部模型' : '查看全部模型' }}
        </NButton>
      </div>
      <div v-if="showAllModels" class="scene-bar">
        <span class="scene-lbl">场景</span>
        <NButton v-for="s in ['all', ...SCENES]" :key="s" size="tiny" secondary
                 :type="scene === s ? 'primary' : 'default'" @click="scene = s as 'all'">
          {{ s === 'all' ? '全部' : sceneLabel[s] }}
        </NButton>
      </div>
      <div class="model-grid">
        <div
          v-for="m in visibleModels"
          :key="m.id"
          class="model-card"
          :class="{ selected: modelId === m.id, disabled: !m.vram_ok }"
          role="button"
          :tabindex="m.vram_ok ? 0 : -1"
          :aria-pressed="modelId === m.id"
          :aria-disabled="!m.vram_ok"
          @click="selectModel(m.id)"
          @keydown.enter="selectModel(m.id)"
          @keydown.space.prevent="selectModel(m.id)"
        >
          <span v-if="modelId === m.id" class="m-check">✓</span>
          <div class="m-head">
            <span class="m-name">{{ m.name }}</span>
            <NTag v-if="!m.installed && !m.bundled" size="tiny" :bordered="false" type="warning">需下载 {{ m.size_mb }}MB</NTag>
            <NTag v-if="!m.vram_ok" size="tiny" :bordered="false" type="error">显存不足</NTag>
            <span class="m-scenes">
              <NTag v-for="s in SCENES.filter((k) => hasScene(m, k))" :key="s"
                    size="tiny" type="info" :bordered="false">{{ sceneLabel[s] }}</NTag>
            </span>
          </div>
          <div class="m-desc">{{ m.description }}</div>
          <span v-if="recommend?.model_id === m.id" class="m-recommended">素材分析推荐</span>
          <div class="m-tags">
            <span>{{ speedLabel[m.speed] ?? '⚖' }}</span>
            <span>x{{ m.scale.join('/x') }}</span>
            <span>{{ m.vram_gb }}GB 显存</span>
            <span class="m-content">{{ modelCategoryLabel(m) }}</span>
          </div>
          <div v-if="m.vram_note" class="m-warn">{{ m.vram_note }}</div>
        </div>
      </div>
      <div v-if="inputs.length" class="model-preview-row">
        <div class="model-preview-copy">
          <strong>预览当前模型效果</strong>
          <span>{{ tryRunHint }}</span>
        </div>
        <NButton
          type="primary"
          secondary
          :disabled="!canTryRun"
          :title="tryRunTitle"
          @click="tryRun"
        >
          预览效果
        </NButton>
      </div>
      </template>
    </section>

    <!-- ③ 输出设置 -->
    <section v-if="inputs.length || showAllModels" class="sec sv-card">
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
                先按模型原生倍率 ×{{ customScale }} 放大，再缩放至 {{ effW }}x{{ effH }}（宽高自动取偶数）
              </span>
            </div>
            <NFormItem label="解码器">
              <div class="sub-col">
                <NSelect v-model:value="decoder" :options="decoderOptions" style="width: 300px" />
                <span class="sub-hint">硬件解码可减轻 CPU 负担。仅显示该视频可用的选项；运行时不可用会自动切回软件解码</span>
              </div>
            </NFormItem>
            <NFormItem v-if="outKind === 'video'" label="编码器">
              <NSelect v-model:value="codec" :options="codecOptions" style="width: 300px" />
            </NFormItem>
            <NFormItem v-if="outKind === 'video'" label="视频格式">
              <NSelect v-model:value="container" :options="containerOptions" style="width: 300px" />
            </NFormItem>
            <NFormItem v-if="outKind === 'video'" label="音轨">
              <div class="sub-col">
                <NSelect v-model:value="audioMode" :options="audioOptions" style="width: 300px" />
                <span v-if="audioHint" class="sub-hint">{{ audioHint }}</span>
              </div>
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
              <NSlider v-model:value="crf" :min="12" :max="30" :step="1" :marks="{ 14: '高画质', 18: '均衡', 24: '小体积' }" />
            </NFormItem>
            <NFormItem label="补帧">
              <NSelect v-model:value="interp" :options="interpOptions" style="width: 320px" />
              <NTag v-if="interp === 'rife2x' && probeInfo" size="small" :bordered="false" type="info" style="margin-left: 10px">
                {{ probeInfo.fps }} → {{ probeInfo.fps * 2 }} fps
              </NTag>
            </NFormItem>
            <NFormItem v-if="hasDenoiseVariants" label="降噪">
              <NSelect
                v-model:value="denoise"
                :options="denoiseOptions"
                style="width: 320px"
                clearable
                placeholder="使用默认降噪，可选择其他强度"
              />
            </NFormItem>
            <NFormItem label="预处理">
              <div class="sub-col">
                <div class="sub-row">
                  <NSwitch v-model:value="deinterlace" size="small" />
                  <span class="pre-label">反交错</span>
                  <span class="sub-hint">适用于老 DVD、1080i 等隔行视频，减少运动边缘的梳齿纹</span>
                </div>
                <div class="sub-row">
                  <NSwitch v-model:value="deband" size="small" />
                  <span class="pre-label">去色带</span>
                  <span class="sub-hint">减轻天空、暗部等渐变区域的色彩断层；可能损失细小纹理</span>
                </div>
              </div>
            </NFormItem>
            <NCollapse class="adv-collapse" :default-expanded-names="[]">
              <NCollapseItem title="高级选项" name="adv">
                <NFormItem label="分块大小" :show-feedback="false">
                  <NSelect v-model:value="tileChoice" :options="tileOptions" style="width: 200px" />
                </NFormItem>
                <div class="adv-note">
                  自动使用模型默认分块。显存不足时可调小；较小的分块通常更省显存，但处理更慢。
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
                {{ inputs.length }} 个视频将使用相同参数，按队列顺序处理，
                输出到「{{ batchDest }}」；可在 设置 → 输出位置 修改默认目录
              </span>
            </NFormItem>
          </div>
        </div>
      </NForm>
    </section>

    <!-- 吸底操作条：弱化清空，只保留一个主要提交动作 -->
    <div class="footer-bar sv-card">
      <button
        type="button"
        class="clear-action"
        :disabled="submitting || !inputs.length"
        @click="reset"
      >
        清空当前选择
      </button>
      <span class="footer-spacer" />
      <span v-if="inputs.length" class="footer-summary">
        {{ inputs.length === 1 ? '1 个视频已就绪' : `${inputs.length} 个视频将使用相同参数` }}
      </span>
      <NButton
        type="primary"
        :loading="submitting"
        :disabled="!canSubmit"
        @click="submit"
      >
        加入处理队列
      </NButton>
    </div>
  </div>
</template>

<style scoped>
.newtask-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-height: 100%;
}
h1 { font-size: 22px; font-weight: 600; letter-spacing: 0.3px; }
.sub { font-size: 12.5px; color: var(--sv-text-dim); margin-top: 4px; }

.presets {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  border: 1px solid rgba(var(--sv-accent-rgb), 0.18);
  border-radius: var(--sv-radius-md);
  background: linear-gradient(90deg, rgba(var(--sv-accent-rgb), 0.07), rgba(var(--sv-accent2-rgb), 0.05) 55%, transparent);
  flex-wrap: wrap;
}
.presets-label { font-size: 12px; color: var(--sv-text-dim); flex-shrink: 0; }
.preset {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 7px 14px;
  border-radius: 9px;
  border: 1px solid var(--sv-border-mid);
  background: var(--sv-fill-2);
  color: var(--sv-text);
  font-size: 13px;
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s, transform 0.15s;
}
.preset:hover {
  border-color: rgba(var(--sv-accent-rgb), 0.55);
  background: var(--sv-accent-bg);
  transform: translateY(-1px);
}
.p-icon { font-size: 14px; }
.p-del {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 15px;
  height: 15px;
  margin-left: 2px;
  border-radius: 50%;
  font-size: 12px;
  line-height: 1;
  color: var(--sv-text-dim);
  transition: color 0.15s, background 0.15s;
}
.p-del:hover { color: #fff; background: var(--sv-danger); }
.preset-user { border-style: dashed; }
.preset-save { border-style: dashed; color: var(--sv-text-dim); }
.preset-save:disabled { opacity: 0.45; cursor: not-allowed; }
.preset-save-form { display: flex; gap: 8px; align-items: center; }
.preset-save-hint { margin-top: 6px; font-size: 12px; color: var(--sv-text-dim); }

/* 智能推荐卡 */
.rec-card { background: linear-gradient(90deg, rgba(var(--sv-success-rgb), 0.05), rgba(var(--sv-accent-rgb), 0.04)); }
.rec-flex { display: flex; align-items: center; gap: 14px; }
.rec-badge {
  flex-shrink: 0;
  padding: 4px 10px;
  border-radius: 6px;
  background: linear-gradient(135deg, var(--sv-success), var(--sv-accent));
  color: #fff;
  font-size: 12px;
  font-weight: 600;
}
.rec-main { flex: 1; min-width: 0; }
.rec-unavailable { color: var(--sv-warning); font-size: 13px; margin-top: 6px; }
.rec-title { font-size: 13.5px; font-weight: 600; color: var(--sv-text); }
.rec-reasons {
  margin-top: 4px;
  font-size: 12px;
  color: var(--sv-text-faint);
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}
.pre-label { font-size: 13px; color: var(--sv-text); }

/* 步骤面板：每一步包进一块画布，层次立起来（底/描边/圆角由 .sv-card 提供） */
.sec {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px 18px;
}
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
  color: var(--sv-text-dim);
}
.sel-chip.on { color: var(--sv-accent-strong); }
.sec-num {
  width: 20px;
  height: 20px;
  border-radius: 6px;
  background: var(--sv-grad);
  color: #fff;
  font-size: 12px;
  font-weight: 600;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.probe-card { background: var(--sv-well); }
.probe-flex { display: flex; align-items: center; gap: 14px; }
.probe-thumb {
  width: 168px;
  aspect-ratio: 16 / 9;
  object-fit: contain;
  border-radius: 8px;
  border: 1px solid rgba(255, 255, 255, 0.08);
  background: var(--sv-panel-deep);
  flex-shrink: 0;
}
.probe-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 8px 16px;
  font-size: 13px;
  color: var(--sv-text-dim);
  min-width: 0;
  flex: 1;
}
.probe-grid b { color: var(--sv-text); font-weight: 600; margin-left: 4px; }
.probe-flags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 10px;
}
.flag {
  font-size: 12px;
  color: var(--sv-warning);
  background: var(--sv-warning-bg);
  border: 1px solid rgba(var(--sv-warning-rgb), 0.3);
  border-radius: 6px;
  padding: 2px 8px;
}
.probe-err { color: var(--sv-danger); font-size: 13px; }
.probe-skel {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 14px;
  border: 1px solid var(--sv-border-soft);
  border-radius: 10px;
}
.skel-thumb { width: 168px; aspect-ratio: 16 / 9; flex-shrink: 0; }
.skel-rows { flex: 1; display: flex; flex-direction: column; gap: 12px; }

.recents { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.recents-label { font-size: 12px; color: var(--sv-text-dim); }
.recent-chip {
  display: inline-flex;
  align-items: center;
  max-width: 240px;
  padding: 4px 12px;
  border-radius: 14px;
  border: 1px solid var(--sv-border-mid);
  background: var(--sv-fill-1);
  color: var(--sv-text-dim);
  font-size: 12.5px;
  cursor: pointer;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  transition: border-color 0.15s, color 0.15s;
}
.recent-chip:hover { border-color: rgba(var(--sv-accent-rgb), 0.55); color: var(--sv-text); }

/* 拖拽遮罩：拖文件进窗口时整页高亮 */
.drop-mask {
  position: fixed;
  inset: 0;
  z-index: 50;
  background: rgba(10, 12, 16, 0.78);
  border: 2px dashed rgba(var(--sv-accent-rgb), 0.75);
  display: flex;
  align-items: center;
  justify-content: center;
  pointer-events: none;
  box-shadow: inset 0 0 120px rgba(var(--sv-accent-rgb), 0.12);
}
.drop-tip {
  font-size: 18px;
  font-weight: 650;
  color: var(--sv-text);
  letter-spacing: 1px;
  padding: 14px 28px;
  border-radius: var(--sv-radius-md);
  border: 1px solid rgba(var(--sv-accent-rgb), 0.45);
  background: var(--sv-accent-bg);
  box-shadow: 0 0 40px rgba(var(--sv-accent-rgb), 0.2);
}

.model-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px; }
.model-card {
  position: relative;
  border: 1.5px solid var(--sv-border-mid);
  border-radius: var(--sv-radius-md);
  padding: 14px;
  cursor: pointer;
  background: var(--sv-fill-1);
  transition: border-color 0.16s, background 0.16s, transform 0.16s, box-shadow 0.16s;
}
.model-card:hover { border-color: var(--sv-border-strong); transform: translateY(-2px); }
.model-card.selected {
  border-color: var(--sv-accent);
  background: var(--sv-accent-bg);
}
.model-card.disabled { opacity: 0.75; cursor: not-allowed; }
.m-check {
  position: absolute;
  top: 10px;
  right: 10px;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: var(--sv-grad);
  color: #fff;
  font-size: 11px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.m-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; padding-right: 18px; }
.m-scenes { margin-left: auto; display: inline-flex; gap: 4px; }
.scene-bar { display: flex; align-items: center; gap: 6px; margin: 0 0 10px; }
.scene-lbl { font-size: 12px; color: var(--sv-text-dim); }
.m-name { font-weight: 650; font-size: 14px; }
.m-desc { color: var(--sv-text-dim); font-size: 13px; line-height: 1.65; margin: 8px 0 12px; }
.m-tags { display: flex; gap: 10px; font-size: 12.5px; color: var(--sv-text-dim); flex-wrap: wrap; }
.m-content {
  color: var(--sv-text-dim);
}
.m-warn { margin-top: 6px; font-size: 12.5px; color: var(--sv-danger); }
.model-card:focus-visible { outline: 2px solid var(--sv-accent); outline-offset: 3px; }
.input-drop { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 10px; padding: 20px; border: 1px dashed var(--sv-border-strong); border-radius: var(--sv-radius-md); background: var(--sv-fill-1); color: var(--sv-text); cursor: pointer; font: inherit; transition: border-color 160ms, background 160ms; }
.input-empty .input-drop { min-height: 175px; }
.input-drop svg { color: var(--sv-accent-strong); margin-bottom: 4px; }
.input-drop strong { font-size: 16px; font-weight: 600; }
.input-drop span { color: var(--sv-text-dim); font-size: 13px; }
.input-drop:hover { border-color: var(--sv-accent); background: var(--sv-accent-bg); }
.model-intro, .model-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
.model-intro { padding: 10px 0; }
.model-intro strong { font-size: 14px; font-weight: 500; }
.model-intro p, .model-hint { color: var(--sv-text-dim); font-size: 13px; line-height: 1.65; margin-top: 6px; }
.m-recommended { display: inline-block; margin-bottom: 10px; font-size: 12.5px; color: var(--sv-accent-strong); }
.model-preview-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 12px 14px;
  border: 1px solid var(--sv-border-soft);
  border-radius: 10px;
  background: var(--sv-well);
}
.model-preview-copy {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}
.model-preview-copy strong { font-size: 13px; font-weight: 600; color: var(--sv-text); }
.model-preview-copy span { font-size: 12px; line-height: 1.5; color: var(--sv-text-dim); }

/* 输出设置两列；窄窗口（内容宽 <752px）自动退化单列 */
.out-cols {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
  column-gap: 36px;
}
.out-col { display: flex; flex-direction: column; }

.batch-note { font-size: 12.5px; color: var(--sv-text-dim); }
.res-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.sub-row { display: flex; align-items: center; gap: 10px; }
.sub-col { display: flex; flex-direction: column; gap: 4px; }
.sub-hint { font-size: 12px; color: var(--sv-text-dim); }
.img-hint { margin-left: 12px; font-size: 12px; color: var(--sv-text-dim); }
.res-x { color: var(--sv-text-dim); }
.res-hints { font-size: 12px; margin: -6px 0 2px 102px; min-height: 16px; }
.res-err { color: var(--sv-danger); }
.res-warn { color: var(--sv-warning); }
.res-ok { color: var(--sv-text-dim); }
.adv-collapse { border: none; }
.adv-collapse :deep(.n-collapse-item__header) { padding: 8px 0 0; }
.adv-note { font-size: 12px; color: var(--sv-text-dim); margin-top: 6px; }

/* 吸底操作条：滚动时贴住可视区底部，内容不足一屏时沉到页底 */
.footer-bar {
  position: sticky;
  bottom: 0;
  margin-top: auto;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  padding: 12px 16px;
  background: var(--sv-panel-2);
  border: 1px solid var(--sv-border-mid);
  border-radius: var(--sv-radius-md);
  box-shadow: var(--sv-card-inset), 0 -6px 24px rgba(0, 0, 0, 0.3), 0 8px 22px rgba(0, 0, 0, 0.25);
  z-index: 5;
  flex-wrap: wrap;
}
.clear-action {
  appearance: none;
  border: 0;
  background: transparent;
  color: var(--sv-text-dim);
  padding: 6px 2px;
  font: inherit;
  font-size: 12.5px;
  cursor: pointer;
  transition: color 0.15s;
}
.clear-action:hover:not(:disabled) { color: var(--sv-text); }
.clear-action:disabled { opacity: 0.38; cursor: default; }
.footer-summary { font-size: 12px; color: var(--sv-text-dim); }
.footer-spacer { flex: 1; }
</style>
