<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { NButton, NColorPicker, NInput, NInputNumber, NSelect, useMessage } from 'naive-ui'
import { api, type WatermarkOptions, type SubtitleOptions } from '../api'

const props = defineProps<{ modelValue: WatermarkOptions; inputs: string[]; width: number; height: number; subtitle?: SubtitleOptions }>()
const emit = defineEmits<{ 'update:modelValue': [value: WatermarkOptions]; ready: [value: boolean] }>()
const message = useMessage()
const update = (patch: Partial<WatermarkOptions>) => emit('update:modelValue', { ...props.modelValue, ...patch })
const renderOptions = computed<WatermarkOptions>(() => ({
  ...props.modelValue,
  position: props.modelValue.position ?? 'top-right',
  start_s: props.modelValue.start_s ?? 0,
  duration_s: props.modelValue.duration_s ?? 5,
}))
const ready = computed(() => props.modelValue.kind === 'text' ? !!props.modelValue.text?.trim() : !!props.modelValue.path?.trim())
watch(ready, v => emit('ready', v), { immediate: true })
async function pickLogo() {
  const path = await window.sv.pickLogo()
  if (path) update({ path })
}
const image = ref('')
const previewTime = ref<number | null>(null)
const loading = ref(false)
let sequence = 0
function clear() { sequence++; image.value = ''; loading.value = false }
// Controls may emit unchanged values on blur. Only rendering changes invalidate a preview.
watch(() => JSON.stringify([renderOptions.value, props.subtitle, props.inputs, props.width, props.height, previewTime.value]), clear)
onBeforeUnmount(clear)
function imageFailed() { clear(); message.error('水印预览图片加载失败，请重新预览') }
async function preview() {
  // Commit pending input/blur updates before capturing the request and its sequence.
  await nextTick()
  clear()
  const seq = sequence
  loading.value = true
  try {
    const response = await api.subtitlePreview({ input: props.inputs[0], watermark: renderOptions.value,
      subtitle: props.subtitle, width: props.width, height: props.height,
      time_s: previewTime.value ?? (props.modelValue.start_s ?? 0) + Math.min(.2, (props.modelValue.duration_s ?? 5) / 2) })
    if (!response.ok) throw new Error((await response.json()).detail ?? '水印预览失败')
    const blob = await response.blob()
    const url = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => resolve(String(reader.result))
      reader.onerror = () => reject(new Error('水印预览图片读取失败'))
      reader.readAsDataURL(blob)
    })
    if (seq !== sequence) return
    image.value = url
    const warnings = JSON.parse(response.headers.get('X-Subtitle-Warnings') ?? '[]') as string[]
    if (warnings.length) message.warning(warnings.join('\n'))
  } catch (error) {
    if (seq === sequence) message.error(error instanceof Error ? error.message : String(error))
  } finally { if (seq === sequence) loading.value = false }
}
</script>

<template>
  <div class="watermark-settings">
    <div class="controls">
      <label class="field">水印内容
        <NSelect :value="modelValue.kind" :options="[{ label: '文字水印', value: 'text' }, { label: '图片 Logo', value: 'image' }]" @update:value="kind => update({ kind })" />
      </label>
      <label v-if="modelValue.kind === 'text'" class="field">水印文字
        <NInput :value="modelValue.text ?? ''" type="textarea" :autosize="{ minRows: 2, maxRows: 5 }" :maxlength="100" placeholder="例如：频道名 / 制作署名" @update:value="text => update({ text })" />
        <span class="hint">按 Enter 换行，多行文字沿所选顶部位置对齐。</span>
      </label>
      <div v-else class="field">
        <span>Logo 图片</span>
        <div class="line"><NInput :value="modelValue.path ?? ''" placeholder="选择 PNG / JPG / WebP" @update:value="path => update({ path })" /><NButton @click="pickLogo">选择图片</NButton></div>
        <span class="hint">推荐透明 PNG；保留宽高比，不会拉伸。</span>
      </div>
      <label class="field">顶部位置
        <NSelect :value="modelValue.position ?? 'top-right'" :options="[{ label: '左上角', value: 'top-left' }, { label: '顶部居中', value: 'top-center' }, { label: '右上角', value: 'top-right' }]" @update:value="position => update({ position })" />
      </label>
      <div class="pair">
        <label class="field">开始时间（秒）<NInputNumber :value="modelValue.start_s ?? 0" :min="0" :max="86400" :step="0.1" @update:value="v => update({ start_s: v ?? 0 })" /></label>
        <label class="field">显示时长（秒）<NInputNumber :value="modelValue.duration_s ?? 5" :min="0.1" :max="3600" :step="0.1" @update:value="v => update({ duration_s: v ?? 5 })" /></label>
      </div>
      <div class="pair">
        <label class="field">不透明度<NInputNumber :value="Math.round((modelValue.opacity ?? .85) * 100)" :min="5" :max="100" @update:value="v => update({ opacity: (v ?? 85) / 100 })" /></label>
        <label class="field">边距（1080p 基准）<NInputNumber :value="modelValue.margin ?? 40" :min="0" :max="400" @update:value="v => update({ margin: v ?? 40 })" /></label>
      </div>
      <template v-if="modelValue.kind === 'text'">
        <label class="field">字体<NInput :value="modelValue.font_name ?? 'Microsoft YaHei'" @update:value="font_name => update({ font_name })" /></label>
        <div class="pair">
          <label class="field">字号（1080p 基准）<NInputNumber :value="modelValue.font_size ?? 48" :min="12" :max="144" @update:value="v => update({ font_size: v ?? 48 })" /></label>
          <label class="field">文字颜色<NColorPicker :value="modelValue.font_color ?? '#FFFFFF'" :modes="['hex']" :show-alpha="false" @update:value="font_color => update({ font_color })" /></label>
        </div>
      </template>
      <label v-else class="field">Logo 宽度（画面宽度 %）<NInputNumber :value="modelValue.width_pct ?? 12" :min="1" :max="50" @update:value="v => update({ width_pct: v ?? 12 })" /></label>
      <p class="hint">{{ modelValue.start_s ?? 0 }}–{{ (modelValue.start_s ?? 0) + (modelValue.duration_s ?? 5) }} 秒显示，之后自动消失。每个批量视频使用相同水印。</p>
    </div>
    <div class="preview">
      <div v-if="inputs.length === 1" class="line"><NInputNumber v-model:value="previewTime" :min="0" placeholder="片头 / 指定秒数" clearable /><NButton :disabled="!ready || width < 2 || height < 2" :loading="loading" @click="preview">预览水印</NButton></div>
      <img v-if="image" :src="image" alt="片头水印画面预览" @error="imageFailed" />
      <div v-else class="placeholder">填写文字或选择 Logo 后预览，检查位置与大小</div>
      <p class="hint">在最终输出尺寸绘制；预览使用缩放后的源画面，并显示已配置的字幕。</p>
    </div>
  </div>
</template>

<style scoped>
.watermark-settings { display: grid; grid-template-columns: minmax(0, 380px) minmax(0, 1fr); gap: 28px; }
.controls, .preview { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
.field { display: flex; flex-direction: column; gap: 6px; font-size: 12px; color: var(--sv-text-dim); }
.pair, .line { display: flex; gap: 8px; align-items: center; }
.pair > *, .line > :first-child { flex: 1; min-width: 0; }
.hint { font-size: 12px; color: var(--sv-text-dim); line-height: 1.6; margin: 0; }
.placeholder { display: grid; place-items: center; aspect-ratio: 16 / 9; border: 1px dashed var(--sv-border); border-radius: 8px; color: var(--sv-text-dim); font-size: 13px; padding: 24px; text-align: center; }
.preview img { width: 100%; border-radius: 8px; border: 1px solid var(--sv-border); }
@media (max-width: 1000px) { .watermark-settings { grid-template-columns: 1fr; } }
</style>
