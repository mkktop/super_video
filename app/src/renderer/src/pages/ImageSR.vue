<script setup lang="ts">
import { computed, onActivated, onMounted, ref } from 'vue'
import {
  NButton,
  NCheckbox,
  NInputNumber,
  NRadioButton,
  NRadioGroup,
  NSelect,
  NSlider,
  NTag,
  useMessage,
} from 'naive-ui'
import { api, mediaSrc } from '../api'
import { refreshTasks, store, ui } from '../store'

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

const files = ref<string[]>([])
const modelId = ref('')
const targetScale = ref(2)
const format = ref<'png' | 'jpg'>('png')
const jpgQuality = ref(92)
const tileChoice = ref(0) // 0 = 模型默认
const mergePdf = ref(false) // 批量 ≥2 张可勾选：另出一份无损封装的 PDF
const submitting = ref(false)

// ---- 模型 ----
const srModels = computed(() => {
  const all = store.models.filter((m) => m.kind !== 'interp')
  return all.sort((a, b) => Number(b.installed || b.bundled) - Number(a.installed || a.bundled))
})
const contentLabel = { anime: '动漫', comic: '漫画', general: '真人/通用', real: '真人/通用' } as Record<string, string>
const selectedModel = computed(() => store.models.find((m) => m.id === modelId.value))
const scaleOptions = computed(
  () => (selectedModel.value?.scale ?? []).map((s) => ({ label: `x${s}`, value: s })),
)

/** 选模型：新模型支持当前倍率则保留，否则回落到最小倍率（与新建任务页一致） */
function selectModel(id: string) {
  const spec = store.models.find((m) => m.id === id)
  if (!spec || !spec.vram_ok) return
  modelId.value = id
  if (!spec.scale.includes(targetScale.value)) targetScale.value = Math.min(...spec.scale)
}
const tileOptions = [
  { label: '自动（模型默认）', value: 0 },
  ...[128, 192, 256, 384, 512, 768, 1024].map((v) => ({ label: `${v} px`, value: v })),
]

const outDirLabel = computed(() => {
  const d = String(store.settings.output_dir ?? '').trim()
  return d || '源图片所在目录'
})

function baseName(p: string): string {
  return p.split(/[\\/]/).pop() ?? p
}
// file:// 直拼对含 #/? 的文件名会破（mediaSrc 已做逐段编码，这里复用）
function thumbUrl(p: string): string {
  return mediaSrc(p)
}

const IMAGE_EXT = /\.(png|jpe?g|webp|bmp|tiff?)$/i

// ---- 拖拽入队：整个页面都是放置区 ----
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
  for (const p of imgs) if (!files.value.some((x) => x.toLowerCase() === p.toLowerCase())) files.value.push(p)
}

function pick() {
  void window.sv.pickImages().then((picked) => {
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
}

const canSubmit = computed(
  () =>
    files.value.length > 0 &&
    !!modelId.value &&
    !!selectedModel.value?.vram_ok &&
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
  // 批量合并为一个任务：后端一次模型加载循环处理全部图片
  const n = files.value.length
  const wantPdf = mergePdf.value && n >= 2
  const r = await api.createTask({
    inputs: files.value,
    model_id: modelId.value,
    params: {
      kind: 'image',
      scale: targetScale.value,
      target_scale: targetScale.value,
      format: format.value,
      ...(format.value === 'jpg' ? { jpg_quality: jpgQuality.value } : {}),
      ...(tileChoice.value ? { tile: tileChoice.value } : {}),
      ...(wantPdf ? { merge_pdf: true } : {}),
    },
  })
  submitting.value = false
  if (r.ok) {
    message.success(
      `已加入队列（${n} 张图片合并为 1 个批量任务${selectedModel.value && !selectedModel.value.installed && !selectedModel.value.bundled ? '，模型将自动下载' : ''}${wantPdf ? '，另将无损合并输出一份 PDF' : ''}）`,
    )
    files.value = []
    ui.page = 'tasks'
    refreshTasks()
  } else {
    message.error(`创建失败: ${(await r.json()).detail ?? r.status}`)
  }
}
</script>

<script lang="ts">
// KeepAlive include 按名匹配：选了一半图片切页回来不丢
export default { name: 'ImageSR' }
</script>

<template>
  <div
    class="imgsr-page"
    @dragenter="onDragEnter"
    @dragover.prevent
    @dragleave="onDragLeave"
    @drop.prevent="onDropFiles"
  >
    <div v-if="dragDepth" class="drop-mask">
      <div class="drop-tip">松开即可加入图片</div>
    </div>
    <div class="page-head">
      <div>
        <h1>图片超分</h1>
        <p class="sub">单张或批量 → 选模型放大 → 结果保存为 PNG / JPG</p>
      </div>
    </div>

    <!-- ① 选择图片 -->
    <section class="sec">
      <h2 class="sec-title"><span class="sec-num">1</span>选择图片</h2>
      <NButton dashed block size="large" @click="pick">
        {{ files.length ? `已选 ${files.length} 张（点击继续追加）` : '点击选择图片（可多选批量入队，也可直接拖进窗口）' }}
      </NButton>
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
    <section class="sec">
      <h2 class="sec-title">
        <span class="sec-num">2</span>模型与输出
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
          @click="selectModel(m.id)"
        >
          <span v-if="modelId === m.id" class="m-check">✓</span>
          <div class="m-head">
            <span class="m-name">{{ m.name }}</span>
            <NTag v-if="!m.installed && !m.bundled" size="tiny" :bordered="false" type="warning">需下载 {{ m.size_mb }}MB</NTag>
            <NTag v-if="!m.vram_ok" size="tiny" :bordered="false" type="error">显存不足</NTag>
            <span v-if="(m.scenes ?? ['video', 'image']).some((s) => s !== 'image')" class="m-scenes">
              <NTag v-for="s in (m.scenes ?? []).filter((k) => k !== 'image')" :key="s"
                    size="tiny" type="info" :bordered="false">{{ s === 'manga' ? '漫画' : '视频' }}</NTag>
            </span>
          </div>
          <div class="m-desc">{{ m.description }}</div>
          <div class="m-tags">
            <span>x{{ m.scale.join('/x') }}</span>
            <span>{{ m.vram_gb }}GB 显存</span>
            <span v-for="c in m.content" :key="c" class="m-content">{{ contentLabel[c] ?? c }}</span>
          </div>
        </div>
      </div>
      <div class="form-rows">
        <div class="row inline">
          <span class="lbl">放大倍数</span>
          <NRadioGroup v-model:value="targetScale" size="small">
            <NRadioButton v-for="s in scaleOptions" :key="s.value" :value="s.value">x{{ s.value }}</NRadioButton>
          </NRadioGroup>
          <NTag v-if="files.length === 1" size="small" :bordered="false">
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
        <div v-if="files.length >= 2" class="row inline">
          <span class="lbl">批量合并</span>
          <NCheckbox v-model:checked="mergePdf">
            另外输出一份 PDF（全部结果按顺序无损封装，逐张图片文件仍保留）
          </NCheckbox>
        </div>
        <div class="row stack">
          <span class="lbl">分块大小（高级）</span>
          <NSelect v-model:value="tileChoice" :options="tileOptions" style="width: 200px" />
        </div>
        <p class="hint-row">
          自动=按模型默认；超大图（如 8K 扫描件）显存不足时调小分块。结果保存到「{{
            outDirLabel
          }}」，目录内无同名时沿用原文件名，同名冲突自动改用「原名_倍率」后缀，不覆盖现有文件。PDF
          无损口径：PNG 结果逐像素一致直接嵌入，JPG 结果按原文件字节嵌入不再压缩。
        </p>
      </div>
    </section>

    <!-- 吸底操作条 -->
    <div class="footer-bar">
      <NButton :disabled="submitting" @click="clearAll">清空</NButton>
      <NButton type="primary" :loading="submitting" :disabled="!canSubmit" @click="submit">
        加入队列（{{ files.length }} 张）
      </NButton>
    </div>
  </div>
</template>

<style scoped>
.imgsr-page {
  display: flex;
  flex-direction: column;
  gap: 18px;
  min-height: 100%;
}
h1 { font-size: 20px; font-weight: 700; }
.sub { font-size: 12.5px; color: #9aa0a6; margin-top: 4px; }

.sec { display: flex; flex-direction: column; gap: 12px; }
.sec-title {
  font-size: 15px;
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: 8px;
}
.sel-chip { margin-left: auto; font-size: 12px; font-weight: 400; color: #9aa0a6; }
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

/* 缩略图墙 */
.thumb-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
  gap: 12px;
}
.thumb-cell {
  position: relative;
  border: 1px solid #2a2d31;
  border-radius: 10px;
  background: #1a1c1f;
  padding: 6px;
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
  background: #141517;
}
.thumb-broken {
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  color: #6d747d;
  border: 1px dashed #33363b;
}
.rm {
  position: absolute;
  top: 10px;
  right: 10px;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  border: none;
  background: rgba(20, 21, 23, 0.82);
  color: #e8eaed;
  font-size: 11px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  transition: background 0.15s;
}
.rm:hover { background: rgba(232, 62, 62, 0.9); }
.fname {
  font-size: 11.5px;
  color: #9aa0a6;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.form-rows {
  background: #1a1c1f;
  border: 1px solid #26292e;
  border-radius: 12px;
  padding: 6px 18px 12px;
}

/* 模型卡片网格（与新建任务页同款交互） */
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
.model-card.selected { border-color: #4f8cff; background: rgba(79, 140, 255, 0.08); }
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
.m-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.m-scenes { margin-left: auto; display: inline-flex; gap: 4px; }
.m-name { font-weight: 600; font-size: 14px; }
.m-desc {
  color: #9aa0a6;
  font-size: 12px;
  margin: 6px 0;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.m-tags { display: flex; gap: 10px; font-size: 12px; color: #7c838c; flex-wrap: wrap; }
.m-content { color: #8fa3c8; }

.drop-mask {
  position: fixed;
  inset: 0;
  z-index: 50;
  background: rgba(13, 14, 16, 0.72);
  border: 2px dashed #4f8cff;
  display: flex;
  align-items: center;
  justify-content: center;
  pointer-events: none;
}
.drop-tip { font-size: 18px; font-weight: 600; color: #e8eaed; letter-spacing: 1px; }
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
.lbl { font-weight: 600; font-size: 13px; color: #e8eaed; }
.q-label { font-size: 12.5px; color: #9aa0a6; }
.hint-row {
  color: #9aa0a6;
  font-size: 12px;
  line-height: 1.55;
  border-top: 1px solid #212429;
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
  padding: 12px 4px 4px;
  background: #141517;
  border-top: 1px solid #232629;
  z-index: 5;
}
</style>
