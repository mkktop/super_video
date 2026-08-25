<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { NTag } from 'naive-ui'
import { api } from '../api'
import { store } from '../store'
import TrendChart from '../components/TrendChart.vue'
import type { ChartSeries } from '../components/TrendChart.vue'
import PerfRings from '../components/PerfRings.vue'

const rangeMin = ref(15)
const ranges = [5, 15, 60]
const samplingOn = ref(true)

const latest = computed(() => store.perf.latest)
const gpu0 = computed(() => latest.value?.gpus?.[0] ?? null)
const vramTotalGb = computed(() => (gpu0.value?.mem_total_mb ?? 0) / 1024)
const vramUsedGb = computed(() => (gpu0.value?.mem_used_mb ?? 0) / 1024)

onMounted(async () => {
  try {
    const s = (await api.settings()) as { perf_sampling?: boolean }
    samplingOn.value = s.perf_sampling !== false
  } catch {
    /* 读不到按默认开 */
  }
})
// 设置页重新开启后首个采样点到达即恢复提示状态
watch(latest, () => {
  if (!samplingOn.value) samplingOn.value = true
})

// ---- 仪表环（PerfRings 共享组件，首页同款） ----

// ---- 运行任务 ----
const running = computed(() => store.tasks.find((t) => t.status === 'running'))
const runPercent = computed(() => {
  const r = running.value
  if (!r || !r.total_frames) return 0
  return Math.min(100, Math.round((r.progress_frames / r.total_frames) * 100))
})

function fmtEta(sec: number): string {
  if (!sec || sec < 0) return '—'
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  const s = Math.floor(sec % 60)
  return h ? `${h}时${m}分` : m ? `${m}分${s}秒` : `${s}秒`
}

// ---- 趋势系列 ----
const pctSeries: ChartSeries[] = [
  { key: 'cpu', label: 'CPU', color: '#4f8cff', get: (s) => s.cpu },
  { key: 'gpu', label: 'GPU', color: '#34d399', get: (s) => s.gpus?.[0]?.util ?? null },
  { key: 'mem', label: '内存', color: '#f59e0b', get: (s) => s.mem_pct },
  {
    key: 'taskcpu',
    label: '任务进程 CPU',
    color: '#8b5cf6',
    dashed: true,
    get: (s) => s.task?.cpu_pct ?? null,
  },
]

const vramSeries: ChartSeries[] = [
  {
    key: 'vram',
    label: '显存',
    color: '#8b5cf6',
    fill: true,
    get: (s) => (s.gpus?.[0] ? s.gpus[0].mem_used_mb / 1024 : null),
  },
  {
    key: 'taskmem',
    label: '任务进程内存',
    color: '#fbbf24',
    dashed: true,
    get: (s) => s.task?.mem_gb ?? null,
  },
]

const vramMax = computed(() => Math.max(4, Math.ceil((vramTotalGb.value || 4) * 2) / 2))
const gpuEver = computed(
  () => store.perf.samples.length > 0 && store.perf.samples.some((s) => s.gpus?.length),
)
</script>

<template>
  <div class="perf-page">
    <div class="page-head">
      <h1>性能监控</h1>
      <div class="range-btns">
        <button
          v-for="r in ranges"
          :key="r"
          :class="{ on: rangeMin === r }"
          @click="rangeMin = r"
        >
          最近 {{ r }} 分钟
        </button>
      </div>
    </div>

    <div v-if="!samplingOn" class="card off-tip">
      后台性能采样已关闭,仪表与趋势不再更新 —— 可在「设置 · 性能监控」中重新开启。
    </div>

    <!-- 仪表环 -->
    <PerfRings />

    <!-- 运行任务 -->
    <section v-if="running" class="card task-card">
      <div class="tc-info">
        <span class="tc-label">正在运行</span>
        <span class="tc-file">{{ running.input_path.split(/[\\/]/).pop() }}</span>
        <NTag size="small" type="info" :bordered="false">{{ running.model_id }}</NTag>
      </div>
      <div class="tc-progress">
        {{ runPercent }}% · {{ running.progress_frames }}/{{ running.total_frames }} 帧<template
          v-if="running.fps_run"
        >
          · {{ running.fps_run.toFixed(1) }} fps · 剩余 {{ fmtEta(running.eta_sec) }}
        </template>
      </div>
      <div v-if="latest?.task" class="tc-proc">
        <div><b>{{ latest.task.cpu_pct }}%</b><span>进程 CPU</span></div>
        <div><b>{{ latest.task.mem_gb }} GB</b><span>进程内存</span></div>
        <div><b>{{ latest.task.n_proc }}</b><span>进程数</span></div>
      </div>
      <div v-else class="tc-proc tc-proc-wait">任务进程占用采集中…</div>
    </section>

    <!-- 占用率趋势 -->
    <section class="card chart-card">
      <div class="chart-head">
        <h2>占用率趋势</h2>
        <span class="chart-note">CPU / GPU / 内存为整机百分比,虚线是超分任务进程树(worker+ffmpeg)</span>
      </div>
      <TrendChart :samples="store.perf.samples" :range-min="rangeMin" :series="pctSeries" />
    </section>

    <!-- 显存趋势 -->
    <section v-if="gpuEver" class="card chart-card">
      <div class="chart-head">
        <h2>显存与任务内存</h2>
        <span class="chart-note">显存来自 nvidia-smi,任务进程内存为 worker 进程树 RSS</span>
      </div>
      <TrendChart
        :samples="store.perf.samples"
        :range-min="rangeMin"
        :series="vramSeries"
        :max="vramMax"
        unit=" GB"
      />
    </section>
  </div>
</template>

<style scoped>
.perf-page { display: flex; flex-direction: column; gap: 16px; }

.page-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
h1 { font-size: 20px; font-weight: 700; }
h2 { font-size: 15px; font-weight: 600; color: #c6cad0; }

.range-btns { display: flex; gap: 6px; }
.range-btns button {
  border: 1px solid #2a2d31;
  background: #1e2023;
  color: #9aa0a6;
  font-size: 12.5px;
  padding: 5px 12px;
  border-radius: 7px;
  cursor: pointer;
  transition: all 0.15s;
}
.range-btns button:hover { color: #e8eaed; }
.range-btns button.on {
  background: rgba(79, 140, 255, 0.14);
  border-color: rgba(79, 140, 255, 0.5);
  color: #4f8cff;
}

.card {
  background: #1e2023;
  border: 1px solid #26292e;
  border-radius: 12px;
}

.off-tip {
  padding: 12px 16px;
  font-size: 13px;
  color: #fbbf24;
  border-color: rgba(251, 191, 36, 0.3);
  background: rgba(251, 191, 36, 0.06);
}

.task-card {
  padding: 14px 18px;
  display: flex;
  align-items: center;
  gap: 20px;
  flex-wrap: wrap;
  border-color: rgba(79, 140, 255, 0.35);
}
.tc-info { display: flex; align-items: center; gap: 10px; min-width: 0; }
.tc-label { color: #4f8cff; font-size: 13px; flex-shrink: 0; }
.tc-file {
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 260px;
}
.tc-progress { font-size: 12.5px; color: #9aa0a6; font-variant-numeric: tabular-nums; }
.tc-proc { display: flex; gap: 22px; margin-left: auto; }
.tc-proc > div { text-align: center; }
.tc-proc b { font-size: 16px; font-weight: 700; display: block; font-variant-numeric: tabular-nums; }
.tc-proc span { font-size: 11px; color: #6b7280; }
.tc-proc-wait { font-size: 12px; color: #6b7280; }

.chart-card { padding: 16px 18px 12px; }
.chart-head { display: flex; align-items: baseline; gap: 12px; margin-bottom: 10px; }
.chart-note { font-size: 11.5px; color: #6b7280; }
</style>
