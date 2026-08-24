<script setup lang="ts">
import { computed } from 'vue'
import { NButton, NProgress, NTag } from 'naive-ui'
import { store, ui } from '../store'

const running = computed(() => store.tasks.find((t) => t.status === 'running'))
// 统计走 /api/stats 全量聚合：任务列表有历史上限，直接数列表会漏旧任务
const stats = computed(() => store.stats)

function fmtBytes(b: number): string {
  return b > 1e9 ? `${(b / 1e6 / 1024).toFixed(2)} GB` : b > 1e6 ? `${(b / 1e6).toFixed(1)} MB` : `${b} B`
}

function fmtFrames(n: number): string {
  return n > 10000 ? `${(n / 10000).toFixed(1)} 万` : String(n)
}

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

const hw = computed(() => store.hardware)
</script>

<template>
  <div class="home">
    <!-- Hero -->
    <section class="hero">
      <div class="glow glow-a" />
      <div class="glow glow-b" />
      <div class="hero-text">
        <h1>视频超分<span class="grad">工作台</span></h1>
        <p>低分辨率视频 · AI 重建 · 高清输出　让老片重获新生</p>
        <div class="hero-actions">
          <NButton type="primary" size="large" @click="ui.showNewTask = true">＋ 新建超分任务</NButton>
          <NButton size="large" quaternary @click="ui.page = 'tasks'">查看任务队列</NButton>
        </div>
      </div>
      <div class="hero-chip">
        <span class="chip-dot" />
        推理引擎就绪 · {{ store.gpuName }}
      </div>
    </section>

    <!-- 运行状态 -->
    <section v-if="running" class="card run-card" @click="ui.page = 'tasks'">
      <div class="run-info">
        <span class="run-label">正在处理</span>
        <span class="run-file">{{ running.input_path.split(/[\\/]/).pop() }}</span>
        <NTag size="small" type="info" :bordered="false">{{ running.model_id }}</NTag>
      </div>
      <div class="run-progress">
        <NProgress type="line" :percentage="runPercent" :show-indicator="false" :height="10" processing />
        <span class="run-pct">
          {{ runPercent }}% · {{ running.progress_frames }}/{{ running.total_frames }} 帧<template v-if="running.fps_run"> · {{ running.fps_run.toFixed(1) }} 帧/秒 · 剩余 {{ fmtEta(running.eta_sec) }}</template>
        </span>
      </div>
    </section>

    <!-- 统计 -->
    <section class="stat-grid">
      <div class="card stat">
        <div class="stat-icon i-blue">▤</div>
        <div>
          <div class="stat-num">{{ stats.total }}</div>
          <div class="stat-label">累计任务</div>
        </div>
      </div>
      <div class="card stat">
        <div class="stat-icon i-green">✓</div>
        <div>
          <div class="stat-num">{{ stats.done }}</div>
          <div class="stat-label">已完成</div>
        </div>
      </div>
      <div class="card stat">
        <div class="stat-icon i-purple">▦</div>
        <div>
          <div class="stat-num">{{ fmtFrames(stats.frames) }}</div>
          <div class="stat-label">累计处理帧</div>
        </div>
      </div>
      <div class="card stat">
        <div class="stat-icon i-amber">⬒</div>
        <div>
          <div class="stat-num">{{ fmtBytes(stats.bytes) }}</div>
          <div class="stat-label">累计产出</div>
        </div>
      </div>
    </section>

    <!-- 硬件 -->
    <section>
      <h2 class="sec-title">本机硬件</h2>
      <div class="hw-grid">
        <div class="card hw hw-gpu">
          <div class="hw-head">
            <span class="hw-icon">⚡</span>
            <span class="hw-name">{{ hw?.gpus?.[0]?.name ?? '—' }}</span>
          </div>
          <div class="hw-tags">
            <NTag type="success" size="small" :bordered="false">AI 推理就绪</NTag>
            <NTag v-if="hw?.gpus?.[0]?.vram_gb" type="info" size="small" :bordered="false">
              显存 {{ hw.gpus[0].vram_gb }} GB
            </NTag>
            <NTag size="small" :bordered="false">
              {{ store.engine?.backend === 'cuda' ? 'CUDA 加速' : 'DirectML' }}
            </NTag>
          </div>
        </div>
        <div class="card hw">
          <div class="hw-head">
            <span class="hw-icon">◫</span>
            <span class="hw-name">处理器</span>
          </div>
          <div class="hw-detail">{{ hw?.cpu || '—' }}</div>
          <div class="hw-sub">{{ hw?.cpu_cores ?? '—' }} 核心</div>
        </div>
        <div class="card hw">
          <div class="hw-head">
            <span class="hw-icon">▩</span>
            <span class="hw-name">内存</span>
          </div>
          <div class="hw-detail">{{ hw?.ram_gb ?? '—' }} GB</div>
          <div class="hw-sub">系统内存</div>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.home { display: flex; flex-direction: column; gap: 18px; }

.hero {
  position: relative;
  border-radius: 14px;
  padding: 34px 30px 30px;
  background: linear-gradient(135deg, #1b2130 0%, #191b1f 60%);
  border: 1px solid #262b36;
  overflow: hidden;
}
.glow { position: absolute; border-radius: 50%; filter: blur(70px); opacity: 0.5; }
.glow-a { width: 320px; height: 320px; background: #2b4c8f; top: -160px; right: -60px; }
.glow-b { width: 240px; height: 240px; background: #4c2b8f; bottom: -140px; right: 180px; opacity: 0.35; }
.hero-text { position: relative; }
h1 {
  font-size: 30px;
  font-weight: 700;
  letter-spacing: 1px;
  color: #e8eaed;
}
.grad {
  background: linear-gradient(90deg, #4f8cff, #8b5cf6);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  margin-left: 8px;
}
.hero-text p { margin: 10px 0 20px; color: #9aa0a6; font-size: 14px; letter-spacing: 0.5px; }
.hero-actions { display: flex; gap: 12px; }
.hero-chip {
  position: absolute;
  right: 24px;
  top: 24px;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 14px;
  border-radius: 20px;
  background: rgba(52, 211, 153, 0.09);
  border: 1px solid rgba(52, 211, 153, 0.25);
  color: #34d399;
  font-size: 12.5px;
}
.chip-dot {
  width: 7px; height: 7px; border-radius: 50%;
  background: #34d399;
  box-shadow: 0 0 8px rgba(52, 211, 153, 0.9);
}

.card {
  background: #1e2023;
  border: 1px solid #26292e;
  border-radius: 12px;
}

.run-card {
  padding: 16px 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  cursor: pointer;
  border-color: rgba(79, 140, 255, 0.35);
  transition: border-color 0.15s;
}
.run-card:hover { border-color: rgba(79, 140, 255, 0.7); }
.run-info { display: flex; align-items: center; gap: 12px; min-width: 0; }
.run-label { color: #4f8cff; font-size: 13px; flex-shrink: 0; }
.run-file { font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.run-progress { min-width: 320px; display: flex; align-items: center; gap: 12px; flex: 1; }
.run-progress > div:first-child { flex: 1; }
.run-pct { font-size: 12.5px; color: #9aa0a6; font-variant-numeric: tabular-nums; white-space: nowrap; }

.stat-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; }
.stat { display: flex; align-items: center; gap: 14px; padding: 18px; }
.stat-icon {
  width: 42px; height: 42px; border-radius: 10px;
  display: flex; align-items: center; justify-content: center; font-size: 18px;
}
.i-blue { background: rgba(79, 140, 255, 0.14); color: #4f8cff; }
.i-green { background: rgba(52, 211, 153, 0.12); color: #34d399; }
.i-purple { background: rgba(139, 92, 246, 0.14); color: #a78bfa; }
.i-amber { background: rgba(251, 191, 36, 0.12); color: #fbbf24; }
.stat-num { font-size: 22px; font-weight: 700; font-variant-numeric: tabular-nums; }
.stat-label { font-size: 12px; color: #9aa0a6; margin-top: 2px; }

.sec-title { font-size: 15px; font-weight: 600; color: #c6cad0; margin-bottom: 12px; }
.hw-grid { display: grid; grid-template-columns: 1.6fr 1.2fr 0.7fr; gap: 14px; }
.hw { padding: 18px 20px; display: flex; flex-direction: column; gap: 10px; }
.hw-gpu {
  background: linear-gradient(135deg, #1e2531, #1e2023);
  border-color: #2c3442;
}
.hw-head { display: flex; align-items: center; gap: 10px; }
.hw-icon { font-size: 18px; }
.hw-name { font-weight: 600; font-size: 14.5px; }
.hw-tags { display: flex; gap: 8px; }
.hw-detail { font-size: 13px; color: #c6cad0; word-break: break-all; }
.hw-sub { font-size: 12px; color: #6b7280; }
</style>
