<script setup lang="ts">
import { computed } from 'vue'
import { NButton, NProgress, NTag } from 'naive-ui'
import { store, ui } from '../store'
import { fmtBytes, fmtEta } from '../utils'
import PerfRings from '../components/PerfRings.vue'

const running = computed(() => store.tasks.find((t) => t.status === 'running'))
// 统计走 /api/stats 全量聚合：任务列表有历史上限，直接数列表会漏旧任务
const stats = computed(() => store.stats)

function fmtFrames(n: number): string {
  return n > 10000 ? `${(n / 10000).toFixed(1)} 万` : String(n)
}

const runPercent = computed(() => {
  const r = running.value
  if (!r || !r.total_frames) return 0
  return Math.min(100, Math.round((r.progress_frames / r.total_frames) * 100))
})

const hw = computed(() => store.hardware)

// naive Line 进度的渐变色只认 { stops: [from, to] } 对象形态（数组形态会崩）
const gradFill: { stops: [string, string] } = { stops: ['#4f8cff', '#8b5cf6'] }

// ---- 显卡主卡：实时显存占用（perf 2s 一拍）+ 推理后端徽标 ----
const gpuLive = computed(() => store.perf.latest?.gpus?.[0] ?? null)
const vramTotalGb = computed(() => {
  const live = gpuLive.value?.mem_total_mb
  if (live) return live / 1024
  return hw.value?.gpus?.[0]?.vram_gb ?? null
})
const vramUsedGb = computed(() => {
  const used = gpuLive.value?.mem_used_mb
  return used ? used / 1024 : null
})
const vramPct = computed(() => {
  const total = vramTotalGb.value
  const used = vramUsedGb.value
  if (!total || used == null) return 0
  return Math.min(100, Math.round((used / total) * 100))
})
const backendLabel = computed(() => {
  if (!store.engine) return '未就绪'
  return store.engine.backend === 'trt'
    ? 'TensorRT'
    : store.engine.backend === 'cuda'
      ? 'CUDA'
      : 'DirectML'
})

// ---- 处理器/内存卡：实时占用（perf 2s 一拍；null=尚无采样，回落静态展示） ----
const cpuPct = computed(() => {
  const l = store.perf.latest
  return l ? Math.round(l.cpu) : null
})
const procCpuPct = computed(() => {
  const p = store.perf.latest?.task?.cpu_pct
  return p != null ? Math.round(p) : null
})
const ramUsedGb = computed(() => store.perf.latest?.mem_used_gb ?? null)

// 全新用户（还没跑过任何任务）：四宫格全 0 没有意义，换成三步上手引导
const fresh = computed(() => store.stats.total === 0)
</script>

<template>
  <div class="home">
    <!-- Hero -->
    <section class="hero">
      <div class="hero-text">
        <h1>视频超分<span class="grad">工作台</span></h1>
        <p>低分辨率视频 · AI 重建 · 高清输出　让老片重获新生</p>
        <div class="hero-actions">
          <NButton type="primary" size="large" @click="ui.page = 'newtask'">＋ 新建超分任务</NButton>
          <NButton size="large" quaternary @click="ui.page = 'tasks'">查看任务队列</NButton>
        </div>
      </div>
      <!-- 低清 → 高清 的像素渐清晰示意（纯 CSS 装饰） -->
      <div class="px-demo" aria-hidden="true">
        <div class="px-screen">
          <div class="px-sharp" />
          <div class="px-mosaic" />
          <div class="px-line" />
          <span class="px-tag sd">480p</span>
          <span class="px-tag hd">4K</span>
        </div>
      </div>
      <div class="hero-chip">
        <span class="chip-dot" :class="{ off: !store.engine }" />
        {{ store.engine ? `推理引擎就绪 · ${store.engine.backend}` : '推理引擎未就绪' }} · {{ store.gpuName }}
      </div>
    </section>

    <!-- 运行状态 -->
    <section v-if="running" class="card run-card" @click="ui.page = 'tasks'">
      <div class="run-info">
        <span class="run-pulse" />
        <span class="run-label">正在处理</span>
        <span class="run-file">{{ running.input_path.split(/[\\/]/).pop() }}</span>
        <NTag size="small" type="info" :bordered="false">{{ running.model_id }}</NTag>
      </div>
      <div class="run-progress">
        <NProgress
          type="line"
          :percentage="runPercent"
          :show-indicator="false"
          :height="10"
          :color="gradFill"
          processing
        />
        <span class="run-pct">
          {{ runPercent }}% · {{ running.progress_frames }}/{{ running.total_frames }} 帧<template v-if="running.fps_run"> · {{ running.fps_run.toFixed(1) }} 帧/秒 · 剩余 {{ fmtEta(running.eta_sec) }}</template>
        </span>
      </div>
    </section>

    <!-- 三步上手（仅无任何历史任务时展示） -->
    <section v-if="fresh && !running" class="card guide">
      <div class="guide-title">三步完成第一次超分</div>
      <div class="guide-steps">
        <div class="g-step">
          <span class="g-num">1</span>
          <div><b>选视频</b>新建超分任务，把要处理的视频拖进窗口或点击选择</div>
        </div>
        <div class="g-step">
          <span class="g-num">2</span>
          <div><b>挑模型</b>不确定哪个合适？用「模型对比」拿同一段素材并排试</div>
        </div>
        <div class="g-step">
          <span class="g-num">3</span>
          <div><b>入队等待</b>处理期间可以最小化窗口，完成时会有系统通知</div>
        </div>
      </div>
      <div class="guide-actions">
        <NButton type="primary" @click="ui.page = 'newtask'">＋ 新建超分任务</NButton>
        <NButton quaternary @click="ui.page = 'mcompare'">先对比模型</NButton>
      </div>
    </section>

    <!-- 统计 -->
    <section v-if="!fresh" class="stat-grid">
      <div class="card stat">
        <div class="stat-icon i-blue">
          <svg width="20" height="20" viewBox="0 0 20 20"><rect x="3" y="2.5" width="14" height="15" rx="2.4" fill="none" stroke="currentColor" stroke-width="1.5" /><path d="M7 6.5h6M7 10h6M7 13.5h3.6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" /></svg>
        </div>
        <div>
          <div class="stat-num">{{ stats.total }}</div>
          <div class="stat-label">累计任务</div>
        </div>
      </div>
      <div class="card stat">
        <div class="stat-icon i-green">
          <svg width="20" height="20" viewBox="0 0 20 20"><path d="M4 10.5l4 4 8-9" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" /></svg>
        </div>
        <div>
          <div class="stat-num">{{ stats.done }}</div>
          <div class="stat-label">已完成</div>
        </div>
      </div>
      <div class="card stat">
        <div class="stat-icon i-purple">
          <svg width="20" height="20" viewBox="0 0 20 20"><rect x="2.5" y="4.5" width="15" height="11" rx="2" fill="none" stroke="currentColor" stroke-width="1.5" /><path d="M2.5 8h15M7 4.5v11M13 4.5v11" stroke="currentColor" stroke-width="1.2" /></svg>
        </div>
        <div>
          <div class="stat-num">{{ fmtFrames(stats.frames) }}</div>
          <div class="stat-label">累计处理帧</div>
        </div>
      </div>
      <div class="card stat">
        <div class="stat-icon i-amber">
          <svg width="20" height="20" viewBox="0 0 20 20"><ellipse cx="10" cy="5.2" rx="6.5" ry="2.7" fill="none" stroke="currentColor" stroke-width="1.5" /><path d="M3.5 5.2v9.6c0 1.5 2.9 2.7 6.5 2.7s6.5-1.2 6.5-2.7V5.2" fill="none" stroke="currentColor" stroke-width="1.5" /><path d="M3.5 10c0 1.5 2.9 2.7 6.5 2.7s6.5-1.2 6.5-2.7" fill="none" stroke="currentColor" stroke-width="1.5" /></svg>
        </div>
        <div>
          <div class="stat-num">{{ fmtBytes(stats.bytes) }}</div>
          <div class="stat-label">累计产出</div>
        </div>
      </div>
    </section>

    <!-- 硬件 -->
    <section>
      <h2 class="sec-title">硬件信息</h2>
      <div class="hw-grid">
      <div class="card hw hw-gpu">
        <div class="gpu-circuit" aria-hidden="true" />
        <div class="gpu-head">
          <span class="gpu-icon">
            <svg width="22" height="22" viewBox="0 0 22 22"><rect x="2" y="5.5" width="17" height="11" rx="2.4" fill="none" stroke="currentColor" stroke-width="1.5" /><circle cx="7.4" cy="11" r="2.1" fill="none" stroke="currentColor" stroke-width="1.3" /><path d="M12.5 8.8l3.4 2.2-3.4 2.2z" fill="currentColor" /><path d="M4.8 16.5v2M16.8 16.5v2" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" /><path d="M6 2.6h10M6 1.2v2.8M16 1.2v2.8" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" /></svg>
          </span>
          <div class="gpu-title">
            <span class="gpu-kind">GRAPHICS · 图形处理器</span>
            <span class="gpu-name">{{ hw?.gpus?.[0]?.name ?? '—' }}</span>
          </div>
          <span class="gpu-backend" :class="{ off: !store.engine }">
            <span class="gb-dot" />{{ backendLabel }}
          </span>
        </div>
        <div v-if="vramUsedGb != null" class="gpu-vram">
          <div class="vram-bar">
            <div class="vram-fill" :style="{ width: vramPct + '%' }" />
          </div>
          <span class="vram-text">
            显存 {{ vramUsedGb.toFixed(1) }} / {{ vramTotalGb?.toFixed(1) }} GB
            <b v-if="gpuLive?.util != null"> · GPU {{ gpuLive.util }}%</b>
          </span>
        </div>
        <div class="hw-tags">
          <NTag :type="store.engine ? 'success' : 'warning'" size="small" :bordered="false">
            {{ store.engine ? 'AI 推理就绪' : '推理引擎未就绪' }}
          </NTag>
          <NTag v-if="vramUsedGb == null && hw?.gpus?.[0]?.vram_gb" type="info" size="small" :bordered="false">
            显存 {{ hw.gpus[0].vram_gb }} GB
          </NTag>
        </div>
      </div>
        <div class="card hw hw-cpu">
          <div class="chip-head">
            <span class="chip-icon">
              <svg width="17" height="17" viewBox="0 0 17 17"><rect x="3.5" y="3.5" width="10" height="10" rx="1.8" fill="none" stroke="currentColor" stroke-width="1.4" /><rect x="6.8" y="6.8" width="3.4" height="3.4" rx="0.7" fill="currentColor" /><path d="M6 1.5v2M11 1.5v2M6 13.5v2M11 13.5v2M1.5 6h2M1.5 11h2M13.5 6h2M13.5 11h2" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" /></svg>
            </span>
            <div class="chip-title">
              <span class="chip-kind">PROCESSOR · 处理器</span>
              <span class="chip-name" :title="hw?.cpu ?? ''">{{ hw?.cpu || '—' }}</span>
            </div>
          </div>
          <div v-if="cpuPct != null" class="chip-live">
            <div class="live-bar"><div class="live-fill fill-cpu" :style="{ width: cpuPct + '%' }" /></div>
            <span class="live-text">
              占用 <b>{{ cpuPct }}%</b><template v-if="procCpuPct != null"> · 进程 {{ procCpuPct }}%</template>
            </span>
          </div>
          <div v-else class="hw-sub">{{ hw?.cpu_cores ?? '—' }} 核心</div>
          <div v-if="cpuPct != null" class="hw-sub">{{ hw?.cpu_cores ?? '—' }} 核心</div>
        </div>
        <div class="card hw hw-mem">
          <div class="chip-head">
            <span class="chip-icon icon-amber">
              <svg width="17" height="17" viewBox="0 0 17 17"><rect x="2" y="5" width="13" height="7" rx="1.8" fill="none" stroke="currentColor" stroke-width="1.4" /><path d="M4.5 7.2v2.6M7 7.2v2.6M9.5 7.2v2.6M12 7.2v2.6" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" /></svg>
            </span>
            <div class="chip-title">
              <span class="chip-kind">MEMORY · 内存</span>
              <span class="chip-name">{{ hw?.ram_gb ?? '—' }} GB</span>
            </div>
          </div>
          <div v-if="ramUsedGb != null" class="chip-live">
            <div class="live-bar"><div class="live-fill fill-mem" :style="{ width: Math.min(100, (ramUsedGb / (hw?.ram_gb || 1)) * 100) + '%' }" /></div>
            <span class="live-text">已用 <b>{{ ramUsedGb.toFixed(1) }} GB</b></span>
          </div>
          <div v-else class="hw-sub">系统内存</div>
        </div>
      </div>
    </section>

    <!-- 实时性能 -->
    <section>
      <div class="sec-head">
        <h2 class="sec-title">实时性能</h2>
        <button class="sec-link" @click="ui.page = 'perf'">查看趋势 →</button>
      </div>
      <PerfRings />
    </section>
  </div>
</template>

<style scoped>
.home { display: flex; flex-direction: column; gap: 18px; }

/* ---- Hero：极光 + 像素渐清晰示意 ---- */
.hero {
  position: relative;
  display: flex;
  align-items: center;
  border-radius: 16px;
  padding: 36px 32px 32px;
  background:
    radial-gradient(420px 260px at 82% -30%, rgba(79, 140, 255, 0.16), transparent 68%),
    radial-gradient(360px 240px at 55% 130%, rgba(139, 92, 246, 0.1), transparent 68%),
    linear-gradient(135deg, #1a2130 0%, #171920 55%, #191a26 100%);
  border: 1px solid rgba(96, 120, 180, 0.22);
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.05), 0 10px 28px rgba(0, 0, 0, 0.28);
  overflow: hidden;
}
.hero-text { position: relative; z-index: 1; }
h1 {
  font-size: 31px;
  font-weight: 750;
  letter-spacing: 1px;
  color: #f2f4f8;
}
.grad {
  background: linear-gradient(90deg, #6fa0ff, #a78bfa);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  margin-left: 8px;
}
.hero-text p { margin: 10px 0 22px; color: #9aa1ad; font-size: 14px; letter-spacing: 0.5px; }
.hero-actions { display: flex; gap: 12px; }
.hero-chip {
  position: absolute;
  right: 24px;
  top: 22px;
  z-index: 1;
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
.chip-dot.off { background: #fbbf24; box-shadow: 0 0 8px rgba(251, 191, 36, 0.8); }

/* 像素渐清晰示意：左 480p 马赛克 → 扫描线 → 右 4K 顺滑 */
.px-demo { position: relative; z-index: 1; margin: 14px 84px 0 auto; flex-shrink: 0; }
.px-screen {
  --cut: 34%;
  position: relative;
  width: 224px;
  height: 132px;
  border-radius: 12px;
  border: 1px solid rgba(140, 160, 210, 0.3);
  overflow: hidden;
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.4), inset 0 0 0 1px rgba(255, 255, 255, 0.03);
  animation: px-sweep 7s ease-in-out infinite;
}
@property --cut {
  syntax: '<percentage>';
  inherits: false;
  initial-value: 34%;
}
@keyframes px-sweep {
  0%, 12% { --cut: 26%; }
  48%, 60% { --cut: 62%; }
  88%, 100% { --cut: 26%; }
}
.px-sharp {
  position: absolute;
  inset: 0;
  background:
    radial-gradient(90px 70px at 74% 26%, rgba(255, 220, 150, 0.5), transparent 70%),
    linear-gradient(165deg, #35567e 0%, #2b3f68 45%, #1d2b4a 100%);
}
.px-sharp::after {
  content: '';
  position: absolute;
  inset: 0;
  background:
    linear-gradient(180deg, transparent 62%, rgba(16, 22, 38, 0.85) 62.5%),
    linear-gradient(200deg, transparent 46%, rgba(20, 30, 52, 0.9) 46.5%);
}
.px-mosaic {
  position: absolute;
  inset: 0;
  background:
    radial-gradient(90px 70px at 74% 26%, rgba(255, 220, 150, 0.5), transparent 70%),
    linear-gradient(165deg, #35567e 0%, #2b3f68 45%, #1d2b4a 100%);
  clip-path: inset(0 calc(100% - var(--cut)) 0 0);
}
/* 马赛克块：两层正交条纹叠出低分辨率色块感 */
.px-mosaic::before {
  content: '';
  position: absolute;
  inset: 0;
  background:
    repeating-linear-gradient(0deg, rgba(255, 255, 255, 0.07) 0 8px, transparent 8px 16px),
    repeating-linear-gradient(90deg, rgba(6, 10, 22, 0.28) 0 8px, transparent 8px 16px);
}
.px-mosaic::after {
  content: '';
  position: absolute;
  inset: 0;
  background:
    linear-gradient(180deg, transparent 62%, rgba(16, 22, 38, 0.85) 62.5%),
    linear-gradient(200deg, transparent 46%, rgba(20, 30, 52, 0.9) 46.5%);
  background-size: 16px 16px, 16px 16px;
}
.px-line {
  position: absolute;
  top: 0;
  bottom: 0;
  left: var(--cut);
  width: 2px;
  background: linear-gradient(180deg, transparent, #9fc0ff 18%, #c4d5ff 50%, #9fc0ff 82%, transparent);
  box-shadow: 0 0 14px rgba(120, 165, 255, 0.95);
}
.px-tag {
  position: absolute;
  bottom: 8px;
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0.6px;
  padding: 2px 8px;
  border-radius: 5px;
  color: rgba(255, 255, 255, 0.85);
  background: rgba(10, 14, 26, 0.72);
  border: 1px solid rgba(255, 255, 255, 0.14);
}
.px-tag.sd { left: 8px; }
.px-tag.hd {
  right: 8px;
  color: #bfe3ff;
  border-color: rgba(140, 180, 255, 0.4);
  box-shadow: 0 0 10px rgba(110, 160, 255, 0.35);
}
@media (max-width: 1180px) {
  .px-demo { display: none; }
}

.card {
  background: linear-gradient(180deg, #1c2027, #181b21);
  border: 1px solid rgba(255, 255, 255, 0.06);
  border-radius: 14px;
}

/* ---- 运行卡 ---- */
.run-card {
  padding: 16px 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  cursor: pointer;
  border-color: rgba(79, 140, 255, 0.38);
  background:
    linear-gradient(90deg, rgba(79, 140, 255, 0.09), rgba(139, 92, 246, 0.04) 42%, transparent 70%),
    linear-gradient(180deg, #1c2027, #181b21);
  transition: border-color 0.18s, box-shadow 0.18s, transform 0.18s;
}
.run-card:hover {
  border-color: rgba(79, 140, 255, 0.7);
  box-shadow: 0 6px 22px rgba(79, 140, 255, 0.16);
  transform: translateY(-1px);
}
.run-info { display: flex; align-items: center; gap: 12px; min-width: 0; }
.run-pulse {
  width: 8px; height: 8px; border-radius: 50%;
  background: #4f8cff;
  box-shadow: 0 0 8px rgba(79, 140, 255, 0.95);
  animation: run-blink 1.6s ease-in-out infinite;
  flex-shrink: 0;
}
@keyframes run-blink { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
.run-label { color: #6fa0ff; font-size: 13px; flex-shrink: 0; }
.run-file { font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.run-progress { min-width: 320px; display: flex; align-items: center; gap: 12px; flex: 1; }
.run-progress > div:first-child { flex: 1; }
.run-pct { font-size: 12.5px; color: #9aa1ad; font-variant-numeric: tabular-nums; white-space: nowrap; }

/* ---- 统计卡 ---- */
.stat-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; }
.stat {
  display: flex; align-items: center; gap: 14px; padding: 18px;
  transition: transform 0.18s ease, border-color 0.18s ease, box-shadow 0.18s ease;
  animation: rise-in 0.45s ease-out backwards;
}
.stat:nth-child(2) { animation-delay: 0.06s; }
.stat:nth-child(3) { animation-delay: 0.12s; }
.stat:nth-child(4) { animation-delay: 0.18s; }
@keyframes rise-in {
  from { opacity: 0; transform: translateY(10px); }
  to { opacity: 1; transform: translateY(0); }
}
.stat:hover {
  transform: translateY(-3px);
  border-color: rgba(255, 255, 255, 0.12);
  box-shadow: 0 8px 18px rgba(0, 0, 0, 0.25);
}
.stat-icon {
  width: 42px; height: 42px; border-radius: 12px;
  display: flex; align-items: center; justify-content: center;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.08);
  flex-shrink: 0;
}
.i-blue { background: rgba(79, 140, 255, 0.14); color: #6fa0ff; box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.08), 0 0 16px rgba(79, 140, 255, 0.12); }
.i-green { background: rgba(52, 211, 153, 0.12); color: #34d399; box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.08), 0 0 16px rgba(52, 211, 153, 0.1); }
.i-purple { background: rgba(139, 92, 246, 0.14); color: #a78bfa; box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.08), 0 0 16px rgba(139, 92, 246, 0.12); }
.i-amber { background: rgba(251, 191, 36, 0.12); color: #fbbf24; box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.08), 0 0 16px rgba(251, 191, 36, 0.1); }
.stat-num { font-size: 22px; font-weight: 750; font-variant-numeric: tabular-nums; }
.stat-label { font-size: 12px; color: #9aa1ad; margin-top: 2px; }

/* ---- 分节标题：品牌渐变短线 ---- */
.sec-title {
  font-size: 15px;
  font-weight: 650;
  color: #d5dae2;
  margin-bottom: 12px;
  display: flex;
  align-items: center;
  gap: 9px;
}
.sec-title::before {
  content: '';
  width: 4px;
  height: 15px;
  border-radius: 3px;
  background: var(--sv-grad);
}

/* 三步上手引导 */
.guide { padding: 22px 24px; }
.guide-title { font-size: 16px; font-weight: 700; margin-bottom: 16px; }
.guide-steps { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 16px; }
.g-step {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  color: #9aa1ad;
  font-size: 13px;
  line-height: 1.6;
}
.g-step b { color: #e9ecf2; margin-right: 4px; }
.g-num {
  width: 22px;
  height: 22px;
  border-radius: 7px;
  background: var(--sv-grad);
  color: #fff;
  font-size: 12px;
  font-weight: 600;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  box-shadow: 0 0 12px rgba(79, 140, 255, 0.35);
}
.guide-actions { display: flex; gap: 12px; margin-top: 18px; }
.sec-head { display: flex; align-items: center; justify-content: space-between; }
.sec-head .sec-title { margin-bottom: 12px; }
.sec-link {
  border: none;
  background: none;
  color: #6fa0ff;
  font-size: 12.5px;
  cursor: pointer;
  padding: 4px 8px;
  margin-bottom: 6px;
  border-radius: 6px;
  transition: background 0.15s;
}
.sec-link:hover { text-decoration: underline; background: rgba(79, 140, 255, 0.08); }

/* ---- 硬件卡 ---- */
.hw-grid { display: grid; grid-template-columns: 1.6fr 1.2fr 0.7fr; gap: 14px; }
.hw { padding: 18px 20px; display: flex; flex-direction: column; gap: 10px; }
.hw-sub { font-size: 12px; color: #8a919d; }
.hw-tags { display: flex; gap: 8px; }

/* 处理器/内存卡：与显卡主卡同语言（小标签 + 金属名 + 实时条），辉光收敛让 GPU 当主角 */
.hw-cpu {
  background:
    radial-gradient(220px 120px at 92% -30%, rgba(79, 140, 255, 0.1), transparent 70%),
    linear-gradient(180deg, #1b202a, #171a21);
  border-color: rgba(96, 130, 200, 0.28);
}
.hw-mem {
  background:
    radial-gradient(180px 110px at 90% -30%, rgba(251, 191, 36, 0.09), transparent 70%),
    linear-gradient(180deg, #1c2027, #181b21);
  border-color: rgba(180, 150, 90, 0.24);
}
.chip-head { display: flex; align-items: center; gap: 11px; min-width: 0; }
.chip-icon { display: inline-flex; color: #7fb0ff; filter: drop-shadow(0 0 5px rgba(79, 140, 255, 0.45)); flex-shrink: 0; }
.chip-icon.icon-amber { color: #fbbf24; filter: drop-shadow(0 0 5px rgba(251, 191, 36, 0.4)); }
.chip-title { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.chip-kind { font-size: 9.5px; font-weight: 600; letter-spacing: 1.8px; color: #7c8ba8; }
/* 金属名：静态银蓝渐变（流光是显卡卡专属，避免满屏动效） */
.chip-name {
  font-size: 14.5px;
  font-weight: 750;
  letter-spacing: 0.3px;
  line-height: 1.2;
  background: linear-gradient(100deg, #d4ddf0 0%, #9db8e8 45%, #e6edf9 100%);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.chip-live { display: flex; align-items: center; gap: 10px; min-width: 0; }
.live-bar {
  flex: 1;
  height: 7px;
  border-radius: 4px;
  background: rgba(255, 255, 255, 0.06);
  overflow: hidden;
}
.live-fill {
  height: 100%;
  border-radius: 4px;
  transition: width 0.6s ease-out;
}
.fill-cpu { background: linear-gradient(90deg, #4f8cff, #22d3ee); box-shadow: 0 0 9px rgba(79, 140, 255, 0.5); }
.fill-mem { background: linear-gradient(90deg, #f59e0b, #fbbf24); box-shadow: 0 0 9px rgba(251, 191, 36, 0.4); }
.live-text { font-size: 12px; color: #9aa1ad; font-variant-numeric: tabular-nums; white-space: nowrap; }
.hw-cpu .live-text b { color: #8ab4ff; font-weight: 650; }
.hw-mem .live-text b { color: #fbbf24; font-weight: 650; }

/* 显卡主卡：整机门面——暗色电路底 + 金属渐变型号名 + 实时显存条 */
.hw-gpu {
  position: relative;
  background:
    radial-gradient(300px 150px at 86% -24%, rgba(79, 140, 255, 0.2), transparent 68%),
    radial-gradient(220px 140px at -6% 118%, rgba(139, 92, 246, 0.14), transparent 68%),
    linear-gradient(180deg, #1a1f2a, #171a21);
  border-color: rgba(96, 130, 200, 0.38);
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.06), 0 0 22px rgba(79, 140, 255, 0.09);
  overflow: hidden;
  justify-content: space-between;
}
/* 电路板走线：右侧极淡的斜向细线 */
.gpu-circuit {
  position: absolute;
  inset: 0;
  pointer-events: none;
  background:
    repeating-linear-gradient(115deg, rgba(140, 170, 255, 0.05) 0 1px, transparent 1px 26px),
    repeating-linear-gradient(115deg, rgba(140, 170, 255, 0.03) 0 1px, transparent 1px 78px);
  -webkit-mask-image: linear-gradient(105deg, transparent 38%, rgba(0, 0, 0, 0.85) 75%);
  mask-image: linear-gradient(105deg, transparent 38%, rgba(0, 0, 0, 0.85) 75%);
}
.gpu-head {
  position: relative;
  display: flex;
  align-items: center;
  gap: 14px;
  min-width: 0;
}
.gpu-icon {
  display: inline-flex;
  color: #7fb0ff;
  filter: drop-shadow(0 0 7px rgba(79, 140, 255, 0.65));
  animation: gpu-breathe 2.6s ease-in-out infinite;
  flex-shrink: 0;
}
@keyframes gpu-breathe {
  0%, 100% { filter: drop-shadow(0 0 5px rgba(79, 140, 255, 0.45)); }
  50% { filter: drop-shadow(0 0 10px rgba(79, 140, 255, 0.85)); }
}
.gpu-title { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.gpu-kind {
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 2px;
  color: #7c8ba8;
}
/* 型号名：银蓝金属渐变 + 缓速流光扫过 */
.gpu-name {
  font-size: 21px;
  font-weight: 800;
  letter-spacing: 0.4px;
  line-height: 1.15;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  background: linear-gradient(100deg, #c8d6f2 0%, #8fb4ff 28%, #eef4ff 50%, #b39cff 72%, #c8d6f2 100%);
  background-size: 220% 100%;
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  animation: gpu-shimmer 7s linear infinite;
}
@keyframes gpu-shimmer {
  0% { background-position: 0% 0; }
  100% { background-position: -220% 0; }
}
.gpu-backend {
  position: relative;
  margin-left: auto;
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  gap: 7px;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.8px;
  color: #9fc2ff;
  padding: 5px 13px;
  border-radius: 999px;
  border: 1px solid rgba(79, 140, 255, 0.45);
  background: linear-gradient(180deg, rgba(79, 140, 255, 0.14), rgba(79, 140, 255, 0.05));
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.08), 0 0 14px rgba(79, 140, 255, 0.14);
}
.gpu-backend .gb-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #4f8cff;
  box-shadow: 0 0 8px rgba(79, 140, 255, 0.9);
  animation: run-blink 1.8s ease-in-out infinite;
}
.gpu-backend.off { color: #fbbf24; border-color: rgba(251, 191, 36, 0.4); background: rgba(251, 191, 36, 0.07); box-shadow: none; }
.gpu-backend.off .gb-dot { background: #fbbf24; box-shadow: 0 0 8px rgba(251, 191, 36, 0.8); animation: none; }
/* 实时显存占用条 */
.gpu-vram {
  position: relative;
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}
.vram-bar {
  flex: 1;
  height: 8px;
  border-radius: 5px;
  background: rgba(255, 255, 255, 0.06);
  overflow: hidden;
}
.vram-fill {
  height: 100%;
  border-radius: 5px;
  background: linear-gradient(90deg, #4f8cff, #8b5cf6);
  box-shadow: 0 0 10px rgba(79, 140, 255, 0.55);
  transition: width 0.6s ease-out;
}
.vram-text {
  font-size: 12px;
  color: #9aa1ad;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.vram-text b { color: #8ab4ff; font-weight: 650; }
</style>
