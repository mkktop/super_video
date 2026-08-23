<script setup lang="ts">
import { computed, onMounted, onUnmounted } from 'vue'
import { NButton, NEmpty, NTag } from 'naive-ui'
import { api } from '../api'
import { store, ui } from '../store'
import CompareSlider from '../components/CompareSlider.vue'

const task = computed(() => store.tasks.find((t) => t.id === ui.compareTaskId))
const canCompare = computed(() => !!task.value?.preview_src && !!task.value?.preview_path)
const busy = computed(() => task.value?.status === 'running' || task.value?.status === 'queued')

const fileName = computed(() => task.value?.input_path.split(/[\\/]/).pop() ?? '')
const outName = computed(() => task.value?.output_path.split(/[\\/]/).pop() ?? '')
const modelName = computed(
  () => store.models.find((m) => m.id === task.value?.model_id)?.name ?? task.value?.model_id ?? '',
)
const scaleLabel = computed(() => {
  const t = task.value
  if (!t) return ''
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
    </div>

    <div class="stage">
      <CompareSlider
        v-if="canCompare"
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
