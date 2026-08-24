<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { NButton, NEmpty, NRadioButton, NRadioGroup, NTag } from 'naive-ui'
import { api } from '../api'
import { store, ui } from '../store'
import CompareSlider from '../components/CompareSlider.vue'
import VideoCompare from '../components/VideoCompare.vue'

const task = computed(() => store.tasks.find((t) => t.id === ui.compareTaskId))
const canCompare = computed(() => !!task.value?.preview_src && !!task.value?.preview_path)
// 视频对比直接播输入/输出文件，只有已完成的任务有输出；
// 图片序列任务的输出是目录（不是可播放的视频文件），按扩展名排除
const canVideo = computed(
  () => task.value?.status === 'done' && !!task.value.input_path && !!task.value.output_path
    && /\.(mp4|m4v|mov|webm|mkv)$/i.test(task.value.output_path),
)
const mode = ref<'frames' | 'video'>('frames')
const busy = computed(() => task.value?.status === 'running' || task.value?.status === 'queued')

const fileName = computed(() => task.value?.input_path.split(/[\\/]/).pop() ?? '')
const outName = computed(() => task.value?.output_path.split(/[\\/]/).pop() ?? '')
const modelName = computed(
  () => store.models.find((m) => m.id === task.value?.model_id)?.name ?? task.value?.model_id ?? '',
)
const scaleLabel = computed(() => {
  const t = task.value
  if (!t) return ''
  const tw = t.params?.target_w as number | undefined
  const th = t.params?.target_h as number | undefined
  if (tw && th) return `${t.src_w}x${t.src_h} → ${tw}x${th}`
  const k = Number(t.params?.target_scale ?? t.params?.scale ?? 4)
  return `${t.src_w}x${t.src_h} → ${t.src_w * k}x${t.src_h * k}`
})

function back() {
  ui.compareTaskId = null
  ui.page = 'tasks'
}
function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape') back()
}
onMounted(() => window.addEventListener('keydown', onKey))
onUnmounted(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <div class="compare-page">
    <div class="head">
      <NButton size="small" quaternary @click="back">← 返回任务</NButton>
      <div class="names">
        <span class="file">{{ fileName }}</span>
        <span class="arrow">→</span>
        <span class="out">{{ outName }}</span>
      </div>
      <div class="tags">
        <NTag size="small" :bordered="false">{{ modelName }}</NTag>
        <NTag size="small" :bordered="false" type="info">{{ scaleLabel }}</NTag>
      </div>
      <NRadioGroup v-model:value="mode" size="small">
        <NRadioButton value="frames">静帧</NRadioButton>
        <NRadioButton value="video" :disabled="!canVideo">视频</NRadioButton>
      </NRadioGroup>
    </div>

    <div class="stage">
      <VideoCompare
        v-if="mode === 'video' && canVideo"
        :src-path="task!.input_path"
        :out-path="task!.output_path"
      />
      <CompareSlider
        v-else-if="canCompare"
        :src-url="api.previewUrl(task!.id, task!.updated_at, true)"
        :out-url="api.previewUrl(task!.id, task!.updated_at)"
      />
      <NEmpty
        v-else
        :description="busy ? '任务还在处理中，完成前几秒会出现首对预览' : '该任务没有可对比的预览图'"
        class="empty"
      >
        <template #extra>
          <NButton size="small" @click="back">返回</NButton>
        </template>
      </NEmpty>
    </div>
  </div>
</template>

<style scoped>
.compare-page {
  display: flex;
  flex-direction: column;
  width: 100%; /* page-full 是横向 flex 容器，舞台内容全为绝对定位不撑宽，不显式给宽会塌缩成头部行宽 */
  height: 100%;
  gap: 10px;
}
.head {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-shrink: 0;
}
.names {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  overflow: hidden;
}
.file { font-weight: 600; white-space: nowrap; }
.arrow, .out { color: #9aa0a6; font-size: 13px; white-space: nowrap; }
.file, .out { overflow: hidden; text-overflow: ellipsis; }
.tags { display: flex; gap: 6px; flex-shrink: 0; margin-left: auto; }
.stage {
  flex: 1;
  min-height: 0;
  border: 1px solid #2a2d31;
  border-radius: 8px;
  background: #0d0e10;
}
.empty { margin: auto; }
</style>
