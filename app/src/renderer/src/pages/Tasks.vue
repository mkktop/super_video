<script setup lang="ts">
import { ref } from 'vue'
import { NButton, NEmpty, NSpace } from 'naive-ui'
import TaskCard from '../components/TaskCard.vue'
import { api, type Task } from '../api'
import { refreshTasks, store, ui } from '../store'

// 拖拽重排：仅排队任务可拖；拖到排队卡上按指针上下半区插前/后，拖到运行卡上插队首
const draggingId = ref('')
const dragOverId = ref('')
const dragOverBefore = ref(false)

function resetDrag() {
  draggingId.value = ''
  dragOverId.value = ''
}

function onDragStart(t: Task, ev: DragEvent) {
  draggingId.value = t.id
  if (ev.dataTransfer) ev.dataTransfer.effectAllowed = 'move'
}

function onDragOver(t: Task, ev: DragEvent) {
  if (!draggingId.value || draggingId.value === t.id) return
  ev.preventDefault() // 允许放置
  if (t.status === 'queued') {
    const rect = (ev.currentTarget as HTMLElement).getBoundingClientRect()
    dragOverBefore.value = ev.clientY < rect.top + rect.height / 2
    dragOverId.value = t.id
  } else if (t.status === 'running') {
    dragOverBefore.value = true // 放到运行卡 = 插到队首
    dragOverId.value = t.id
  }
}

function onDrop() {
  const dragId = draggingId.value
  const overId = dragOverId.value
  resetDrag()
  if (!dragId || !overId || dragId === overId) return
  const over = store.tasks.find((t) => t.id === overId)
  if (!over) return
  const order = store.tasks.filter((t) => t.status === 'queued').map((t) => t.id).filter((id) => id !== dragId)
  const idx = over.status === 'queued' ? order.indexOf(overId) + (dragOverBefore.value ? 0 : 1) : 0
  order.splice(idx, 0, dragId)
  // 乐观更新本地顺序；失败/刷新会被服务端顺序覆盖
  const runningTask = store.tasks.find((t) => t.status === 'running')
  const rest = store.tasks.filter((t) => t.status !== 'queued' && t.status !== 'running')
  store.tasks = [
    ...(runningTask ? [runningTask] : []),
    ...order.map((id) => store.tasks.find((t) => t.id === id)!).filter(Boolean),
    ...rest,
  ]
  api.reorderTasks(order).then(refreshTasks).catch(refreshTasks)
}
</script>

<template>
  <div class="tasks-page">
    <div class="page-head">
      <div>
        <h1>任务队列</h1>
        <p class="sub">严格串行执行 · 一次处理一个视频 · 拖拽排队任务可调整顺序</p>
      </div>
      <NButton type="primary" @click="ui.page = 'newtask'">＋ 新建任务</NButton>
    </div>

    <NEmpty
      v-if="store.ready && store.tasks.length === 0"
      description="队列为空，点击右上角「新建任务」添加视频"
      style="margin-top: 15vh"
    />
    <NSpace v-else vertical :size="12">
      <TaskCard
        v-for="t in store.tasks"
        :key="t.id"
        :task="t"
        :draggable="t.status === 'queued'"
        :class="{ 'task-dragging': draggingId === t.id, 'task-drag-over': dragOverId === t.id }"
        @dragstart="onDragStart(t, $event)"
        @dragover="onDragOver(t, $event)"
        @drop="onDrop"
        @dragend="resetDrag"
      />
    </NSpace>
    <div v-if="!store.ready" class="loading">
      {{ store.initError ? '后端连接失败，请使用上方提示中的"重试"按钮' : '正在连接后端服务…' }}
    </div>
  </div>
</template>

<style scoped>
.tasks-page { display: flex; flex-direction: column; gap: 16px; }
.page-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
h1 { font-size: 20px; font-weight: 700; }
.sub { font-size: 12.5px; color: #9aa0a6; margin-top: 4px; }
.loading { margin-top: 30vh; text-align: center; color: #9aa0a6; }
.task-dragging { opacity: 0.45; }
.task-drag-over { box-shadow: 0 0 0 2px #4f8cff; }
</style>
