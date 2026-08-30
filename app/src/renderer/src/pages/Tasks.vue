<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { NButton, NEmpty, NPopconfirm, NSpace } from 'naive-ui'
import TaskCard from '../components/TaskCard.vue'
import { api, type Task } from '../api'
import { refreshStats, refreshTasks, store, ui } from '../store'

// ---- 队列完成动作倒计时横幅（关机/休眠宽限期，可撤销） ----
const tick = ref(Date.now())
let tickTimer: ReturnType<typeof setInterval> | null = null
onMounted(() => {
  tickTimer = setInterval(() => (tick.value = Date.now()), 1000)
})
onUnmounted(() => {
  if (tickTimer) clearInterval(tickTimer)
})
const queueBanner = computed(() => (tick.value, store.queueAction))
const secsLeft = computed(() =>
  queueBanner.value ? Math.max(0, Math.ceil((queueBanner.value.endsAt - tick.value) / 1000)) : 0,
)
const bannerText = computed(() =>
  queueBanner.value?.action === 'sleep' ? '休眠' : '关机',
)
const cancelingAction = ref(false)
async function cancelQueueAction() {
  cancelingAction.value = true
  try {
    await api.cancelQueueAction()
    store.queueAction = null // 乐观清横幅；WS 取消事件随迟到不重复伤害
  } finally {
    cancelingAction.value = false
  }
}

// ---- 状态筛选 ----
type Filter = 'all' | 'active' | 'done' | 'failed'
const filter = ref<Filter>('all')
const filterTabs: Array<{ key: Filter; label: string }> = [
  { key: 'all', label: '全部' },
  { key: 'active', label: '进行中' },
  { key: 'done', label: '已完成' },
  { key: 'failed', label: '失败/取消' },
]
const filtered = computed(() =>
  store.tasks.filter((t) => {
    if (filter.value === 'all') return true
    if (filter.value === 'active') return t.status === 'running' || t.status === 'queued'
    if (filter.value === 'done') return t.status === 'done'
    return t.status === 'failed' || t.status === 'canceled'
  }),
)
const doneCount = computed(() => store.tasks.filter((t) => t.status === 'done').length)

// ---- 批量清理已完成 ----
const cleaning = ref(false)
async function clearDone() {
  cleaning.value = true
  for (const t of store.tasks.filter((x) => x.status === 'done')) {
    await api.remove(t.id)
  }
  cleaning.value = false
  refreshTasks()
  refreshStats()
  // 卡片即时消失本身就是反馈，不额外弹 message
  if (filter.value === 'done') filter.value = 'all'
}

// ---- 排队顺序调整（拖拽 + 箭头微调共用） ----
// 拖拽：仅排队任务可拖；拖到排队卡上按指针上下半区插前/后，拖到运行卡上插队首
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

/** 按新顺序乐观更新本地列表并提交服务端；失败/刷新会被服务端顺序覆盖 */
function applyOrder(order: string[]) {
  const runningTask = store.tasks.find((t) => t.status === 'running')
  const rest = store.tasks.filter((t) => t.status !== 'queued' && t.status !== 'running')
  store.tasks = [
    ...(runningTask ? [runningTask] : []),
    ...order.map((id) => store.tasks.find((t) => t.id === id)!).filter(Boolean),
    ...rest,
  ]
  api.reorderTasks(order).then(refreshTasks).catch(refreshTasks)
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
  applyOrder(order)
}

// 箭头微调：排队任务在队列内上移/下移一位（触控板拖拽不好精准操作）
const queuedIds = computed(() =>
  store.tasks.filter((t) => t.status === 'queued').map((t) => t.id),
)
function moveTask(id: string, dir: -1 | 1) {
  const order = [...queuedIds.value]
  const i = order.indexOf(id)
  const j = i + dir
  if (i < 0 || j < 0 || j >= order.length) return
  ;[order[i], order[j]] = [order[j], order[i]]
  applyOrder(order)
}

// 失败/取消任务「改参数重试」：带原参数跳新建任务页
function retryWithParams(t: Task) {
  ui.pendingTaskParams = t
  ui.page = 'newtask'
}
</script>

<template>
  <div class="tasks-page">
    <div class="page-head">
      <div>
        <h1>任务队列</h1>
        <p class="sub">
          严格串行执行 · 拖拽排队任务调整顺序（拖到运行中的任务上=插到队首）· 悬停卡片可用箭头微调
        </p>
      </div>
      <NSpace :size="8">
        <NPopconfirm
          v-if="doneCount"
          @positive-click="clearDone"
        >
          <template #trigger>
            <NButton size="small" :loading="cleaning">清理已完成（{{ doneCount }}）</NButton>
          </template>
          删除全部 {{ doneCount }} 条已完成任务的记录（输出文件不受影响）？
        </NPopconfirm>
        <NButton type="primary" @click="ui.page = 'newtask'">＋ 新建任务</NButton>
      </NSpace>
    </div>

    <!-- 完成动作倒计时：新任务入队 / 手动取消 / 改设置都会撤销 -->
    <div v-if="queueBanner" class="queue-done-banner">
      <span class="qd-text">
        {{ bannerText === '关机' ? '⚠️' : '🌙' }} 任务队列已全部完成，<b class="qd-count">{{ secsLeft }}</b> 秒后{{ bannerText }}
      </span>
      <NButton size="small" :loading="cancelingAction" @click="cancelQueueAction">取消{{ bannerText }}</NButton>
    </div>

    <div class="filter-bar">
      <button
        v-for="ft in filterTabs"
        :key="ft.key"
        class="filter-btn"
        :class="{ on: filter === ft.key }"
        @click="filter = ft.key"
      >
        {{ ft.label }}
      </button>
    </div>

    <NEmpty
      v-if="store.ready && filtered.length === 0"
      :description="store.tasks.length ? '该筛选下没有任务' : '队列为空，点击右上角「新建任务」添加视频'"
      style="margin-top: 12vh"
    />
    <NSpace v-else vertical :size="12">
      <TaskCard
        v-for="t in filtered"
        :key="t.id"
        :task="t"
        :draggable="t.status === 'queued'"
        :can-up="t.status === 'queued' && queuedIds.indexOf(t.id) > 0"
        :can-down="t.status === 'queued' && queuedIds.indexOf(t.id) < queuedIds.length - 1"
        :class="{ 'task-dragging': draggingId === t.id, 'task-drag-over': dragOverId === t.id }"
        @dragstart="onDragStart(t, $event)"
        @dragover="onDragOver(t, $event)"
        @drop="onDrop"
        @dragend="resetDrag"
        @move="moveTask(t.id, $event)"
        @retry-params="retryWithParams(t)"
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
  gap: 16px;
  flex-wrap: wrap;
}
h1 { font-size: 20px; font-weight: 700; }
.sub { font-size: 12.5px; color: #9aa0a6; margin-top: 4px; }
.filter-bar { display: flex; gap: 6px; }
.filter-btn {
  border: 1px solid #2a2d31;
  background: #1e2023;
  color: #9aa0a6;
  font-size: 12.5px;
  padding: 5px 14px;
  border-radius: 7px;
  cursor: pointer;
  transition: all 0.15s;
}
.filter-btn:hover { color: #e8eaed; }
.filter-btn.on {
  background: rgba(79, 140, 255, 0.14);
  border-color: rgba(79, 140, 255, 0.5);
  color: #4f8cff;
}
.loading { margin-top: 30vh; text-align: center; color: #9aa0a6; }
.task-dragging { opacity: 0.45; }
.task-drag-over { box-shadow: 0 0 0 2px #4f8cff; }

.queue-done-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 14px;
  border: 1px solid rgba(251, 191, 36, 0.45);
  border-radius: 8px;
  background: rgba(251, 191, 36, 0.08);
  font-size: 13px;
  color: #e8eaed;
}
.qd-count {
  font-size: 16px;
  color: #fbbf24;
  font-variant-numeric: tabular-nums;
}
</style>
