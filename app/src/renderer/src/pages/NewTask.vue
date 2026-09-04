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
const modelId = ref('')
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

function joinDefault(dir: string, name: string): string {
  return `${dir.replace(/[\\/]+$/, '')}\\${name}`
}

/** 排队/运行中任务将写入的输出路径集合（小写归一）：预填命名避让用，
 *  与后端创建期的活动任务播种同口径（A 还没跑、B 预填同名会互相覆盖） */
const activeOuts = computed(() =>
  new Set(
    store.tasks
      .filter((t) => t.status === 'queued' || t.status === 'running')
      .map((t) => t.output_path.toLowerCase()),
  ),
)

/** 后端 _sr_output_name 的镜像：目录无同名 → 沿用原名；同名（含源文件本身与
 *  尚未开跑的活动任务）→ _倍率 后缀（也被占用再退 _2 序号）。异步查存在性，
 *  表单预填与实际创建保持一致。 */
async function defaultOutputName(stem: string, fmt: string, srcPath: string): Promise<string> {
  const suffix =
    resMode.value === 'custom' ? `${effW.value}x${effH.value}` : `${targetScale.value}x`
  const outDir = globalOutDir.value || srcPath.replace(/[\\/][^\\/]*$/, '')
  const plain = joinDefault(outDir, `${stem}.${fmt}`)
  // 与源同路径（同目录同扩展名）时"同名文件"就是源本身，必须保后缀
  if (
    plain.toLowerCase() !== srcPath.toLowerCase() &&
    !activeOuts.value.has(plain.toLowerCase()) &&
    !(await window.sv.fsExists(plain))
  ) {
    return plain
  }
  const suffixed = joinDefault(outDir, `${stem}_${suffix}.${fmt}`)
  if (!activeOuts.value.has(suffixed.toLowerCase())) return suffixed
  return joinDefault(outDir, `${stem}_${suffix}_2.${fmt}`)
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

// ---- 模型选择（场景筛选/已装优先/倍率与降噪选项，见 composables/useModelOptions） ----
const { scene, srModels, selectedModel, scaleOptions, interpOptions,
        denoiseOptions, hasDenoiseVariants, selectModel } = useModelOptions(modelId, targetScale)

// ---- 智能推荐（probe 附带；源分析失败时无此块，卡片整张不出现） ----
const recommend = computed(() => probeInfo.value?.recommend ?? null)
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
  if (r.model_id) selectModel(r.model_id)
  const spec = r.model_id ? store.models.find((m) => m.id === r.model_id) : null
  if (r.target_scale && (!spec || spec.scale.includes(r.target_scale))) {
    targetScale.value = r.target_scale
  }
  deinterlace.value = r.deinterlace
  deband.value = r.deband
  resMode.value = 'scale'
  message.success(`已应用推荐配置：${r.model_name || '模型'} · x${r.target_scale}`)
  modelSec.value?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}
// ---- 编码/解码/音轨/字幕选项（设备能力与探测实测，见 composables/useEncoderOptions） ----
const { codecOptions, containerOptions, decoderOptions, audioOptions,
        srcSubs, subHint, audioHint, mediaFlags } = useEncoderOptions(
  probeInfo, container, audioMode, outKind)

const speedLabel = { fast: '⚡', balanced: '⚖', slow: '🐢' } as Record<string, string>
const contentLabel = { anime: '动漫', comic: '漫画', general: '真人/通用', real: '真人/通用' } as Record<string, string>

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
    !(resMode.value === 'custom' && !customOk.value),
)

// ---- 文件选择：最近输入与整页拖拽（与剪切页共用，见 composables/videoPicks） ----
const { recents, pushRecent } = useRecentVideos()
const { dragDepth, onDragEnter, onDragLeave, onDropFiles } = useFileDrop((vids) => setInput(vids))

async function setInput(files: string[]) {
  const seq = ++probeSeq
  inputs.value = files
  probeInfo.value = null
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
    const r = await api.probe(files[0], true, true)
    if (seq !== probeSeq) return // 已重选其他文件：丢弃过期响应
    probing.value = false
    if (r.ok) {
      probeInfo.value = (await r.json()) as ProbeInfo
      // 换文件后当前选的硬解可能不再支持（老编码/设备差异）：回落软解，避免带着无效值提交
      const d = probeInfo.value.decoder
      if (d && decoder.value !== 'sw' && !d[decoder.value]) decoder.value = 'sw'
    } else {
      const e = await r.json()
      probeInfo.value = {
        ok: false, error: e.detail ?? `HTTP ${r.status}`,
        width: 0, height: 0, fps: 0, duration_s: 0, total_frames: 0,
        codec: '', pix_fmt: '', has_audio: false, audio_tracks: [], subtitles: [],
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

// 任务页「改参数重试」入口：带原任务全部参数进本页，调完重新入队
watch([() => ui.page, () => ui.pendingTaskParams], async () => {
  if (ui.page !== 'newtask' || !ui.pendingTaskParams) return
  const t = ui.pendingTaskParams
  ui.pendingTaskParams = null
  const p = (t.params ?? {}) as Record<string, unknown>
  // 图片批量任务的输入在 params.images 里
  const imgs = p.images as { in: string }[] | undefined
  await setInput(Array.isArray(imgs) && imgs.length ? imgs.map((x) => x.in) : [t.input_path])
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
  message.info('已带入原任务参数，调整后点「加入队列」')
})

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
  const out = inputs.value.length === 1 ? output.value || undefined : undefined
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

/** 409（输出文件已存在/撞活动任务）→ 确认覆盖弹窗；resolve false=用户放弃保持表单 */
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
  let ok = 0
  let lastErr = ''
  for (const input of inputs.value) {
    let r = await api.createTask(buildCreateBody(input, false))
    if (r.status === 409) {
      // 仅显式指定输出路径会 409（自动命名有后缀避让）：确认后带 overwrite 重交
      const detail = (await r.json().catch(() => ({}))).detail ?? '输出路径冲突'
      submitting.value = false
      if (!(await confirmOverwrite(String(detail)))) return
      submitting.value = true
      r = await api.createTask(buildCreateBody(input, true))
    }
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

// ---- 先试跑 20 秒（复用模型对比基建：片头 20s + 当前模型跑一版，看效果再决定入队） ----
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
  if (inputs.value.length !== 1 || !probeInfo.value?.ok || !selectedModel.value) return ''
  return `先用「${selectedModel.value.name}」跑片头 20 秒看效果与速度？满意再入队全片`
})
const tryRunTitle = computed(() => {
  if (!inputs.value.length) return ''
  if (inputs.value.length > 1) return '批量文件不支持试跑'
  if (!probeInfo.value?.ok) return '等待视频信息读取完成'
  if (probeInfo.value.duration_s <= 1) return '视频过短，直接入队即可'
  if (!modelId.value || !selectedModel.value) return '请先选择模型'
  if (!selectedModel.value.vram_ok) return '当前模型超出显存，不可用'
  if (!selectedModel.value.installed && !selectedModel.value.bundled) return '模型未下载：先下载（入队也会自动下载）'
  return '用当前模型对片头 20 秒快速跑一版，可拖动分割线对比原片'
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
      <div class="drop-tip">松开即可选入视频文件</div>
    </div>
    <div class="page-head">
      <div>
        <h1>新建超分任务</h1>
        <p class="sub">选择视频与模型 → 配置输出 → 加入队列串行处理</p>
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
    <section class="sec">
      <h2 class="sec-title"><span class="sec-num">1</span>选择视频</h2>
      <NButton dashed block size="large" @click="pickInput">
        {{ inputs.length ? `已选 ${inputs.length} 个文件（点击重选）` : '点击选择视频文件（可多选批量入队，也可直接拖进窗口）' }}
      </NButton>
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
          <span class="rec-badge">智能推荐</span>
          <div class="rec-main">
            <div class="rec-title">
              <template v-if="recommend.animated !== null">
                {{ recommend.animated ? '检测到动画内容' : '检测到真人/实拍内容' }} ·
              </template>
              {{ recommend.model_name || '无可用推荐模型' }} x{{ recommend.target_scale }}
              <template v-if="recommendFlags.length">
                · 建议开启 {{ recommendFlags.join(' + ') }}
              </template>
            </div>
            <div class="rec-reasons">{{ recommend.reasons.join('；') }}</div>
          </div>
          <NButton
            size="small"
            type="primary"
            secondary
            :disabled="!recommend.model_id"
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
    <section ref="modelSec" class="sec">
      <h2 class="sec-title">
        <span class="sec-num">2</span>选择模型
        <span class="sel-chip" :class="{ on: !!selectedModel }">
          {{ selectedModel ? `已选 ${selectedModel.name} · x${targetScale}` : '点击卡片选择' }}
        </span>
      </h2>
      <div class="scene-bar">
        <span class="scene-lbl">场景</span>
        <NButton v-for="s in ['all', ...SCENES]" :key="s" size="tiny" secondary
                 :type="scene === s ? 'primary' : 'default'" @click="scene = s as 'all'">
          {{ s === 'all' ? '全部' : sceneLabel[s] }}
        </NButton>
      </div>
      <div class="model-grid">
        <div
          v-for="m in srModels"
          :key="m.id"
          class="model-card"
          :class="{ selected: modelId === m.id, disabled: !m.vram_ok }"
          @click="selectModel(m.id)"
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
          <div class="m-tags">
            <span>{{ speedLabel[m.speed] ?? '⚖' }}</span>
            <span>x{{ m.scale.join('/x') }}</span>
            <span>{{ m.vram_gb }}GB 显存</span>
            <span v-for="c in m.content" :key="c" class="m-content">{{ contentLabel[c] ?? c }}</span>
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
            <NFormItem label="解码器">
              <div class="sub-col">
                <NSelect v-model:value="decoder" :options="decoderOptions" style="width: 300px" />
                <span class="sub-hint">硬解可加快解码速度；按所选视频实测支持情况开放，运行时不可用自动回退软件解码</span>
              </div>
            </NFormItem>
            <NFormItem v-if="outKind === 'video'" label="编码器">
              <NSelect v-model:value="codec" :options="codecOptions" style="width: 300px" />
            </NFormItem>
            <NFormItem v-if="outKind === 'video'" label="封装容器">
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
              <NSlider v-model:value="crf" :min="12" :max="30" :step="1" :marks="{ 14: '近无损', 18: '推荐', 24: '小体积' }" />
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
                placeholder="默认保守模式（点此可选档位，可清空恢复默认）"
              />
            </NFormItem>
            <NFormItem label="预处理">
              <div class="sub-col">
                <div class="sub-row">
                  <NSwitch v-model:value="deinterlace" size="small" />
                  <span class="pre-label">反交错</span>
                  <span class="sub-hint">老 DVD / 1080i 隔行片源——交错纹路不先去掉会被超分放大成锯齿</span>
                </div>
                <div class="sub-row">
                  <NSwitch v-model:value="deband" size="small" />
                  <span class="pre-label">去色带</span>
                  <span class="sub-hint">修复夜空/暗部渐变里的色彩断层（动画源常见）</span>
                </div>
              </div>
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
      <span class="hint-inline tryrun-hint">
        {{ tryRunHint }}
      </span>
      <span class="footer-spacer" />
      <NButton :disabled="submitting" @click="reset">清空</NButton>
      <NButton
        :disabled="!canTryRun"
        :title="tryRunTitle"
        @click="tryRun"
      >
        ▶ 先试跑 20 秒
      </NButton>
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
  gap: 16px;
  min-height: 100%;
}
h1 { font-size: 21px; font-weight: 750; letter-spacing: 0.3px; }
.sub { font-size: 12.5px; color: #9aa1ad; margin-top: 4px; }

.presets {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  border: 1px solid rgba(79, 140, 255, 0.18);
  border-radius: 12px;
  background: linear-gradient(90deg, rgba(79, 140, 255, 0.07), rgba(139, 92, 246, 0.05) 55%, transparent);
  flex-wrap: wrap;
}
.presets-label { font-size: 12px; color: #9aa1ad; flex-shrink: 0; }
.preset {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 7px 14px;
  border-radius: 9px;
  border: 1px solid rgba(255, 255, 255, 0.08);
  background: rgba(255, 255, 255, 0.035);
  color: #e9ecf2;
  font-size: 13px;
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s, transform 0.15s;
}
.preset:hover {
  border-color: rgba(79, 140, 255, 0.55);
  background: rgba(79, 140, 255, 0.1);
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
  color: #9aa1ad;
  transition: color 0.15s, background 0.15s;
}
.p-del:hover { color: #fff; background: #e05252; }
.preset-user { border-style: dashed; }
.preset-save { border-style: dashed; color: #9aa1ad; }
.preset-save:disabled { opacity: 0.45; cursor: not-allowed; }
.preset-save-form { display: flex; gap: 8px; align-items: center; }
.preset-save-hint { margin-top: 6px; font-size: 12px; color: #9aa1ad; }

/* 智能推荐卡 */
.rec-card { background: linear-gradient(90deg, rgba(34, 197, 94, 0.05), rgba(79, 140, 255, 0.04)); }
.rec-flex { display: flex; align-items: center; gap: 14px; }
.rec-badge {
  flex-shrink: 0;
  padding: 4px 10px;
  border-radius: 6px;
  background: linear-gradient(135deg, #22c55e, #4f8cff);
  color: #fff;
  font-size: 12px;
  font-weight: 600;
}
.rec-main { flex: 1; min-width: 0; }
.rec-title { font-size: 13.5px; font-weight: 600; color: #e8eaed; }
.rec-reasons {
  margin-top: 4px;
  font-size: 12px;
  color: #9aa0a6;
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}
.pre-label { font-size: 13px; color: #e8eaed; }

/* 步骤面板：每一步包进一块画布，层次立起来 */
.sec {
  display: flex;
  flex-direction: column;
  gap: 12px;
  background: linear-gradient(180deg, #1c2027, #181b21);
  border: 1px solid rgba(255, 255, 255, 0.06);
  border-radius: 14px;
  padding: 16px 18px;
}
.sec-title {
  font-size: 15px;
  font-weight: 650;
  display: flex;
  align-items: center;
  gap: 8px;
}
.sel-chip {
  margin-left: auto;
  font-size: 12px;
  font-weight: 400;
  color: #9aa1ad;
}
.sel-chip.on { color: #6fa0ff; }
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
  box-shadow: 0 0 10px rgba(79, 140, 255, 0.3);
}

.probe-card { background: rgba(0, 0, 0, 0.18); }
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
  color: #9aa1ad;
  min-width: 0;
  flex: 1;
}
.probe-grid b { color: #e9ecf2; font-weight: 600; margin-left: 4px; }
.probe-flags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 10px;
}
.flag {
  font-size: 12px;
  color: #fbbf24;
  background: rgba(251, 191, 36, 0.09);
  border: 1px solid rgba(251, 191, 36, 0.3);
  border-radius: 6px;
  padding: 2px 8px;
}
.probe-err { color: #f87171; font-size: 13px; }
.probe-skel {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 14px;
  border: 1px solid rgba(255, 255, 255, 0.06);
  border-radius: 10px;
}
.skel-thumb { width: 168px; aspect-ratio: 16 / 9; flex-shrink: 0; }
.skel-rows { flex: 1; display: flex; flex-direction: column; gap: 12px; }

.recents { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.recents-label { font-size: 12px; color: #9aa1ad; }
.recent-chip {
  display: inline-flex;
  align-items: center;
  max-width: 240px;
  padding: 4px 12px;
  border-radius: 14px;
  border: 1px solid rgba(255, 255, 255, 0.08);
  background: rgba(255, 255, 255, 0.03);
  color: #c6cbd4;
  font-size: 12.5px;
  cursor: pointer;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  transition: border-color 0.15s, color 0.15s;
}
.recent-chip:hover { border-color: rgba(79, 140, 255, 0.55); color: #fff; }

/* 拖拽遮罩：拖文件进窗口时整页高亮 */
.drop-mask {
  position: fixed;
  inset: 0;
  z-index: 50;
  background: rgba(10, 12, 16, 0.78);
  border: 2px dashed rgba(79, 140, 255, 0.75);
  display: flex;
  align-items: center;
  justify-content: center;
  pointer-events: none;
  box-shadow: inset 0 0 120px rgba(79, 140, 255, 0.12);
}
.drop-tip {
  font-size: 18px;
  font-weight: 650;
  color: #e9ecf2;
  letter-spacing: 1px;
  padding: 14px 28px;
  border-radius: 14px;
  border: 1px solid rgba(79, 140, 255, 0.45);
  background: rgba(79, 140, 255, 0.08);
  box-shadow: 0 0 40px rgba(79, 140, 255, 0.2);
}

.model-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px; }
.model-card {
  position: relative;
  border: 1.5px solid rgba(255, 255, 255, 0.07);
  border-radius: 12px;
  padding: 14px;
  cursor: pointer;
  background: rgba(255, 255, 255, 0.02);
  transition: border-color 0.16s, background 0.16s, transform 0.16s, box-shadow 0.16s;
}
.model-card:hover { border-color: rgba(255, 255, 255, 0.16); transform: translateY(-2px); }
.model-card.selected {
  border-color: #4f8cff;
  background: linear-gradient(180deg, rgba(79, 140, 255, 0.1), rgba(139, 92, 246, 0.05));
  box-shadow: 0 0 0 1px rgba(79, 140, 255, 0.45), 0 6px 18px rgba(79, 140, 255, 0.16);
}
.model-card.disabled { opacity: 0.45; cursor: not-allowed; }
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
  box-shadow: 0 0 10px rgba(79, 140, 255, 0.5);
}
.m-head { display: flex; align-items: center; gap: 8px; }
.m-scenes { margin-left: auto; display: inline-flex; gap: 4px; }
.scene-bar { display: flex; align-items: center; gap: 6px; margin: 0 0 10px; }
.scene-lbl { font-size: 12px; color: #9aa1ad; }
.m-name { font-weight: 650; font-size: 14px; }
.m-desc { color: #9aa1ad; font-size: 12px; margin: 6px 0; }
.m-tags { display: flex; gap: 10px; font-size: 12px; color: #7c838f; flex-wrap: wrap; }
.m-content {
  color: #8fa3c8;
}
.m-warn { margin-top: 6px; font-size: 11.5px; color: #f87171; }

/* 输出设置两列；窄窗口（内容宽 <752px）自动退化单列 */
.out-cols {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
  column-gap: 36px;
}
.out-col { display: flex; flex-direction: column; }

.batch-note { font-size: 12.5px; color: #9aa1ad; }
.res-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.sub-row { display: flex; align-items: center; gap: 10px; }
.sub-col { display: flex; flex-direction: column; gap: 4px; }
.sub-hint { font-size: 12px; color: #9aa1ad; }
.img-hint { margin-left: 12px; font-size: 12px; color: #9aa1ad; }
.res-x { color: #9aa1ad; }
.res-hints { font-size: 12px; margin: -6px 0 2px 102px; min-height: 16px; }
.res-err { color: #f87171; }
.res-warn { color: #fbbf24; }
.res-ok { color: #9aa1ad; }
.adv-collapse { border: none; }
.adv-collapse :deep(.n-collapse-item__header) { padding: 8px 0 0; }
.adv-note { font-size: 12px; color: #9aa1ad; margin-top: 6px; }

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
  background: rgba(20, 23, 29, 0.88);
  border: 1px solid rgba(255, 255, 255, 0.07);
  border-radius: 14px;
  box-shadow: 0 -6px 24px rgba(0, 0, 0, 0.3), 0 8px 22px rgba(0, 0, 0, 0.25);
  z-index: 5;
  flex-wrap: wrap;
}
.tryrun-hint { font-size: 12px; color: #6fa0ff; }
.footer-spacer { flex: 1; }
</style>
