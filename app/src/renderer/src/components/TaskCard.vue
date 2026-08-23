<script setup lang="ts">
import { computed } from 'vue'
import {
  NButton,
  NCard,
  NCollapse,
  NCollapseItem,
  NProgress,
  NTag,
  useMessage,
} from 'naive-ui'
import { api, type Task } from '../api'
import { store } from '../store'

const props = defineProps<{ task: Task }>()
const message = useMessage()

const fileName = computed(() => props.task.input_path.split(/[\\/]/).pop() ?? '')
const outName = computed(() => props.task.output_path.split(/[\\/]/).pop() ?? '')
const modelName = computed(
  () => store.models.find((m) => m.id === props.task.model_id)?.name ?? props.task.model_id,
)

const percent = computed(() => {
  const { progress_frames: p, total_frames: t } = props.task
  if (!t) return 0
  return Math.min(100, Math.round((p / t) * 100))
})

const statusMeta: Record<Task['status'], { label: string; type: 'default' | 'info' | 'success' | 'error' | 'warning'; pulse?: boolean }> = {
  queued: { label: '排队中', type: 'default' },
  running: { label: '运行中', type: 'info', pulse: true },
  done: { label: '完成', type: 'success' },
  failed: { label: '失败', type: 'error' },
  canceled: { label: '已取消', type: 'warning' },
}

function fmtEta(sec: number): string {
  if (!sec || sec < 0) return '--'
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  const s = sec % 60
  return h ? `${h}时${m}分` : m ? `${m}分${s}秒` : `${s}秒`
}

function fmtBytes(b: number): string {
  return b > 1e9 ? `${(b / 1e9).toFixed(2)}GB` : b > 1e6 ? `${(b / 1e6).toFixed(1)}MB` : `${b}B`
}

const scaleLabel = computed(() => {
  const s = Number(props.task.params?.scale ?? 4)
  return `${props.task.src_w}x${props.task.src_h} → ${props.task.src_w * s}x${props.task.src_h * s}`
})

const isBusy = computed(() => props.task.status === 'running' || props.task.status === 'queued')

async function onCancel() {
  const r = await api.cancel(props.task.id)
  if (!r.ok) message.error(`取消失败: ${(await r.json()).detail ?? r.status}`)
}

async function onDelete() {
  const r = await api.remove(props.task.id)
  if (!r.ok) message.error(`删除失败: ${(await r.json()).detail ?? r.status}`)
}

function onOpenFolder() {
  window.sv.showInFolder(props.task.output_path)
}
</script>

<template>
  <n-card size="small" :bordered="true" class="task-card">
    <div class="row1">
      <div class="names">
        <span class="file">{{ fileName }}</span>
        <span class="arrow">→</span>
        <span class="out">{{ outName }}</span>
      </div>
      <div class="badges">
        <n-tag size="small" :bordered="false">{{ modelName }}</n-tag>
        <n-tag size="small" :bordered="false" type="info">x{{ task.params?.scale ?? 4 }}</n-tag>
        <n-tag size="small" :bordered="false" :type="statusMeta[task.status].type">
          {{ statusMeta[task.status].label }}{{ task.queue_position ? ` #${task.queue_position}` : '' }}
        </n-tag>
      </div>
    </div>

    <div v-if="task.status === 'running' || task.status === 'done'" class="progress-wrap">
      <n-progress
        type="line"
        :percentage="percent"
        :status="task.status === 'done' ? 'success' : 'default'"
        :show-indicator="false"
        :height="8"
      />
      <div class="stats">
        <span>{{ scaleLabel }}</span>
        <span v-if="task.status === 'running'">
          {{ task.progress_frames }}/{{ task.total_frames }} 帧 · {{ task.fps_run.toFixed(1) }} fps ·
          剩余 {{ fmtEta(task.eta_sec) }}
        </span>
        <span v-else>{{ fmtBytes(task.out_bytes) }}</span>
      </div>
    </div>

    <n-collapse v-if="task.error" class="err">
      <n-collapse-item title="错误信息" name="err">
        <div class="err-text">{{ task.error }}</div>
      </n-collapse-item>
    </n-collapse>

    <div class="row3">
      <img
        v-if="task.preview_path || task.status === 'done'"
        :src="api.previewUrl(task.id, task.updated_at)"
        class="preview"
      />
      <div class="spacer" />
      <n-button v-if="isBusy" size="small" quaternary type="error" @click="onCancel">取消</n-button>
      <n-button v-if="task.status === 'done'" size="small" quaternary type="info" @click="onOpenFolder">
        打开所在文件夹
      </n-button>
      <n-button v-if="!isBusy" size="small" quaternary @click="onDelete">删除</n-button>
    </div>
  </n-card>
</template>

<style scoped>
.task-card { background: #1e2023; }
.row1 { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.names { display: flex; align-items: center; gap: 8px; min-width: 0; }
.file { font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.arrow, .out { color: #9aa0a6; font-size: 13px; }
.out { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.badges { display: flex; gap: 6px; flex-shrink: 0; }
.progress-wrap { margin-top: 10px; }
.stats {
  display: flex; justify-content: space-between; margin-top: 6px;
  font-size: 12px; color: #9aa0a6; font-variant-numeric: tabular-nums;
}
.err { margin-top: 8px; }
.err-text { color: #f87171; font-size: 12px; word-break: break-all; white-space: pre-wrap; }
.row3 { display: flex; align-items: flex-end; gap: 10px; margin-top: 10px; }
.preview {
  max-height: 96px; max-width: 45%; border-radius: 6px; border: 1px solid #2a2d31;
  object-fit: contain;
}
.spacer { flex: 1; }
</style>
