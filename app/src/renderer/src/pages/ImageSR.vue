<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  NButton,
  NInputNumber,
  NRadioButton,
  NRadioGroup,
  NSelect,
  NSlider,
  NTag,
  useMessage,
} from 'naive-ui'
import { api } from '../api'
import { refreshTasks, store, ui } from '../store'

const message = useMessage()

// 模型对比页"用此模型处理图片"入口：进页预选模型与倍率
onMounted(() => {
  if (!ui.pendingModel) return
  const spec = store.models.find((m) => m.id === ui.pendingModel)
  if (spec?.vram_ok) {
    modelId.value = spec.id
    const want = ui.pendingScale
    if (want && spec.scale.includes(want)) targetScale.value = want
  }
  ui.pendingModel = null
  ui.pendingScale = null
})

const files = ref<string[]>([])
const modelId = ref('')
const targetScale = ref(2)
const format = ref<'png' | 'jpg'>('png')
const jpgQuality = ref(92)
const tileChoice = ref(0) // 0 = 模型默认
const submitting = ref(false)

// ---- 模型 ----
const srModels = computed(() => {
  const all = store.models.filter((m) => m.kind !== 'interp')
  return all.sort((a, b) => Number(b.installed || b.bundled) - Number(a.installed || a.bundled))
})
const modelOptions = computed(() =>
  srModels.value.map((m) => ({
    label: `${m.name}（x${m.scale.join('/x')}，${m.vram_gb}GB 显存${m.installed || m.bundled ? '' : '，需下载'}）`,
    value: m.id,
    disabled: !m.vram_ok,
  })),
)
const selectedModel = computed(() => store.models.find((m) => m.id === modelId.value))
const scaleOptions = computed(
  () => (selectedModel.value?.scale ?? []).map((s) => ({ label: `x${s}`, value: s })),
)
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
function thumbUrl(p: string): string {
  // Windows 路径 → file:// URL（webSecurity 保持关闭的产品决策下可用）
  return `file:///${p.replace(/\\/g, '/')}`
}

function pick() {
  void window.sv.pickImages().then((picked) => {
    for (const p of picked) if (!files.value.includes(p)) files.value.push(p)
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

async function submit() {
  if (!canSubmit.value) return
  submitting.value = true
  // 批量合并为一个任务：后端一次模型加载循环处理全部图片
  const n = files.value.length
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
    },
  })
  submitting.value = false
  if (r.ok) {
    message.success(
      `已加入队列（${n} 张图片合并为 1 个批量任务${selectedModel.value && !selectedModel.value.installed && !selectedModel.value.bundled ? '，模型将自动下载' : ''}）`,
    )
    files.value = []
    ui.page = 'tasks'
    refreshTasks()
  } else {
    message.error(`创建失败: ${(await r.json()).detail ?? r.status}`)
  }
}
</script>

<template>
  <div class="imgsr-page">
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
        {{ files.length ? `已选 ${files.length} 张（点击继续追加）` : '点击选择图片（可多选批量入队）' }}
      </NButton>
      <div v-if="files.length" class="thumb-grid">
        <div v-for="(f, i) in files" :key="f" class="thumb-cell">
          <img :src="thumbUrl(f)" class="thumb" loading="lazy" alt="" />
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
          {{ selectedModel ? `已选 ${selectedModel.name} · x${targetScale}` : '先选模型' }}
        </span>
      </h2>
      <div class="form-rows">
        <div class="row stack">
          <span class="lbl">模型{{ selectedModel && !selectedModel.vram_ok ? '（显存不足，请换）' : '' }}</span>
          <NSelect
            v-model:value="modelId"
            :options="modelOptions"
            placeholder="选择超分模型"
            filterable
            style="max-width: 460px"
          />
        </div>
        <div class="row inline">
          <span class="lbl">放大倍数</span>
          <NRadioGroup v-model:value="targetScale" size="small">
            <NRadioButton v-for="s in selectedModel?.scale ?? []" :key="s" :value="s">x{{ s }}</NRadioButton>
          </NRadioGroup>
          <NTag
            v-if="files.length === 1"
            size="small"
            :bordered="false"
          >
            单张结果约 {{ targetScale * 100 }}% 尺寸增大
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
        <div class="row stack">
          <span class="lbl">分块大小（高级）</span>
          <NSelect v-model:value="tileChoice" :options="tileOptions" style="width: 200px" />
        </div>
        <p class="hint-row">
          自动=按模型默认；超大图（如 8K 扫描件）显存不足时调小分块。结果保存到「{{
            outDirLabel
          }}」，目录内无同名时沿用原文件名，同名冲突自动改用「原名_倍率」后缀，不覆盖现有文件。
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
