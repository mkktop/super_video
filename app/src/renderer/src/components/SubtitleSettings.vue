<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { NButton, NColorPicker, NInput, NInputNumber, NSelect, useMessage } from 'naive-ui'
import { api, type ProbeInfo, type SubtitleOptions, type WatermarkOptions } from '../api'

const props = defineProps<{
  modelValue: SubtitleOptions
  inputs: string[]
  probe: ProbeInfo | null
  width: number
  height: number
  watermark?: WatermarkOptions
}>()
const emit = defineEmits<{
  'update:modelValue': [value: SubtitleOptions]
  ready: [value: boolean]
}>()
const message = useMessage()
const update = (patch: Partial<SubtitleOptions>) => emit('update:modelValue', { ...props.modelValue, ...patch })
const sources = computed(() => [
  { label: '视频内字幕轨', value: 'embedded' },
  { label: '外部字幕文件', value: 'external', disabled: props.inputs.length > 1 },
  { label: '自动匹配同名字幕', value: 'matching' },
])
const tracks = computed(() => props.probe?.subtitle_tracks?.map(t => ({
  value: t.stream, label: `${t.stream + 1} · ${t.title || t.language} · ${t.codec}${t.forced ? ' · 强制' : ''}`,
  disabled: !t.burn_supported,
})) ?? props.probe?.subtitles?.map((codec, stream) => ({ value: stream, label: `${stream + 1} · ${codec}`, disabled: !['subrip', 'ass', 'ssa', 'mov_text', 'webvtt'].includes(codec) })) ?? [])
const ready = computed(() => {
  const o = props.modelValue
  if (props.probe?.subtitle_burn?.supported === false) return false
  if (o.source === 'external') return !!o.path?.trim() && props.inputs.length === 1
  if (o.source === 'matching') return props.inputs.length > 0
  if (o.selection === 'match' || props.inputs.length > 1) return !!(o.language?.trim() || o.title?.trim())
  return tracks.value.some(t => t.value === (o.stream ?? 0) && !t.disabled)
})
watch(() => [props.inputs.length, props.modelValue.source], () => {
  if (props.inputs.length > 1 && props.modelValue.source === 'embedded' && props.modelValue.selection !== 'match') update({ selection: 'match', stream: undefined })
}, { immediate: true })
watch(ready, v => emit('ready', v), { immediate: true })
const isPlain = computed(() => props.modelValue.source === 'external'
  ? !props.modelValue.path || /\.srt$/i.test(props.modelValue.path)
  : props.modelValue.source === 'matching' || props.modelValue.selection === 'match' || !['ass', 'ssa'].includes(props.probe?.subtitles?.[props.modelValue.stream ?? 0] ?? ''))
const editStyle = computed(() => isPlain.value || props.modelValue.style_mode === 'custom')
const mayHaveAss = computed(() => !isPlain.value || props.modelValue.source === 'matching' || props.modelValue.selection === 'match')
async function pickSubtitle() {
  const path = await window.sv.pickSubtitle()
  if (path) update({ path })
}
async function pickFonts() {
  const fonts_dir = await window.sv.pickDir()
  if (fonts_dir) update({ fonts_dir })
}
const previewUrl = ref('')
const previewTime = ref<number | null>(null)
const renderedTime = ref('')
const previewing = ref(false)
let sequence = 0
function clearPreview() {
  sequence++
  previewing.value = false
  previewUrl.value = ''
}
function imageFailed() {
  clearPreview()
  message.error('字幕预览图片加载失败，请重新预览')
}
watch(() => [props.modelValue, props.watermark, props.inputs, props.width, props.height, previewTime.value], clearPreview, { deep: true })
onBeforeUnmount(clearPreview)
async function preview() {
  clearPreview()
  const seq = sequence
  previewing.value = true
  try {
    const response = await api.subtitlePreview({ input: props.inputs[0], subtitle: props.modelValue,
      watermark: props.watermark,
      width: props.width, height: props.height,
      ...(previewTime.value === null ? {} : { time_s: previewTime.value }),
    })
    if (!response.ok) throw new Error((await response.json()).detail ?? '字幕预览失败')
    const blob = await response.blob()
    // The desktop CSP permits data: images; blob: object URLs are blocked.
    const imageUrl = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => resolve(String(reader.result))
      reader.onerror = () => reject(new Error('字幕预览图片读取失败'))
      reader.readAsDataURL(blob)
    })
    if (seq !== sequence) return
    previewUrl.value = imageUrl
    renderedTime.value = response.headers.get('X-Subtitle-Time') ?? ''
    const warnings = JSON.parse(response.headers.get('X-Subtitle-Warnings') ?? '[]') as string[]
    if (warnings.length) message.warning(warnings.join('\n'))
  } catch (error) {
    if (seq === sequence) message.error(error instanceof Error ? error.message : String(error))
  } finally {
    if (seq === sequence) previewing.value = false
  }
}
</script>

<template>
  <div class="subtitle-settings">
    <div class="subtitle-controls">
    <label class="subtitle-field">字幕来源
      <NSelect :value="modelValue.source" :options="sources" @update:value="source => update({ source })" />
    </label>
    <label v-if="modelValue.source === 'embedded' && inputs.length === 1" class="subtitle-field">选择方式
      <NSelect :value="modelValue.selection ?? 'track'" :options="[{ label: '选择指定字幕轨', value: 'track' }, { label: '按语言 / 标题匹配', value: 'match' }]" @update:value="selection => update({ selection })" />
    </label>
    <label v-if="modelValue.source === 'embedded' && inputs.length === 1 && modelValue.selection !== 'match'" class="subtitle-field">字幕轨
      <NSelect :value="modelValue.stream ?? 0" :options="tracks" placeholder="选择文本字幕轨"
        @update:value="stream => update({ stream })" />
      <span class="subtitle-hint">PGS 等位图字幕暂不支持烧录，可改为保留字幕轨。</span>
    </label>
    <template v-if="modelValue.source === 'embedded' && (inputs.length > 1 || modelValue.selection === 'match')">
      <label class="subtitle-field">字幕语言（可选）
        <NInput :value="modelValue.language ?? ''" placeholder="zh 中文 / ja 日语 / en 英语" @update:value="language => update({ language })" />
      </label>
      <label class="subtitle-field">标题包含（可选）
        <NInput :value="modelValue.title ?? ''" placeholder="例如：简体、繁体、CHS、CHT" @update:value="title => update({ title })" />
      </label>
      <span class="subtitle-hint">至少填写一项；两项同时填写时需同时匹配。每个视频独立查找唯一文本字幕，不依赖轨道顺序。无匹配或多个候选时停止并提示。</span>
    </template>
    <div v-if="modelValue.source === 'external'" class="subtitle-field">
      <span>字幕文件</span>
      <div class="subtitle-line"><NInput :value="modelValue.path ?? ''" :input-props="{ 'aria-label': '字幕文件' }" placeholder="选择 SRT / ASS / SSA" @update:value="path => update({ path })" />
        <NButton @click="pickSubtitle">选择字幕</NButton></div>
    </div>
    <label v-if="modelValue.source === 'external'" class="subtitle-field">字幕编码
      <NSelect :value="modelValue.encoding ?? 'utf-8-sig'" :options="[{ label: 'UTF-8', value: 'utf-8-sig' }, { label: 'GB18030 / GBK', value: 'gb18030' }, { label: 'Big5', value: 'big5' }]" @update:value="encoding => update({ encoding })" />
    </label>
    <p v-if="modelValue.source === 'matching'" class="subtitle-hint">每个视频需有唯一同名的 SRT、ASS 或 SSA 文件。多个候选时会提示选择。</p>
    <label v-if="mayHaveAss" class="subtitle-field">ASS / SSA 样式
      <NSelect :value="modelValue.style_mode ?? 'preserve'" :options="[{ label: '保留原样式', value: 'preserve' }, { label: '自定义覆盖基础样式', value: 'custom' }]" @update:value="style_mode => update({ style_mode })" />
    </label>
    <span v-if="mayHaveAss && modelValue.style_mode === 'custom'" class="subtitle-hint">覆盖基础字体、字号、颜色、描边、阴影和垂直边距，保留原定位与动画。字幕中的行内样式及单行边距仍优先，建议检查预览。</span>
    <div class="subtitle-pair">
      <label class="subtitle-field">字幕延迟（秒）
        <NInputNumber :value="modelValue.delay_s ?? 0" :min="-3600" :max="3600" :step="0.1" @update:value="v => update({ delay_s: v ?? 0 })" />
      </label>
      <label v-if="editStyle" class="subtitle-field">字号（1080p 基准）
        <NInputNumber :value="modelValue.font_size ?? 48" :min="12" :max="144" @update:value="v => update({ font_size: v ?? 48 })" />
      </label>
    </div>
    <span class="subtitle-hint">延迟为正值时晚显示，为负值时提前显示。</span>
    <template v-if="editStyle">
      <label class="subtitle-field">字体
        <NInput :value="modelValue.font_name ?? 'Microsoft YaHei'" @update:value="font_name => update({ font_name })" />
      </label>
      <div class="subtitle-pair">
        <label class="subtitle-field">字体颜色
          <NColorPicker :value="modelValue.font_color ?? '#FFFFFF'" :modes="['hex']" :show-alpha="false" @update:value="font_color => update({ font_color })" />
        </label>
        <label class="subtitle-field">阴影
          <NInputNumber :value="modelValue.shadow ?? 1" :min="0" :max="8" :step="0.5" @update:value="v => update({ shadow: v ?? 1 })" />
        </label>
      </div>
      <div class="subtitle-pair">
        <label class="subtitle-field">描边
          <NInputNumber :value="modelValue.outline ?? 2" :min="0" :max="8" :step="0.5" @update:value="v => update({ outline: v ?? 2 })" />
        </label>
        <label class="subtitle-field">{{ !isPlain ? '垂直边距' : '底部边距' }}
          <NInputNumber :value="modelValue.margin_v ?? 50" :min="0" :max="400" @update:value="v => update({ margin_v: v ?? 50 })" />
        </label>
      </div>
    </template>
    <span v-else class="subtitle-hint">ASS / SSA 保留原字体、位置和动画效果。</span>
    <span v-if="(modelValue.source === 'matching' || modelValue.selection === 'match') && modelValue.style_mode !== 'custom'" class="subtitle-hint">匹配到普通文本字幕时使用以上样式；ASS / SSA 保留字幕原样式。</span>
    <div class="subtitle-field">
      <span>字体目录（可选）</span>
      <div class="subtitle-line"><NInput :value="modelValue.fonts_dir ?? ''" :input-props="{ 'aria-label': '字体目录（可选）' }" clearable placeholder="补充字幕所需的字体" @update:value="fonts_dir => update({ fonts_dir })" />
        <NButton @click="pickFonts">选择目录</NButton></div>
    </div>
    </div>
    <div class="subtitle-preview-area">
    <span v-if="probe?.subtitle_burn?.supported === false" class="subtitle-hint">{{ probe.subtitle_burn.error }}</span>
    <div v-if="inputs.length === 1" class="subtitle-line">
      <NInputNumber v-model:value="previewTime" :min="0" :max="probe?.duration_s" placeholder="首条字幕 / 指定秒数" clearable />
      <NButton :disabled="!ready || width < 2 || height < 2" :loading="previewing" @click="preview">预览字幕</NButton>
    </div>
    <template v-if="previewUrl">
      <img :src="previewUrl" class="subtitle-preview" alt="字幕烧录画面预览" @error="imageFailed" />
      <span class="subtitle-hint">{{ renderedTime }} 秒 · 源画面缩放到输出尺寸的字幕预览</span>
    </template>
    <div v-else class="subtitle-placeholder">{{ inputs.length === 1 ? '选择字幕后预览，检查字体、位置与显示时间' : '字幕将逐个匹配，建议先用单个视频检查样式' }}</div>
    <span class="subtitle-hint">字幕在最终尺寸上绘制，烧录后始终显示。已有画面内字幕不会自动移除。</span>
    </div>
  </div>
</template>

<style scoped>
.subtitle-settings { display: grid; grid-template-columns: minmax(0, 380px) minmax(0, 1fr); gap: 28px; width: 100%; }
.subtitle-controls, .subtitle-preview-area { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
.subtitle-placeholder { display: grid; place-items: center; aspect-ratio: 16 / 9; border: 1px dashed var(--sv-border); border-radius: 8px; color: var(--sv-text-dim); font-size: 13px; line-height: 1.8; padding: 24px; text-align: center; }
.subtitle-field { display: flex; flex-direction: column; gap: 6px; font-size: 12px; color: var(--sv-text-dim); }
.subtitle-line, .subtitle-pair { display: flex; gap: 8px; align-items: center; }
.subtitle-line > :first-child { flex: 1; min-width: 0; }
.subtitle-pair > * { flex: 1; min-width: 0; }
.subtitle-hint { font-size: 12px; line-height: 1.6; color: var(--sv-text-dim); margin: 0; }
.subtitle-preview { display: block; width: 100%; border-radius: 8px; border: 1px solid var(--sv-border); }
@media (max-width: 1000px) { .subtitle-settings { grid-template-columns: 1fr; gap: 20px; } }
</style>
