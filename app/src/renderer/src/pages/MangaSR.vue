<script setup lang="ts">
import { computed, onActivated, onMounted, ref, watch } from 'vue'
import {
  NButton,
  NCheckbox,
  NRadioButton,
  NRadioGroup,
  NSelect,
  NSlider,
  NTag,
  useMessage,
} from 'naive-ui'
import { api, mediaSrc } from '../api'
import type { FolderScanResult } from '../api'
import { refreshTasks, store, ui } from '../store'
import { hasScene } from '../composables/useModelOptions'
import MangaModelGrid from '../components/MangaModelGrid.vue'

const message = useMessage()

// 模型对比页"用此模型处理图片"入口：进页预选模型与倍率。
// 本页 KeepAlive 常驻：二次进入只触发 onActivated 不再触发 onMounted，两处都得消费
function consumePendingModel() {
  if (!ui.pendingModel) return
  const spec = store.models.find((m) => m.id === ui.pendingModel)
  if (spec?.vram_ok) {
    modelId.value = spec.id
    const want = ui.pendingScale
    if (want && spec.scale.includes(want)) targetScale.value = want
  }
  ui.pendingModel = null
  ui.pendingScale = null
}
onMounted(consumePendingModel)
onActivated(consumePendingModel)

// 任务页「改参数重试」：带原参数回填本页（漫画任务分流入口）。文件夹模式
// 任务重扫 folder_src 重建摘要卡（扫描失败回落散页清单，不阻塞改参）。
// 双钩子+watch 三管齐下靠置空幂等：首次直达只触发 onMounted，KeepAlive
// 缓存页二次进入只触发 onActivated——单挂 watch 会漏掉组件尚未创建的首次
async function consumeRetryParams() {
  if (ui.page !== 'mangasr' || !ui.pendingTaskParams) return
  const t = ui.pendingTaskParams
  ui.pendingTaskParams = null
  const p = t.params ?? {}
  const imgs = p.images as { in: string }[] | undefined
  const list = Array.isArray(imgs) && imgs.length ? imgs.map((x) => x.in) : [t.input_path]
  const folderSrc = typeof p.folder_src === 'string' ? p.folder_src : ''
  if (folderSrc) {
    try {
      const r = await api.scanImageFolder(folderSrc)
      if (r.total) {
        folder.value = r
        files.value = []
      } else {
        files.value = list
      }
    } catch {
      files.value = list // 源文件夹被移走等：散页兜底，用户自己重选
    }
  } else {
    files.value = list
  }
  // 模型可能已被删除：找不到就保持空，用户手选
  const spec = store.models.find((m) => m.id === t.model_id)
  if (spec?.vram_ok) {
    modelId.value = spec.id
    const want = Number(p.target_scale ?? p.scale ?? 0)
    if (want && spec.scale.includes(want)) targetScale.value = want
  }
  const colorId = typeof p.model_id_color === 'string' ? p.model_id_color : ''
  if (colorId) {
    const colorSpec = store.models.find((m) => m.id === colorId)
    if (colorSpec?.vram_ok) {
      mixMode.value = true
      colorModelId.value = colorId
    }
  }
  mixSplit.value = p.mix_pass === 'split'
  if (p.format === 'jpg') {
    format.value = 'jpg'
    if (typeof p.jpg_quality === 'number') jpgQuality.value = p.jpg_quality
  } else {
    format.value = 'png'
  }
  if (typeof p.tile === 'number') tileChoice.value = p.tile
  mergePdf.value = p.merge_pdf !== false // 漫画页默认开：仅任务显式关过才关
}
onMounted(consumeRetryParams)
onActivated(consumeRetryParams)
watch([() => ui.page, () => ui.pendingTaskParams], () => void consumeRetryParams())

// 文件夹模式（整本漫画）为主入口：与散选互斥——选文件夹清散页，散选/拖拽清文件夹。
// 扫描结果持有 rel 清单，提交时由后端按 rel 镜像输出目录结构
const folder = ref<FolderScanResult | null>(null)
const scanning = ref(false)
const files = ref<string[]>([])
const modelId = ref('')
const targetScale = ref(2)
const format = ref<'png' | 'jpg'>('png')
const jpgQuality = ref(92)
const tileChoice = ref(0) // 0 = 模型默认
const mergePdf = ref(true) // 整本漫画默认出 PDF：阅读场景第一诉求
const submitting = ref(false)

// ---- 模型：默认只列漫画向（scenes 含 manga），开关兜底展开全部 ----
const showAllModels = ref(false)
const srModels = computed(() => {
  const all = store.models.filter((m) => m.kind !== 'interp')
  const pool = showAllModels.value
    ? all
    : all.filter((m) => hasScene(m, 'manga'))
  return pool.sort((a, b) => {
    // 展开全部时漫画向仍排前（本页语义），组内再按已装优先
    const ma = Number(hasScene(a, 'manga'))
    const mb = Number(hasScene(b, 'manga'))
    if (ma !== mb) return mb - ma
    return Number(!!(b.installed || b.bundled)) - Number(!!(a.installed || a.bundled))
  })
})
const selectedModel = computed(() => store.models.find((m) => m.id === modelId.value))

// ---- 混装双模型：黑白页走主模型、彩页走彩色模型（后端逐页识别分派） ----
const mixMode = ref(false)
const colorModelId = ref('')
const mixSplit = ref(false) // 分趟：先黑白趟末释放引擎再建彩模（显存峰值≈单个模型）
const colorSelected = computed(() => store.models.find((m) => m.id === colorModelId.value))
// 倍率须两模型同时支持：混装时取交集（交集空=不可提交）
const commonScales = computed(() => {
  if (!mixMode.value || !selectedModel.value || !colorSelected.value) return null
  const b = new Set(colorSelected.value.scale)
  return selectedModel.value.scale.filter((s) => b.has(s))
})
const scaleOptions = computed(() => {
  const src = commonScales.value ?? (selectedModel.value?.scale ?? [])
  return src.map((s) => ({ label: `x${s}`, value: s }))
})

/** 选模型：新模型支持当前倍率则保留，否则回落到可选档的最小倍率（交集感知；
 * 交集空=不可提交态，倍率回落单看本模型，别让 Math.min(...[]) 产出 Infinity） */
function pickModel(id: string, target: 'bw' | 'color') {
  const spec = store.models.find((m) => m.id === id)
  if (!spec || !spec.vram_ok) return
  if (target === 'bw') modelId.value = id
  else colorModelId.value = id
  const cs = commonScales.value
  const pool = cs && cs.length ? cs : spec.scale
  if (!pool.includes(targetScale.value)) targetScale.value = Math.min(...pool)
}
function selectModel(id: string) {
  pickModel(id, 'bw')
}
function selectColorModel(id: string) {
  pickModel(id, 'color')
}
// 开启混装时自动预选彩模（与当前倍率兼容的第一个漫画向模型，避开主模型）
watch(mixMode, (on) => {
  if (!on) return
  const ok = colorSelected.value && colorSelected.value.vram_ok
    && (colorModelId.value !== modelId.value)
    && (selectedModel.value ? colorSelected.value!.scale.some((s) => selectedModel.value!.scale.includes(s)) : true)
  if (ok) return
  const cand = srModels.value.find(
    (m) => m.vram_ok && m.id !== modelId.value && m.scale.includes(targetScale.value),
  ) ?? srModels.value.find((m) => m.vram_ok && m.id !== modelId.value)
  if (cand) colorModelId.value = cand.id
  if (commonScales.value && commonScales.value.length && !commonScales.value.includes(targetScale.value)) {
    targetScale.value = Math.max(...commonScales.value)
  }
})
const tileOptions = [
  { label: '自动（模型默认）', value: 0 },
  ...[128, 192, 256, 384, 512, 768, 1024].map((v) => ({ label: `${v} px`, value: v })),
]

const outDirLabel = computed(() => {
  const d = String(store.settings.output_dir ?? '').trim()
  return d || '源文件夹旁边'
})

function baseName(p: string): string {
  return p.split(/[\\/]/).pop() ?? p
}
// file:// 直拼对含 #/? 的文件名会破（mediaSrc 已做逐段编码，这里复用）
function thumbUrl(p: string): string {
  return mediaSrc(p)
}

const IMAGE_EXT = /\.(png|jpe?g|webp|bmp|tiff?)$/i

const batchN = computed(() => (folder.value ? folder.value.total : files.value.length))
const dragHint = computed(() => !files.value.length && !folder.value)

// ---- 拖拽入队：整个页面都是放置区（散页拖入先让位文件夹模式） ----
const dragDepth = ref(0)
function onDragEnter(e: DragEvent) {
  if (!e.dataTransfer?.types.includes('Files')) return
  dragDepth.value++
}
function onDragLeave() {
  dragDepth.value = Math.max(0, dragDepth.value - 1)
}
function onDropFiles(e: DragEvent) {
  dragDepth.value = 0
  const dropped = [...(e.dataTransfer?.files ?? [])]
  const imgs = dropped
    .filter((f) => IMAGE_EXT.test(f.name))
    .map((f) => window.sv.pathForFile(f))
  if (!imgs.length) return
  folder.value = null // 互斥：拖散页退出文件夹模式
  for (const p of imgs) if (!files.value.some((x) => x.toLowerCase() === p.toLowerCase())) files.value.push(p)
}

async function pickFolder() {
  const dir = await window.sv.pickDir()
  if (!dir) return
  scanning.value = true
  try {
    const r = await api.scanImageFolder(dir)
    if (!r.total) {
      message.warning('该文件夹（含子目录）里没有受支持的图片')
      return
    }
    folder.value = r
    files.value = [] // 互斥：进文件夹模式清散选
  } catch (e) {
    message.error(`扫描文件夹失败: ${e instanceof Error ? e.message : e}`)
  } finally {
    scanning.value = false
  }
}

function pick() {
  void window.sv.pickImages().then((picked) => {
    if (!picked.length) return
    folder.value = null // 互斥：散选退出文件夹模式
    for (const p of picked) {
      if (!files.value.some((x) => x.toLowerCase() === p.toLowerCase())) files.value.push(p)
    }
  })
}

function removeAt(i: number) {
  files.value.splice(i, 1)
}

function clearAll() {
  files.value = []
  folder.value = null
}

const canSubmit = computed(
  () =>
    batchN.value > 0 &&
    !!modelId.value &&
    !!selectedModel.value?.vram_ok &&
    (!mixMode.value || (!!colorModelId.value && !!colorSelected.value?.vram_ok)) &&
    (!commonScales.value || commonScales.value.length > 0) &&
    !submitting.value,
)

// 解码失败（TIFF 变体/超大图/损坏文件）的缩略图：就地换占位，不影响其他图片
const broken = ref<Set<string>>(new Set())
function onThumbErr(f: string) {
  const s = new Set(broken.value)
  s.add(f)
  broken.value = s
}

async function submit() {
  if (!canSubmit.value) return
  submitting.value = true
  // 批量合并为一个任务：后端一次模型加载循环处理全部图片；
  // 文件夹模式额外带 folder_src，后端按相对路径镜像输出目录结构
  const isFolder = !!folder.value
  const list = isFolder ? folder.value!.files.map((f) => f.path) : files.value
  const n = list.length
  const wantPdf = mergePdf.value && n >= 2
  const r = await api.createTask({
    inputs: list,
    model_id: modelId.value,
    params: {
      kind: 'manga',
      scale: targetScale.value,
      target_scale: targetScale.value,
      format: format.value,
      ...(format.value === 'jpg' ? { jpg_quality: jpgQuality.value } : {}),
      ...(tileChoice.value ? { tile: tileChoice.value } : {}),
      ...(wantPdf ? { merge_pdf: true } : {}),
      ...(isFolder ? { folder_src: folder.value!.folder } : {}),
      ...(mixMode.value && colorModelId.value
        ? { model_id_color: colorModelId.value, ...(mixSplit.value ? { mix_pass: 'split' } : {}) }
        : {}),
    },
  })
  submitting.value = false
  if (r.ok) {
    const mixNote = mixMode.value && colorSelected.value
      ? `，彩色页走 ${colorSelected.value.name}、黑白页走 ${selectedModel.value?.name ?? ''}`
        + (mixSplit.value ? '，分趟处理（显存峰值≈单个模型）' : '')
      : ''
    message.success(
      isFolder
        ? `已加入队列（${baseName(folder.value!.folder)} 整本 ${n} 页合并为 1 个漫画任务，输出将镜像目录结构${wantPdf ? '，另将无损合并输出一份 PDF' : ''}${mixNote}）`
        : `已加入队列（${n} 页漫画合并为 1 个批量任务${selectedModel.value && !selectedModel.value.installed && !selectedModel.value.bundled ? '，模型将自动下载' : ''}${wantPdf ? '，另将无损合并输出一份 PDF' : ''}${mixNote}）`,
    )
    files.value = []
    folder.value = null
    ui.page = 'tasks'
    refreshTasks()
  } else {
    message.error(`创建失败: ${(await r.json()).detail ?? r.status}`)
  }
}
</script>

<script lang="ts">
// KeepAlive include 按名匹配：选了一半漫画切页回来不丢
export default { name: 'MangaSR' }
</script>

<template>
  <div
    class="mangasr-page"
    @dragenter="onDragEnter"
    @dragover.prevent
    @dragleave="onDragLeave"
    @drop.prevent="onDropFiles"
  >
    <div v-if="dragDepth" class="drop-mask">
      <div class="drop-tip">松开即可加入漫画页</div>
    </div>
    <div class="page-head">
      <div>
        <h1>漫画超分</h1>
        <p class="sub">整本漫画文件夹（卷/话子目录原样镜像）→ 漫画专用模型放大 → PNG / JPG / PDF</p>
      </div>
    </div>

    <!-- ① 选择漫画 -->
    <section class="sec sv-card">
      <h2 class="sec-title"><span class="sec-num">1</span>选择漫画</h2>
      <div class="pick-row">
        <NButton dashed size="large" class="grow main" :loading="scanning" @click="pickFolder">
          {{ folder ? `已选 ${baseName(folder.folder)}（点击换一本）` : '选择漫画文件夹（含子目录，整本）' }}
        </NButton>
        <NButton dashed size="large" class="grow" @click="pick">
          {{ files.length ? `已补选 ${files.length} 页（点击继续追加）` : '补选散页（可多选）' }}
        </NButton>
      </div>
      <div v-if="dragHint" class="drop-hint">也可以直接把漫画页拖进窗口</div>

      <!-- 文件夹模式：摘要卡 + 首屏预览（替代几百页散页缩略图墙） -->
      <div v-if="folder" class="folder-card">
        <div class="f-ico" aria-hidden="true">
          <svg viewBox="0 0 24 24" width="26" height="26" fill="none">
            <path d="M3 6.5A1.5 1.5 0 0 1 4.5 5h4l2.2 2.5H19.5A1.5 1.5 0 0 1 21 9v9a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 18V6.5Z"
              stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" />
          </svg>
        </div>
        <div class="f-info">
          <div class="f-name" :title="folder.folder">{{ baseName(folder.folder) }}</div>
          <div class="f-meta">
            共 {{ folder.total }} 页<template v-if="folder.dirs"> · 含 {{ folder.dirs }} 个子目录</template>
            · 输出将镜像目录结构
          </div>
        </div>
        <button class="rm" title="移除文件夹" @click="folder = null">✕</button>
        <div v-if="folder.total" class="folder-peeks">
          <img
            v-for="f in folder.files.slice(0, 10)"
            :key="f.path"
            :src="thumbUrl(f.path)"
            class="peek"
            loading="lazy"
            alt=""
            @error="onThumbErr(f.path)"
          />
          <span v-if="folder.total > 10" class="peek more">+{{ folder.total - 10 }}</span>
        </div>
      </div>

      <div v-if="files.length" class="thumb-grid">
        <div v-for="(f, i) in files" :key="f" class="thumb-cell">
          <img
            v-if="!broken.has(f)"
            :src="thumbUrl(f)"
            class="thumb"
            loading="lazy"
            alt=""
            @error="onThumbErr(f)"
          />
          <div v-else class="thumb thumb-broken" title="此图片无法预览（不影响处理）">无法预览</div>
          <button class="rm" title="移除" @click="removeAt(i)">✕</button>
          <span class="fname" :title="f">{{ baseName(f) }}</span>
        </div>
      </div>
    </section>

    <!-- ② 模型与输出 -->
    <section class="sec sv-card">
      <h2 class="sec-title">
        <span class="sec-num">2</span>模型与输出
        <span class="sel-chip" :class="{ on: !!selectedModel }">
          <template v-if="mixMode && selectedModel && colorSelected">
            {{ selectedModel.name }}（黑白）＋ {{ colorSelected.name }}（彩色） · x{{ targetScale }}{{ mixSplit ? ' · 分趟' : '' }}
          </template>
          <template v-else>
            {{ selectedModel ? `已选 ${selectedModel.name} · x${targetScale}` : '点击卡片选择' }}
          </template>
        </span>
        <NCheckbox v-model:checked="showAllModels" size="small" class="show-all">
          显示全部模型
        </NCheckbox>
      </h2>
      <div class="mix-row">
        <NCheckbox v-model:checked="mixMode">
          彩色 / 黑白混装（逐页自动识别分派）
        </NCheckbox>
        <span class="mix-hint">
          整本里既有彩页又有黑白页时开启：创建时逐页识别，彩页走彩色模型、黑白页走主模型；
          两个模型会{{ mixSplit ? '分趟加载（先黑白后彩色，显存峰值≈单个模型）' : '同时驻留显存（约为两者之和）' }}
        </span>
      </div>
      <div v-if="mixMode && commonScales && commonScales.length === 0" class="mix-warn">
        两个模型没有共同支持的放大倍率，无法混装——请换一组模型
      </div>
      <div v-if="!srModels.length" class="empty-models">
        暂无漫画向模型{{ showAllModels ? '' : '，可勾选「显示全部模型」用通用模型处理' }}
      </div>
      <template v-else-if="mixMode">
        <div class="lane-label">黑白页模型</div>
        <MangaModelGrid :models="srModels" :selected="modelId" @select="selectModel" />
        <div class="lane-label">彩色页模型</div>
        <MangaModelGrid :models="srModels" :selected="colorModelId" @select="selectColorModel" />
        <div class="mix-row">
          <NCheckbox v-model:checked="mixSplit" size="small">
            分趟处理（省显存）
          </NCheckbox>
          <span class="mix-hint">
            先跑完全部黑白页、释放引擎后再加载彩色模型，显存峰值≈单个模型；代价是趟间
            重建引擎（几秒）、进度按「先黑白后彩色」推进；输出与 PDF 页序不受影响
          </span>
        </div>
      </template>
      <MangaModelGrid v-else :models="srModels" :selected="modelId" @select="selectModel" />
      <div class="form-rows">
        <div class="row inline">
          <span class="lbl">放大倍数</span>
          <NRadioGroup v-model:value="targetScale" size="small">
            <NRadioButton v-for="s in scaleOptions" :key="s.value" :value="s.value">x{{ s.value }}</NRadioButton>
          </NRadioGroup>
          <NTag v-if="batchN === 1" size="small" :bordered="false">
            边长 ×{{ targetScale }} · 面积 ×{{ targetScale * targetScale }}
          </NTag>
        </div>
        <div class="row inline">
          <span class="lbl">输出格式</span>
          <NRadioGroup v-model:value="format" size="small">
            <NRadioButton value="png">PNG（无损）</NRadioButton>
            <NRadioButton value="jpg">JPG（体积小）</NRadioButton>
          </NRadioGroup>
          <template v-if="format === 'jpg'">
            <span class="q-label">质量 {{ jpgQuality }}</span>
            <NSlider v-model:value="jpgQuality" :min="60" :max="100" :step="1" style="width: 180px" />
          </template>
        </div>
        <div v-if="batchN >= 2" class="row inline">
          <span class="lbl">整本合并</span>
          <NCheckbox v-model:checked="mergePdf">
            另外输出一份 PDF（全部页按顺序无损封装，逐页图片文件仍保留）
          </NCheckbox>
        </div>
        <div class="row stack">
          <span class="lbl">分块大小（高级）</span>
          <NSelect v-model:value="tileChoice" :options="tileOptions" style="width: 200px" />
        </div>
        <p class="hint-row">
          自动=按模型默认；扫描件超大页显存不足时调小分块。整本输出到「{{
            outDirLabel
          }}」下的「文件夹名_倍率」目录，按源目录结构镜像（卷/话子目录原样保留）；散页保存到「{{
            outDirLabel === '源文件夹旁边' ? '源图片所在目录' : outDirLabel
          }}」。混装识别：创建任务时逐页判断彩色/黑白（识别统计见任务日志），识别结果随任务保存、续跑不重算。PDF
          无损口径：PNG 结果逐像素一致直接嵌入，JPG 结果按原文件字节嵌入不再压缩。
        </p>
      </div>
    </section>

    <!-- 吸底操作条 -->
    <div class="footer-bar sv-card">
      <NButton :disabled="submitting" @click="clearAll">清空</NButton>
      <NButton type="primary" :loading="submitting" :disabled="!canSubmit" @click="submit">
        加入队列（{{ batchN }} 页）
      </NButton>
    </div>
  </div>
</template>

<style scoped>
.mangasr-page {
  display: flex;
  flex-direction: column;
  gap: 18px;
  min-height: 100%;
}
h1 { font-size: 22px; font-weight: 600; letter-spacing: 0.3px; }
.sub { font-size: 12.5px; color: var(--sv-text-dim); margin-top: 4px; }

/* 步骤面板：与新建任务页同款画布 */
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
.sel-chip { margin-left: auto; font-size: 12px; font-weight: 400; color: var(--sv-text-dim); }
.sel-chip.on { color: var(--sv-accent-strong); }
.show-all {
  /* sel-chip 的 margin-left:auto 之后紧贴右侧：开关自身不需要再推 */
  font-size: 12.5px;
  color: var(--sv-text-dim);
}
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
  box-shadow: 0 0 10px rgba(var(--sv-accent-rgb), 0.3);
}

/* 双入口：整本文件夹为主（描边加重），补选散页为辅 */
.pick-row { display: flex; gap: 10px; }
.pick-row .grow { flex: 1 1 0; min-width: 0; }
.pick-row .main { border-width: 2px; font-weight: 600; }
.drop-hint {
  font-size: 12px;
  color: var(--sv-text-faint);
  text-align: center;
}

/* 文件夹模式摘要卡：图标 + 名称 + 统计 + 首屏预览 */
.folder-card {
  position: relative;
  display: grid;
  grid-template-columns: auto 1fr auto;
  align-items: center;
  gap: 12px;
  padding: 14px 16px;
  border: 1.5px solid rgba(var(--sv-accent-rgb), 0.45);
  border-radius: var(--sv-radius-md);
  background:
    linear-gradient(135deg, rgba(var(--sv-accent-rgb), 0.08), rgba(var(--sv-accent2-rgb), 0.04)),
    var(--sv-fill-1);
}
.f-ico {
  width: 44px;
  height: 44px;
  border-radius: 10px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--sv-accent-strong);
  background: var(--sv-accent-bg);
  border: 1px solid rgba(var(--sv-accent-rgb), 0.25);
}
.f-name { font-weight: 650; font-size: 14.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.f-meta { font-size: 12px; color: var(--sv-text-dim); margin-top: 3px; }
.folder-card .rm { position: static; }
.folder-peeks {
  grid-column: 1 / -1;
  display: flex;
  gap: 6px;
  margin-top: 4px;
}
.peek {
  width: 56px;
  height: 42px;
  object-fit: cover;
  border-radius: 6px;
  border: 1px solid var(--sv-border-mid);
  background: rgba(0, 0, 0, 0.28);
}
.peek.more {
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11.5px;
  color: var(--sv-text-dim);
  border-style: dashed;
  background: none;
  flex: none;
}

/* 缩略图墙 */
.thumb-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
  gap: 12px;
}
.thumb-cell {
  position: relative;
  border: 1px solid var(--sv-border-mid);
  border-radius: 10px;
  background: var(--sv-fill-1);
  padding: 6px;
  transition: border-color 0.15s, background 0.15s;
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}
.thumb {
  width: 100%;
  aspect-ratio: 4 / 3;
  object-fit: cover;
  border-radius: 6px;
  display: block;
  background: rgba(0, 0, 0, 0.28);
}
.thumb-broken {
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  color: var(--sv-text-faint);
  border: 1px dashed var(--sv-border-strong);
}
.rm {
  position: absolute;
  top: 10px;
  right: 10px;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  border: none;
  background: var(--sv-panel-2);
  color: var(--sv-text);
  font-size: 11px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  transition: background 0.15s;
}
.rm:hover { background: var(--sv-danger); }
.fname {
  font-size: 11.5px;
  color: var(--sv-text-faint);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.form-rows {
  background: var(--sv-panel-grad);
  border: 1px solid var(--sv-border-soft);
  border-radius: var(--sv-radius-md);
  box-shadow: var(--sv-card-inset);
  padding: 6px 18px 12px;
}

/* 模型区：混装开关行 + 车道分组标签（卡片网格在 MangaModelGrid 组件里） */
.mix-row {
  display: flex;
  align-items: baseline;
  gap: 12px;
  flex-wrap: wrap;
  padding: 8px 10px;
  border: 1px dashed var(--sv-border-mid);
  border-radius: var(--sv-radius-md);
  background: rgba(var(--sv-accent-rgb), 0.04);
}
.mix-hint { font-size: 12px; color: var(--sv-text-faint); line-height: 1.5; }
.mix-warn {
  font-size: 12.5px;
  color: var(--sv-danger);
  padding: 4px 2px;
}
.lane-label {
  font-size: 12.5px;
  font-weight: 650;
  color: var(--sv-text-dim);
  letter-spacing: 0.5px;
  margin-top: 2px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.lane-label::after {
  content: '';
  flex: 1;
  height: 1px;
  background: linear-gradient(90deg, var(--sv-border-mid), transparent);
}
.empty-models {
  padding: 22px 0 10px;
  text-align: center;
  font-size: 12.5px;
  color: var(--sv-text-faint);
}

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
.row { padding: 12px 0; }
.row.stack {
  display: flex;
  flex-direction: column;
  gap: 8px;
  align-items: flex-start;
}
.row.inline {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
}
.lbl { font-weight: 600; font-size: 13px; color: var(--sv-text); }
.q-label { font-size: 12.5px; color: var(--sv-text-dim); }
.hint-row {
  color: var(--sv-text-dim);
  font-size: 12px;
  line-height: 1.55;
  border-top: 1px solid var(--sv-border-soft);
  padding-top: 10px;
  margin: 2px 0 0;
}

/* 吸底操作条：滚动时贴住可视区底部，内容不足一屏时沉到页底 */
.footer-bar {
  position: sticky;
  bottom: 0;
  margin-top: auto;
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 16px;
  background: var(--sv-panel-2);
  border: 1px solid var(--sv-border-mid);
  border-radius: var(--sv-radius-md);
  box-shadow: var(--sv-card-inset), 0 -6px 24px rgba(0, 0, 0, 0.3), 0 8px 22px rgba(0, 0, 0, 0.25);
  z-index: 5;
}
</style>
