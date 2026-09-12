<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import {
  NButton,
  NCard,
  NCheckbox,
  NCollapse,
  NCollapseItem,
  NModal,
  NPopconfirm,
  NProgress,
  NSpin,
  NTag,
  useMessage,
} from 'naive-ui'
import { api, type Task } from '../api'
import { openCompare, refreshStats, refreshTasks, store } from '../store'
import { fmtBytes, fmtEta } from '../utils'

const props = defineProps<{
  task: Task
  /** 排队任务在队列内可上移/下移（undefined=非排队态隐藏箭头） */
  canUp?: boolean
  canDown?: boolean
  /** 任务页批量选择模式：显示勾选框（点选只发事件，选中态由父组件管理） */
  selectMode?: boolean
  selected?: boolean
}>()
const emit = defineEmits<{
  (e: 'move', dir: -1 | 1): void
  (e: 'retryParams'): void
  (e: 'toggleSelect'): void
}>()
const message = useMessage()
const previewBroken = ref(false) // 预览 404/损坏时兜底（done 但预览生成失败不再显示破图图标）
const canCompare = computed(
  () => !!props.task.preview_src && !!props.task.preview_path,
)
// 源文件被删/移动：对比页视频模式要直接播源、静帧从源抽取，都进行不下去——
// 全页对比入口置灰禁点（预览缩略图是任务完成时落盘的，不受影响，仍可显示）
const srcGone = computed(() => props.task.input_exists === false)
const srcGoneTip = '源文件已删除或移动，无法对比'

const fileName = computed(() => {
  const base = props.task.input_path.split(/[\\/]/).pop() ?? ''
  const imgs = props.task.params?.images as { in: string }[] | undefined
  return imgs && imgs.length > 1 ? `${base} 等 ${imgs.length} 张图片` : base
})
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

function fmtElapsed(sec: number): string {
  if (!sec || sec < 0) return '--'
  return sec < 60 ? `${Math.round(sec)}秒` : fmtEta(Math.round(sec))
}

/** 完成态平均速度：图片任务按「张/秒」，视频按 fps；无数据不显示。
 * 口径=总帧数÷本轮用时（端到端：含引擎加载与最终合成，续跑任务会偏高） */
const avgSpeed = computed(() => {
  const fps = props.task.fps_avg ?? 0
  if (!fps || fps <= 0) return ''
  return props.task.params?.kind === 'image'
    ? `平均 ${fps.toFixed(2)} 张/秒`
    : `平均 ${fps.toFixed(2)} fps`
})
const avgSpeedTip = '总帧数 ÷ 本轮用时（含引擎加载与最终合成；断点续跑后的任务会偏高）'

// ---- 超分性能日志（设置 sr_profiling 开启时完成的任务可查看） ----
const logOpen = ref(false)
const logLoading = ref(false)
const logFailed = ref(false)
const logText = ref('')

async function openLog() {
  logOpen.value = true
  if (logText.value) return // 同一张卡反复打开不重复拉取
  logLoading.value = true
  logFailed.value = false
  try {
    logText.value = await api.srLog(props.task.id)
  } catch {
    logFailed.value = true
  } finally {
    logLoading.value = false
  }
}

const scaleLabel = computed(() => {
  const p = props.task.params ?? {}
  const tw = p.target_w as number | undefined
  const th = p.target_h as number | undefined
  if (tw && th) return `${props.task.src_w}x${props.task.src_h} → ${tw}x${th}`
  const t = Number(p.target_scale ?? p.scale ?? 4)
  return `${props.task.src_w}x${props.task.src_h} → ${props.task.src_w * t}x${props.task.src_h * t}`
})

const scaleBadge = computed(() => {
  const p = props.task.params ?? {}
  return p.target_w && p.target_h
    ? `${p.target_w}×${p.target_h}`
    : `x${p.target_scale ?? p.scale ?? 4}`
})

const isBusy = computed(() => props.task.status === 'running' || props.task.status === 'queued')

async function onCancel() {
  const r = await api.cancel(props.task.id)
  if (!r.ok) message.error(`取消失败: ${(await r.json()).detail ?? r.status}`)
}

async function onResume() {
  const r = await api.resume(props.task.id)
  if (!r.ok) {
    message.error(`续跑失败: ${(await r.json()).detail ?? r.status}`)
  } else {
    message.success('已加入队列，继续处理')
  }
}

async function onDelete() {
  const r = await api.remove(props.task.id)
  if (!r.ok) {
    message.error(`删除失败: ${(await r.json()).detail ?? r.status}`)
  } else {
    // 立即刷新，不等 WS 事件/轮询（健康时轮询间隔 8s，会显得删除很慢）
    refreshTasks()
    refreshStats()
  }
}

// naive Line 进度的渐变色只认 { stops: [from, to] } 对象形态；完成态保持绿色
const gradFill: { stops: [string, string] } = { stops: ['var(--sv-accent)', 'var(--sv-accent-2)'] }

// ---- 任务完成微庆祝：running → done 迁移时播一次（弹跳 + 成功色描边扩散），不循环 ----
const celebrate = ref(false)
let celebrateTimer: ReturnType<typeof setTimeout> | null = null
watch(
  () => props.task.status,
  (s, o) => {
    if (s === 'done' && o === 'running') {
      celebrate.value = true
      if (celebrateTimer) clearTimeout(celebrateTimer)
      celebrateTimer = setTimeout(() => (celebrate.value = false), 950)
    }
  },
)
onBeforeUnmount(() => {
  if (celebrateTimer) clearTimeout(celebrateTimer)
})

// fps / ETA：值变化时 150ms 淡入，消除秒级跳变的生硬感
const fpsText = computed(() => props.task.fps_run.toFixed(1))
const etaText = computed(() => fmtEta(props.task.eta_sec))

function onOpenFolder() {
  window.sv.showInFolder(props.task.output_path)
}

// 失败/取消任务：定位输入文件（此时可能还没有产出）
function onOpenInputFolder() {
  window.sv.showInFolder(props.task.input_path)
}
</script>

<template>
  <n-card
    size="small"
    :bordered="true"
    class="task-card sv-card"
    :class="['st-' + task.status, { celebrate: celebrate }]"
  >
    <!-- 运行中：右上角一颗脉动圆点（与侧栏连接点同款 halo） -->
    <span v-if="task.status === 'running'" class="run-dot" aria-hidden="true" />
    <!-- 排队任务：拖拽手柄（六点位），hover 才显示，暗示可拖拽排序 -->
    <span
      v-if="task.status === 'queued' && !selectMode"
      class="drag-grip"
      title="拖拽调整顺序"
      aria-hidden="true"
    >
      <svg width="10" height="18" viewBox="0 0 10 18">
        <circle cx="2.5" cy="3" r="1.5" fill="currentColor" /><circle cx="7.5" cy="3" r="1.5" fill="currentColor" />
        <circle cx="2.5" cy="9" r="1.5" fill="currentColor" /><circle cx="7.5" cy="9" r="1.5" fill="currentColor" />
        <circle cx="2.5" cy="15" r="1.5" fill="currentColor" /><circle cx="7.5" cy="15" r="1.5" fill="currentColor" />
      </svg>
    </span>
    <div class="row1">
      <NCheckbox
        v-if="selectMode"
        :checked="selected"
        class="sel-box"
        title="选择此任务"
        @click.stop
        @update:checked="emit('toggleSelect')"
      />
      <div class="names">
        <span class="file">{{ fileName }}</span>
        <span class="arrow">→</span>
        <span class="out">{{ outName }}</span>
      </div>
      <div class="badges">
        <n-tag size="small" :bordered="false">{{ modelName }}</n-tag>
        <n-tag size="small" :bordered="false" type="info">
          {{ scaleBadge }}
        </n-tag>
        <n-tag v-if="task.params?.interp === 'rife2x'" size="small" :bordered="false" type="success">
          补帧2×
        </n-tag>
        <n-tag
          v-if="task.params?.out_kind === 'png' || task.params?.out_kind === 'jpg'"
          size="small"
          :bordered="false"
          type="warning"
        >
          {{ String(task.params.out_kind).toUpperCase() }} 序列
        </n-tag>
        <n-tag
          size="small"
          :bordered="false"
          :type="statusMeta[task.status].type"
          class="st-tag"
          :class="{ bump: celebrate }"
        >
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
        :color="task.status === 'done' ? undefined : gradFill"
      />
      <div class="stats">
        <span>{{ scaleLabel }}</span>
        <span v-if="task.status === 'running'">
          {{ task.progress_frames }}/{{ task.total_frames }} 帧 ·
          <Transition name="num" mode="out-in"><span :key="fpsText" class="sv-num">{{ fpsText }}</span></Transition> fps ·
          剩余 <Transition name="num" mode="out-in"><span :key="etaText" class="sv-num">{{ etaText }}</span></Transition>
        </span>
        <span v-else :title="avgSpeed ? avgSpeedTip : undefined">
          {{ fmtBytes(task.out_bytes) }} · 用时 {{ fmtElapsed(task.elapsed_s)
          }}<template v-if="avgSpeed"> · {{ avgSpeed }}</template>
        </span>
      </div>
    </div>

    <n-collapse v-if="task.error && task.status === 'failed'" class="err">
      <n-collapse-item title="错误信息" name="err">
        <div class="err-text">{{ task.error }}</div>
      </n-collapse-item>
    </n-collapse>

    <div class="row3">
      <img
        v-if="(task.preview_path || task.status === 'done') && !previewBroken"
        :src="api.previewUrl(task.id, task.updated_at)"
        class="preview"
        :class="{ gone: srcGone }"
        :title="srcGone ? srcGoneTip : undefined"
        @click="canCompare && !srcGone && openCompare(task.id)"
        @error="previewBroken = true"
      />
      <div v-else-if="task.status === 'done'" class="preview-broken">无预览</div>
      <div class="spacer" />
      <NButton
        v-if="canCompare"
        size="small"
        quaternary
        type="info"
        :disabled="srcGone"
        :title="srcGone ? srcGoneTip : undefined"
        @click="openCompare(task.id)"
      >
        全页对比
      </NButton>
      <NButton
        v-if="task.status === 'done' && task.has_sr_log"
        size="small"
        quaternary
        @click="openLog"
      >
        性能日志
      </NButton>
      <NButton v-if="isBusy" size="small" quaternary type="error" @click="onCancel">取消</NButton>
      <NButton
        v-if="task.status === 'failed' || task.status === 'canceled'"
        size="small"
        quaternary
        type="info"
        @click="onResume"
      >
        继续
      </NButton>
      <NButton
        v-if="task.status === 'failed' || task.status === 'canceled'"
        size="small"
        quaternary
        type="info"
        @click="emit('retryParams')"
      >
        改参数重试
      </NButton>
      <NButton v-if="task.status === 'done'" size="small" quaternary type="info" @click="onOpenFolder">
        打开所在文件夹
      </NButton>
      <NButton
        v-if="task.status === 'failed' || task.status === 'canceled'"
        size="small"
        quaternary
        type="info"
        @click="onOpenInputFolder"
      >
        输入所在文件夹
      </NButton>
      <NPopconfirm v-if="!isBusy" @positive-click="onDelete">
        <template #trigger>
          <NButton size="small" quaternary>删除</NButton>
        </template>
        删除这条任务记录{{ task.status === 'done' ? '（已生成的输出文件不受影响）' : '' }}？
      </NPopconfirm>
      <!-- 排队顺序微调：悬停显示（拖拽的补充，触控板不好精准拖） -->
      <span v-if="task.status === 'queued'" class="qbtns">
        <button class="qbtn" title="上移" :disabled="!canUp" @click="emit('move', -1)">↑</button>
        <button class="qbtn" title="下移" :disabled="!canDown" @click="emit('move', 1)">↓</button>
      </span>
    </div>

    <NModal v-model:show="logOpen" preset="card" title="超分性能日志" style="max-width: 760px">
      <NSpin :show="logLoading">
        <pre v-if="logText" class="srlog-pre">{{ logText }}</pre>
        <div v-else-if="logFailed" class="srlog-msg">日志读取失败（文件可能已被清理）</div>
        <div v-else class="srlog-msg">加载中…</div>
      </NSpin>
    </NModal>
  </n-card>
</template>

<style scoped>
/* 卡片即画布：状态脊线 + 悬浮微抬；NCard 圆角经主题已是 14px（sv-card 提供底/描边/高光） */
.task-card {
  position: relative;
  transition: border-color 0.18s var(--sv-ease), box-shadow 0.18s var(--sv-ease), transform 0.18s var(--sv-ease);
  overflow: visible;
}
/* 状态脊线：一眼分清队列里的任务处于什么状态 */
.task-card::before {
  content: '';
  position: absolute;
  left: 0;
  top: 14px;
  bottom: 14px;
  width: 3px;
  border-radius: 0 3px 3px 0;
  background: var(--sv-text-faint); /* 排队态的中性脊线（浅色下也可见） */
  opacity: 0.55;
  transition: background 0.2s, box-shadow 0.2s;
}
/* 运行中：品牌渐变缓慢下流（background-size 300% 三段循环，无缝） */
.task-card.st-running::before {
  background: linear-gradient(180deg, var(--sv-accent), var(--sv-accent-2), var(--sv-accent));
  background-size: 100% 300%;
  box-shadow: 0 0 10px rgba(var(--sv-accent-rgb), 0.65);
}
@media (prefers-reduced-motion: no-preference) {
  .task-card.st-running::before { animation: ridge-flow 3s linear infinite; }
}
@keyframes ridge-flow {
  0% { background-position: 0% 100%; }
  100% { background-position: 0% 0%; }
}
.task-card.st-done::before { background: var(--sv-success); opacity: 0.75; }
.task-card.st-failed::before { background: var(--sv-danger); box-shadow: 0 0 8px rgba(var(--sv-danger-rgb), 0.4); }
.task-card.st-canceled::before { background: var(--sv-warning); opacity: 0.7; }
.task-card:hover {
  border-color: var(--sv-border-strong);
  transform: translateY(-1px);
  box-shadow: var(--sv-card-inset), 0 8px 22px rgba(0, 0, 0, 0.32);
}

/* 任务完成微庆祝：一次性成功色描边扩散（不循环、不撒花、不发声） */
.task-card.celebrate { animation: card-celebrate 0.9s var(--sv-ease) 1; }
@keyframes card-celebrate {
  0% { box-shadow: var(--sv-card-inset), 0 0 0 0 rgba(var(--sv-success-rgb), 0); }
  30% { box-shadow: var(--sv-card-inset), 0 0 0 3px rgba(var(--sv-success-rgb), 0.4), 0 10px 30px rgba(0, 0, 0, 0.3); }
  100% { box-shadow: var(--sv-card-inset), 0 0 0 10px rgba(var(--sv-success-rgb), 0); }
}
.st-tag.bump { animation: tag-bump 0.4s var(--sv-ease) 1; }
@keyframes tag-bump {
  0%, 100% { transform: scale(1); }
  50% { transform: scale(1.15); }
}

/* 运行中右上角脉动圆点：8px + halo 扩散（与侧栏连接点同款） */
.run-dot {
  position: absolute;
  top: 15px;
  right: 15px;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--sv-accent);
  box-shadow: 0 0 8px rgba(var(--sv-accent-rgb), 0.9);
}
.run-dot::after {
  content: '';
  position: absolute;
  inset: -3px;
  border-radius: 50%;
  border: 1px solid rgba(var(--sv-accent-rgb), 0.55);
  animation: dot-halo 2.2s ease-out infinite;
}
@keyframes dot-halo {
  0% { transform: scale(0.6); opacity: 0.9; }
  70%, 100% { transform: scale(1.8); opacity: 0; }
}

/* 排队任务拖拽手柄：卡片左缘外挂，hover 才现身 */
.drag-grip {
  position: absolute;
  left: -26px;
  top: 50%;
  transform: translateY(-50%);
  width: 20px;
  height: 24px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--sv-text-faint);
  cursor: grab;
  opacity: 0;
  transition: opacity var(--sv-dur-fast) ease, color var(--sv-dur-fast) ease;
}
.drag-grip:hover { color: var(--sv-accent-strong); }
.task-card:hover .drag-grip { opacity: 1; }

.row1 { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.sel-box { margin-left: 2px; flex-shrink: 0; }
.names { flex: 1; }
.names { display: flex; align-items: center; gap: 8px; min-width: 0; }
.file { font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.arrow, .out { color: var(--sv-text-dim); font-size: 13px; }
.out { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.badges { display: flex; gap: 6px; flex-shrink: 0; }
.progress-wrap { margin-top: 10px; }
/* 运行中的进度条：渐变填充上叠一道流光 */
.st-running :deep(.n-progress-graph-line-fill) { position: relative; overflow: hidden; }
.st-running :deep(.n-progress-graph-line-fill)::after {
  content: '';
  position: absolute;
  inset: 0;
  transform: translateX(-100%);
  background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.35), transparent);
  animation: fill-sheen 1.6s ease-in-out infinite;
}
@keyframes fill-sheen { 100% { transform: translateX(100%); } }
.stats {
  display: flex; justify-content: space-between; margin-top: 6px;
  font-size: 12px; color: var(--sv-text-dim); font-variant-numeric: tabular-nums;
}
/* fps/ETA 值变化的 150ms 淡入 */
.num-enter-active { transition: opacity 0.15s ease-out; }
.num-leave-active { transition: opacity 0.05s ease-in; }
.num-enter-from, .num-leave-to { opacity: 0; }
.err { margin-top: 8px; }
.err-text { color: var(--sv-danger); font-size: 12px; word-break: break-all; white-space: pre-wrap; }
.row3 { display: flex; align-items: flex-end; gap: 10px; margin-top: 10px; }
.preview-broken {
  width: 88px;
  height: 50px;
  border-radius: var(--sv-radius-sm);
  background: var(--sv-fill-1);
  border: 1px dashed var(--sv-border-mid);
  color: var(--sv-text-faint);
  font-size: 11px;
  display: flex;
  align-items: center;
  justify-content: center;
}
.preview {
  max-height: 96px; max-width: 45%; border-radius: var(--sv-radius-sm); border: 1px solid var(--sv-border-mid);
  object-fit: contain; cursor: pointer;
  transition: transform 0.18s var(--sv-ease), box-shadow 0.18s var(--sv-ease), border-color 0.18s var(--sv-ease);
}
/* 悬停放大预览：像捏住一角掀开看细节 */
.preview:hover:not(.gone) {
  transform: scale(1.6);
  transform-origin: left bottom;
  border-color: rgba(var(--sv-accent-rgb), 0.55);
  box-shadow: 0 12px 34px rgba(0, 0, 0, 0.55);
  z-index: 6;
  position: relative;
}
/* 源文件已删：预览不再充当对比入口（置灰+默认光标），缩略图本身仍保留 */
.preview.gone { cursor: default; opacity: 0.45; }
.spacer { flex: 1; }
.qbtns {
  display: inline-flex;
  gap: 4px;
  opacity: 0;
  transition: opacity var(--sv-dur-fast) ease;
}
.task-card:hover .qbtns { opacity: 1; }
.qbtn {
  width: 22px;
  height: 22px;
  border: 1px solid var(--sv-border);
  border-radius: 6px;
  background: var(--sv-panel-2);
  color: var(--sv-text-dim);
  font-size: 12px;
  line-height: 1;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  transition: border-color var(--sv-dur-fast) ease, color var(--sv-dur-fast) ease, background var(--sv-dur-fast) ease;
}
.qbtn:hover:not(:disabled) { border-color: var(--sv-accent); color: var(--sv-accent-strong); background: var(--sv-accent-bg); }
.qbtn:disabled { opacity: 0.35; cursor: default; }
.srlog-pre {
  margin: 0;
  min-height: 120px;
  max-height: 62vh;
  overflow-y: auto;
  font-family: Consolas, 'Courier New', monospace;
  font-size: 12px;
  line-height: 1.65;
  color: var(--sv-text-code);
  white-space: pre-wrap;
  word-break: break-all;
}
.srlog-msg { min-height: 120px; display: flex; align-items: center; justify-content: center; color: var(--sv-text-dim); font-size: 12.5px; }
</style>
