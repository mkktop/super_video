<script setup lang="ts">
import { computed, onBeforeUnmount, onUnmounted, ref, watch } from 'vue'
import { NButton, NEmpty, NInput, NPopconfirm, NSpace, useDialog, useMessage } from 'naive-ui'
import TaskCard from '../components/TaskCard.vue'
import { api, type Task } from '../api'
import { refreshStats, refreshTasks, store, ui } from '../store'

const message = useMessage()
const dialog = useDialog()

// ---- 队列完成动作倒计时横幅（关机/休眠宽限期，可撤销） ----
// 秒级 tick 只在横幅存在时运行：无倒计时的日常会话不再每秒强制整页重渲染
const tick = ref(Date.now())
let tickTimer: ReturnType<typeof setInterval> | null = null
watch(
  () => store.queueAction,
  (qa) => {
    if (qa && !tickTimer) {
      tick.value = Date.now()
      tickTimer = setInterval(() => (tick.value = Date.now()), 1000)
    } else if (!qa && tickTimer) {
      clearInterval(tickTimer)
      tickTimer = null
    }
  },
  { immediate: true },
)
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
  } catch {
    message.error('取消失败（本地服务未连接？）')
  } finally {
    cancelingAction.value = false
  }
}

// ---- 处理时机挂起提示（定时/闲时模式：有排队任务但闸门未放行） ----
const gateSuspended = computed(() => {
  const g = store.stats.queue_gate
  if (!g || g.active) return null
  const hasWaiting = store.tasks.some((t) => t.status === 'queued' || t.status === 'running')
  return hasWaiting ? g.reason : null
})

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
  baseList.value.filter((t) => {
    if (filter.value === 'all') return true
    if (filter.value === 'active') return t.status === 'running' || t.status === 'queued'
    if (filter.value === 'done') return t.status === 'done'
    return t.status === 'failed' || t.status === 'canceled'
  }),
)
const doneCount = computed(() => baseList.value.filter((t) => t.status === 'done').length)

// ---- 搜索（按文件名/输出路径/模型，后端 LIKE 过滤） ----
// 结果存本地 searchList，不动 store.tasks——侧栏任务徽标/首页依赖全量列表，
// 搜索筛选不能污染全局。store.tasks 随 WS 更新时按 id 就地合入，卡片进度照常走。
const q = ref('')
const searching = ref(false)
const searchList = ref<Task[] | null>(null)
let qTimer: ReturnType<typeof setTimeout> | null = null
const baseList = computed(() => searchList.value ?? store.tasks)

function onSearchInput() {
  if (qTimer) clearTimeout(qTimer)
  qTimer = setTimeout(runSearch, 300)
}
async function runSearch() {
  const kw = q.value.trim()
  if (!kw) {
    searchList.value = null
    return
  }
  searching.value = true
  try {
    searchList.value = await api.tasks(kw)
  } catch {
    message.error('搜索失败（本地服务未连接？）')
  } finally {
    searching.value = false
  }
}
function clearSearch() {
  q.value = ''
  searchList.value = null
}
watch(
  () => store.tasks,
  (list) => {
    if (!searchList.value) return
    const byId = new Map(list.map((t) => [t.id, t]))
    searchList.value = searchList.value
      .map((t) => byId.get(t.id) ?? t)
      .filter((t) => byId.has(t.id) || t.status === 'running' || t.status === 'queued')
  },
)

// ---- 批量选择 ----
const selectMode = ref(false)
const selected = ref<Set<string>>(new Set())
function enterSelect() {
  selectMode.value = true
}
function exitSelect() {
  selectMode.value = false
  selected.value = new Set()
}
function toggleSelect(id: string) {
  const s = new Set(selected.value)
  if (s.has(id)) s.delete(id)
  else s.add(id)
  selected.value = s
}
function toggleAll() {
  if (filtered.value.every((t) => selected.value.has(t.id))) {
    selected.value = new Set()
  } else {
    selected.value = new Set(filtered.value.map((t) => t.id))
  }
}
const selCount = computed(() => selected.value.size)
const selCancelable = computed(() =>
  filtered.value.some((t) => selected.value.has(t.id) && (t.status === 'queued' || t.status === 'running')))
const selDeletable = computed(() =>
  filtered.value.some((t) => selected.value.has(t.id) && t.status !== 'running'))
const selResumable = computed(() =>
  filtered.value.some((t) => selected.value.has(t.id) && (t.status === 'failed' || t.status === 'canceled')))

const batchBusy = ref(false)
async function batchRun(action: 'cancel' | 'delete' | 'resume') {
  const ids = [...selected.value]
  if (!ids.length) return
  if (action === 'delete') {
    const confirmed = await new Promise<boolean>((resolve) => {
      dialog.warning({
        title: `删除 ${ids.length} 条任务记录`,
        content: '仅删除队列记录与临时产物，已生成的输出文件不受影响。确定吗？',
        positiveText: '删除',
        negativeText: '取消',
        onPositiveClick: () => resolve(true),
        onNegativeClick: () => resolve(false),
        onClose: () => resolve(false),
      })
    })
    if (!confirmed) return
  }
  batchBusy.value = true
  try {
    const r = await api.batchTasks(action, ids)
    const failN = Object.keys(r.failed).length
    if (r.done.length) {
      if (action === 'delete') message.success(`已删除 ${r.done.length} 条记录`)
      else if (action === 'cancel') message.success(`已取消 ${r.done.length} 个任务`)
      else message.success(`已将 ${r.done.length} 个任务重新入队`)
    }
    if (failN) {
      const first = Object.values(r.failed)[0]
      message.warning(`${failN} 项未处理：${first ?? '未知原因'}`)
    }
    const doneSet = new Set(r.done)
    selected.value = new Set([...selected.value].filter((id) => !doneSet.has(id)))
    refreshTasks()
    refreshStats()
    if (searchList.value) runSearch() // 删除后搜索结果需同步（含 300 历史上限刷新）
  } catch (e) {
    message.error(`批量操作失败: ${(e as Error).message}`)
  } finally {
    batchBusy.value = false
  }
}

// 离开任务页：退出选择模式、清空搜索（store 不落地，避免其他页被过滤）
watch(
  () => ui.page,
  (p) => {
    if (p !== 'tasks') {
      if (selectMode.value) exitSelect()
      if (q.value) clearSearch()
    }
  },
)
onBeforeUnmount(() => {
  if (qTimer) clearTimeout(qTimer)
})

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
          v-if="!selectMode && doneCount && !searchList"
          @positive-click="clearDone"
        >
          <template #trigger>
            <NButton size="small" :loading="cleaning">清理已完成（{{ doneCount }}）</NButton>
          </template>
          删除全部 {{ doneCount }} 条已完成任务的记录（输出文件不受影响）？
        </NPopconfirm>
        <NButton size="small" v-if="!selectMode" @click="enterSelect">批量选择</NButton>
        <NButton type="primary" @click="ui.page = 'newtask'">＋ 新建任务</NButton>
      </NSpace>
    </div>

    <!-- 处理时机挂起：定时/闲时模式下队列等待放行 -->
    <div v-if="gateSuspended" class="gate-banner">
      <span>⏸ 队列挂起中 · {{ gateSuspended }}（进行中的任务不受影响，跑完即停；可在 设置 → 处理时机 调整）</span>
    </div>

    <!-- 完成动作倒计时：新任务入队 / 手动取消 / 改设置都会撤销 -->
    <div v-if="queueBanner" class="queue-done-banner">
      <span class="qd-text">
        {{ bannerText === '关机' ? '⚠️' : '🌙' }} 任务队列已全部完成，<b class="qd-count">{{ secsLeft }}</b> 秒后{{ bannerText }}
      </span>
      <NButton size="small" :loading="cancelingAction" @click="cancelQueueAction">取消{{ bannerText }}</NButton>
    </div>

    <!-- 批量选择操作条 -->
    <div v-if="selectMode" class="batch-bar">
      <NButton size="small" @click="exitSelect">退出选择</NButton>
      <span class="bb-count">已选 {{ selCount }} 项</span>
      <span class="bb-spacer" />
      <NButton size="small" :disabled="!filtered.length" @click="toggleAll">
        {{ filtered.length && filtered.every((t) => selected.has(t.id)) ? '取消全选' : '全选本页' }}
      </NButton>
      <NButton size="small" type="warning" :disabled="!selCancelable || batchBusy" :loading="batchBusy"
               @click="batchRun('cancel')">批量取消</NButton>
      <NButton size="small" type="info" :disabled="!selResumable || batchBusy" :loading="batchBusy"
               @click="batchRun('resume')">批量继续</NButton>
      <NButton size="small" type="error" :disabled="!selDeletable || batchBusy" :loading="batchBusy"
               @click="batchRun('delete')">批量删除</NButton>
    </div>

    <div class="filter-bar">
      <div class="seg">
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
      <span class="fb-spacer" />
      <NInput
        v-model:value="q"
        size="small"
        clearable
        placeholder="搜索文件名 / 输出 / 模型"
        style="width: 220px"
        :loading="searching"
        @update:value="onSearchInput"
        @clear="clearSearch"
      />
    </div>

    <NEmpty
      v-if="store.ready && filtered.length === 0"
      :description="
        searchList
          ? '没有匹配的任务，换个关键词试试'
          : store.tasks.length
            ? '该筛选下没有任务'
            : '队列为空，点击右上角「新建任务」添加视频'
      "
      style="margin-top: 12vh"
    />
    <NSpace v-else vertical :size="12">
      <TaskCard
        v-for="t in filtered"
        :key="t.id"
        :task="t"
        :draggable="t.status === 'queued' && !selectMode"
        :can-up="t.status === 'queued' && queuedIds.indexOf(t.id) > 0"
        :can-down="t.status === 'queued' && queuedIds.indexOf(t.id) < queuedIds.length - 1"
        :select-mode="selectMode"
        :selected="selected.has(t.id)"
        :class="{ 'task-dragging': draggingId === t.id, 'task-drag-over': dragOverId === t.id }"
        @dragstart="onDragStart(t, $event)"
        @dragover="onDragOver(t, $event)"
        @drop="onDrop"
        @dragend="resetDrag"
        @move="moveTask(t.id, $event)"
        @retry-params="retryWithParams(t)"
        @toggle-select="toggleSelect(t.id)"
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
h1 { font-size: 21px; font-weight: 750; letter-spacing: 0.3px; }
.sub { font-size: 12.5px; color: #9aa1ad; margin-top: 4px; }
.filter-bar { display: flex; gap: 6px; align-items: center; }
.fb-spacer { flex: 1; }
/* 分段式筛选：凹槽容器 + 浮起选中片 */
.seg {
  display: inline-flex;
  gap: 2px;
  padding: 3px;
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.045);
  border: 1px solid rgba(255, 255, 255, 0.05);
}
.filter-btn {
  border: 1px solid transparent;
  background: transparent;
  color: #9aa1ad;
  font-size: 12.5px;
  padding: 4px 14px;
  border-radius: 8px;
  cursor: pointer;
  transition: all 0.16s;
}
.filter-btn:hover { color: #e9ecf2; }
.filter-btn.on {
  background: linear-gradient(180deg, #2a3040, #232837);
  border-color: rgba(79, 140, 255, 0.42);
  color: #8ab4ff;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.28), inset 0 1px 0 rgba(255, 255, 255, 0.07);
}
.loading { margin-top: 30vh; text-align: center; color: #9aa1ad; }
.batch-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border: 1px solid rgba(79, 140, 255, 0.4);
  border-radius: 12px;
  background: linear-gradient(90deg, rgba(79, 140, 255, 0.09), rgba(79, 140, 255, 0.03) 60%, transparent);
}
.bb-count { font-size: 13px; color: #e9ecf2; }
.bb-spacer { flex: 1; }
.task-dragging { opacity: 0.45; }
.task-drag-over { box-shadow: 0 0 0 2px #4f8cff; }

.queue-done-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 14px;
  border: 1px solid rgba(251, 191, 36, 0.45);
  border-radius: 12px;
  background: linear-gradient(90deg, rgba(251, 191, 36, 0.1), rgba(251, 191, 36, 0.04) 60%, transparent);
  font-size: 13px;
  color: #e9ecf2;
}
.qd-count {
  font-size: 16px;
  color: #fbbf24;
  font-variant-numeric: tabular-nums;
}

.gate-banner {
  padding: 10px 14px;
  border: 1px solid rgba(79, 140, 255, 0.4);
  border-radius: 12px;
  background: linear-gradient(90deg, rgba(79, 140, 255, 0.08), rgba(79, 140, 255, 0.03) 60%, transparent);
  font-size: 13px;
  color: #9aa1ad;
}
</style>
