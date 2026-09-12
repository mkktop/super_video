<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, watch } from 'vue'
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

// ---- 统计数字滚动补间：stats 变化时 200ms rAF 从旧值插值到新值（仅变化后触发） ----
const shown = reactive({ ...store.stats })
let rafId = 0
let firstStats = true
watch(
  () => [store.stats.total, store.stats.done, store.stats.frames, store.stats.bytes],
  () => {
    const from = { ...shown }
    const to = { ...store.stats }
    if (firstStats) {
      firstStats = false
      Object.assign(shown, to)
      return
    }
    cancelAnimationFrame(rafId)
    const t0 = performance.now()
    const step = (now: number) => {
      const k = Math.min(1, (now - t0) / 200)
      const e = 1 - (1 - k) * (1 - k)
      shown.total = Math.round(from.total + (to.total - from.total) * e)
      shown.done = Math.round(from.done + (to.done - from.done) * e)
      shown.frames = Math.round(from.frames + (to.frames - from.frames) * e)
      shown.bytes = Math.round(from.bytes + (to.bytes - from.bytes) * e)
      if (k < 1) rafId = requestAnimationFrame(step)
    }
    rafId = requestAnimationFrame(step)
  },
)
onBeforeUnmount(() => cancelAnimationFrame(rafId))

const runPercent = computed(() => {
  const r = running.value
  if (!r || !r.total_frames) return 0
  return Math.min(100, Math.round((r.progress_frames / r.total_frames) * 100))
})

const hw = computed(() => store.hardware)

// naive Line 进度的渐变色只认 { stops: [from, to] } 对象形态（数组形态会崩）
const gradFill: { stops: [string, string] } = { stops: ['var(--sv-accent)', 'var(--sv-accent-2)'] }

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

// 引擎胶囊里的显卡短名：去掉 NVIDIA GeForce 前缀与 (R)/(TM)，全名在下方显卡主卡展示
const gpuShortName = computed(() =>
  (store.gpuName || '').replace(/NVIDIA\s+GeForce\s+/i, '').replace(/\((R|TM)\)/gi, '').trim(),
)
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
      <!-- 右侧：引擎状态胶囊 + 像素重构示意（垂直成组，任何窗宽都不与文字相叠） -->
      <div class="hero-side">
        <div class="engine-chip" :class="{ off: !store.engine }">
          <span class="ec-dot" />
          <span class="ec-status">{{ store.engine ? '引擎就绪' : '引擎未就绪' }}</span>
          <template v-if="store.engine">
            <span class="ec-sep" />
            <span class="ec-backend">{{ backendLabel }}</span>
          </template>
          <template v-if="gpuShortName">
            <span class="ec-sep" />
            <span class="ec-gpu">{{ gpuShortName }}</span>
          </template>
        </div>
        <!-- 像素重构示意：左 1/3 马赛克(480p) / 右 2/3 锐利(4K)，
             品牌扫描线 4s 一轮从左向右扫过，扫过之处像素"重构"为清晰细节 -->
        <div class="px-demo" aria-hidden="true">
          <div class="px-screen">
            <div class="px-art px-sharp" />
            <div class="px-art px-mosaic" />
            <div class="px-band" />
            <div class="px-line" />
            <span class="px-tag sd">480p</span>
            <span class="px-tag hd">4K</span>
          </div>
        </div>
      </div>
    </section>

    <!-- 运行状态 -->
    <section v-if="running" class="card run-card sv-card" @click="ui.page = 'tasks'">
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
        <span class="run-pct sv-num">
          {{ runPercent }}% · {{ running.progress_frames }}/{{ running.total_frames }} 帧<template v-if="running.fps_run"> · {{ running.fps_run.toFixed(1) }} 帧/秒 · 剩余 {{ fmtEta(running.eta_sec) }}</template>
        </span>
      </div>
    </section>

    <!-- 三步上手（仅无任何历史任务时展示） -->
    <section v-if="fresh && !running && store.ready" class="card guide sv-card">
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

    <!-- 统计：加载中给等高骨架，避免就位时跳动（CLS） -->
    <section v-if="!store.ready" class="stat-grid" aria-hidden="true">
      <div v-for="i in 4" :key="i" class="card stat sv-card">
        <div class="sv-skeleton skel-icon" />
        <div class="skel-stat-text">
          <div class="sv-skeleton" style="height: 30px; width: 76px" />
          <div class="sv-skeleton" style="height: 12px; width: 56px; margin-top: 7px" />
        </div>
      </div>
    </section>
    <section v-else-if="!fresh" class="stat-grid">
      <div class="card stat sv-card hoverable">
        <div class="stat-icon i-blue">
          <svg width="20" height="20" viewBox="0 0 20 20"><rect x="3" y="2.5" width="14" height="15" rx="2.4" fill="none" stroke="currentColor" stroke-width="1.5" /><path d="M7 6.5h6M7 10h6M7 13.5h3.6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" /></svg>
        </div>
        <div>
          <div class="stat-num sv-num">{{ shown.total }}</div>
          <div class="stat-label">累计任务</div>
        </div>
      </div>
      <div class="card stat sv-card hoverable">
        <div class="stat-icon i-green">
          <svg width="20" height="20" viewBox="0 0 20 20"><path d="M4 10.5l4 4 8-9" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" /></svg>
        </div>
        <div>
          <div class="stat-num sv-num">{{ shown.done }}</div>
          <div class="stat-label">已完成</div>
        </div>
      </div>
      <div class="card stat sv-card hoverable">
        <div class="stat-icon i-purple">
          <svg width="20" height="20" viewBox="0 0 20 20"><rect x="2.5" y="4.5" width="15" height="11" rx="2" fill="none" stroke="currentColor" stroke-width="1.5" /><path d="M2.5 8h15M7 4.5v11M13 4.5v11" stroke="currentColor" stroke-width="1.2" /></svg>
        </div>
        <div>
          <div class="stat-num sv-num">{{ fmtFrames(shown.frames) }}</div>
          <div class="stat-label">累计处理帧</div>
        </div>
      </div>
      <div class="card stat sv-card hoverable">
        <div class="stat-icon i-amber">
          <svg width="20" height="20" viewBox="0 0 20 20"><ellipse cx="10" cy="5.2" rx="6.5" ry="2.7" fill="none" stroke="currentColor" stroke-width="1.5" /><path d="M3.5 5.2v9.6c0 1.5 2.9 2.7 6.5 2.7s6.5-1.2 6.5-2.7V5.2" fill="none" stroke="currentColor" stroke-width="1.5" /><path d="M3.5 10c0 1.5 2.9 2.7 6.5 2.7s6.5-1.2 6.5-2.7" fill="none" stroke="currentColor" stroke-width="1.5" /></svg>
        </div>
        <div>
          <div class="stat-num sv-num">{{ fmtBytes(shown.bytes) }}</div>
          <div class="stat-label">累计产出</div>
        </div>
      </div>
    </section>

    <!-- 硬件 -->
    <section>
      <h2 class="sec-title">硬件信息</h2>
      <!-- 硬件信息骨架：与真实布局同构（显卡通栏 + 两张副卡） -->
      <div v-if="!hw && !store.initError" class="hw-grid" aria-hidden="true">
        <div class="card hw hw-gpu sv-card">
          <div class="sv-skeleton" style="height: 24px; width: 44%" />
          <div class="sv-skeleton" style="height: 8px; width: 100%" />
          <div class="sv-skeleton" style="height: 18px; width: 30%" />
        </div>
        <div class="card hw sv-card">
          <div class="sv-skeleton" style="height: 20px; width: 80%" />
          <div class="sv-skeleton" style="height: 7px; width: 100%" />
          <div class="sv-skeleton" style="height: 12px; width: 40%" />
        </div>
        <div class="card hw sv-card">
          <div class="sv-skeleton" style="height: 20px; width: 60%" />
          <div class="sv-skeleton" style="height: 7px; width: 100%" />
          <div class="sv-skeleton" style="height: 12px; width: 36%" />
        </div>
      </div>
      <div v-else class="hw-grid">
      <div class="card hw hw-gpu sv-card">
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
          <span class="vram-text sv-num">
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
        <div class="card hw hw-cpu sv-card">
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
            <span class="live-text sv-num">
              占用 <b>{{ cpuPct }}%</b><template v-if="procCpuPct != null"> · 进程 {{ procCpuPct }}%</template>
            </span>
          </div>
          <div v-else class="hw-sub">{{ hw?.cpu_cores ?? '—' }} 核心</div>
          <div v-if="cpuPct != null" class="hw-sub">{{ hw?.cpu_cores ?? '—' }} 核心</div>
        </div>
        <div class="card hw hw-mem sv-card">
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
            <span class="live-text sv-num">已用 <b>{{ ramUsedGb.toFixed(1) }} GB</b></span>
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

/* ---- Hero：极光 + 像素重构示意 ---- */
.hero {
  position: relative;
  display: flex;
  align-items: center;
  border-radius: var(--sv-radius-lg);
  padding: 36px 32px 32px;
  background: var(--sv-hero-grad);
  border: 1px solid var(--sv-hero-border);
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.05), 0 10px 28px rgba(0, 0, 0, 0.28);
  overflow: hidden;
}
.hero-text { position: relative; z-index: 1; }
h1 {
  font-size: 31px;
  font-weight: 750;
  letter-spacing: 1px;
  color: var(--sv-hero-fg);
}
.grad {
  background: linear-gradient(90deg, var(--sv-accent-strong), var(--sv-accent-2-strong));
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  margin-left: 8px;
}
.hero-text p { margin: 10px 0 22px; color: var(--sv-text-dim); font-size: 14px; letter-spacing: 0.5px; }
.hero-actions { display: flex; gap: 12px; }

/* 右侧状态列：胶囊在上、演示图在下，随内容自适应不相叠 */
.hero-side {
  position: relative;
  z-index: 1;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 14px;
  margin: 0 6px 0 auto;
  flex-shrink: 0;
}
/* 引擎状态胶囊：分段式——状态灯 / 引擎就绪 / 后端 / 显卡短名 */
.engine-chip {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  padding: 7px 15px;
  border-radius: 999px;
  background: var(--sv-chip-bg);
  border: 1px solid var(--sv-border-mid);
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.05), 0 4px 14px rgba(0, 0, 0, 0.25);
  white-space: nowrap;
}
.ec-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--sv-success);
  box-shadow: 0 0 9px rgba(var(--sv-success-rgb), 0.95);
  animation: run-blink 2.2s ease-in-out infinite;
}
.ec-status { font-size: 12px; font-weight: 600; color: var(--sv-success-strong); }
.ec-sep { width: 1px; height: 12px; background: var(--sv-border-mid); }
.ec-backend {
  font-size: 11px;
  font-weight: 750;
  letter-spacing: 1.2px;
  text-transform: uppercase;
  color: var(--sv-accent-strong);
  text-shadow: 0 0 12px rgba(var(--sv-accent-rgb), 0.45);
}
.ec-gpu { font-size: 12px; color: var(--sv-text-dim); letter-spacing: 0.2px; }
.engine-chip.off { border-color: rgba(var(--sv-warning-rgb), 0.28); }
.engine-chip.off .ec-dot { background: var(--sv-warning); box-shadow: 0 0 9px rgba(var(--sv-warning-rgb), 0.85); animation: none; }
.engine-chip.off .ec-status { color: var(--sv-warning); }

/* ---- 像素重构示意（主打记忆点）----
   静止格局：左 1/3 马赛克(480p) + 右 2/3 锐利(4K)；
   一条 2px 品牌扫描线 4s 一轮从左向右扫过，扫过之处叠出清晰的"重构带"。
   几何全部由 --scan（注册自定义属性）驱动 clip-path/位移，GPU 合成不触发布局。 */
.px-demo { position: relative; flex-shrink: 0; }
.px-screen {
  --split: 34%;
  --scan: 18%;
  position: relative;
  width: 224px;
  height: 132px;
  border-radius: var(--sv-radius-md);
  border: 1px solid var(--sv-hero-border);
  overflow: hidden;
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.4), inset 0 0 0 1px rgba(255, 255, 255, 0.03);
}
@property --scan {
  syntax: '<percentage>';
  inherits: false;
  initial-value: 18%;
}
@media (prefers-reduced-motion: no-preference) {
  .px-screen { animation: px-scan 4s cubic-bezier(0.45, 0.1, 0.35, 1) infinite; }
  .px-band, .px-line { animation: px-scan-fade 4s linear infinite; }
}
@keyframes px-scan {
  0% { --scan: 18%; }
  86% { --scan: 100%; }
  100% { --scan: 100%; }
}
@keyframes px-scan-fade {
  0% { opacity: 0; }
  7% { opacity: 1; }
  82% { opacity: 1; }
  90%, 100% { opacity: 0; }
}
/* 场景底画（清晰层全幅铺满） */
.px-art { position: absolute; inset: 0; background: var(--sv-px-scene); }
.px-sharp::after {
  content: '';
  position: absolute;
  inset: 0;
  background: var(--sv-px-detail);
}
/* 马赛克层：只露左 split%；粗像素网点 + 正交色块条纹 */
.px-mosaic {
  clip-path: inset(0 calc(100% - var(--split)) 0 0);
}
.px-mosaic::before {
  content: '';
  position: absolute;
  inset: 0;
  background:
    radial-gradient(circle at center, rgba(6, 10, 22, 0.34) 1.4px, transparent 1.5px),
    repeating-linear-gradient(0deg, rgba(255, 255, 255, 0.07) 0 8px, transparent 8px 16px),
    repeating-linear-gradient(90deg, rgba(6, 10, 22, 0.28) 0 8px, transparent 8px 16px);
  background-size: 8px 8px, auto, auto;
}
.px-mosaic::after {
  content: '';
  position: absolute;
  inset: 0;
  background: var(--sv-px-detail);
  background-size: 16px 16px, 16px 16px;
}
/* 重构带：扫描线身后 16% 宽的清晰画（带一点提亮），把马赛克"洗"成细节 */
.px-band {
  position: absolute;
  inset: 0;
  background:
    linear-gradient(90deg, rgba(255, 255, 255, 0.1), rgba(255, 255, 255, 0.02) 70%, transparent),
    var(--sv-px-detail),
    var(--sv-px-scene);
  clip-path: inset(0 calc(100% - var(--scan)) 0 calc(var(--scan) - 16%));
}
/* 扫描线：2px 品牌渐变竖线 + 辉光 */
.px-line {
  position: absolute;
  top: 0;
  bottom: 0;
  left: var(--scan);
  width: 2px;
  background: linear-gradient(180deg, transparent, var(--sv-accent-strong) 18%, #c4d5ff 50%, var(--sv-accent-strong) 82%, transparent);
  box-shadow: 0 0 14px rgba(var(--sv-accent-rgb), 0.95);
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
  border-color: rgba(var(--sv-accent-rgb), 0.4);
  box-shadow: 0 0 10px rgba(var(--sv-accent-rgb), 0.35);
}
/* 减少动态：退化为静态 50% 对比图（隐藏扫描线与重构带，分界线居中） */
@media (prefers-reduced-motion: reduce) {
  .px-band, .px-line { display: none; }
  .px-mosaic { clip-path: inset(0 50% 0 0); }
}
@media (max-width: 1180px) {
  .px-demo { display: none; }
}

/* ---- 运行卡 ---- */
.run-card {
  padding: 16px 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  cursor: pointer;
  border-color: rgba(var(--sv-accent-rgb), 0.38);
  background:
    linear-gradient(90deg, rgba(var(--sv-accent-rgb), 0.09), rgba(var(--sv-accent2-rgb), 0.04) 42%, transparent 70%),
    var(--sv-panel-grad);
  transition: border-color 0.18s var(--sv-ease), box-shadow 0.18s var(--sv-ease), transform 0.18s var(--sv-ease);
}
.run-card:hover {
  border-color: rgba(var(--sv-accent-rgb), 0.7);
  box-shadow: 0 6px 22px rgba(var(--sv-accent-rgb), 0.16);
  transform: translateY(-1px);
}
.run-info { display: flex; align-items: center; gap: 12px; min-width: 0; }
.run-pulse {
  width: 8px; height: 8px; border-radius: 50%;
  background: var(--sv-accent);
  box-shadow: 0 0 8px rgba(var(--sv-accent-rgb), 0.95);
  animation: run-blink 1.6s ease-in-out infinite;
  flex-shrink: 0;
}
@keyframes run-blink { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
.run-label { color: var(--sv-accent-strong); font-size: 13px; flex-shrink: 0; }
.run-file { font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.run-progress { min-width: 220px; display: flex; align-items: center; gap: 12px; flex: 1; }
.run-progress > div:first-child { flex: 1; }
.run-pct { font-size: 12.5px; color: var(--sv-text-dim); font-variant-numeric: tabular-nums; white-space: nowrap; }

/* ---- 统计卡 ---- */
/* 统计卡：窄窗 4→2×2（与硬件区同断点），auto-fit 会出 3+1 孤行故用显式断点 */
.stat-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; }
.stat {
  display: flex; align-items: center; gap: 14px; padding: 18px;
  animation: rise-in 0.45s var(--sv-ease) backwards;
}
.stat:nth-child(2) { animation-delay: 0.06s; }
.stat:nth-child(3) { animation-delay: 0.12s; }
.stat:nth-child(4) { animation-delay: 0.18s; }
@keyframes rise-in {
  from { opacity: 0; transform: translateY(10px); }
  to { opacity: 1; transform: translateY(0); }
}
.stat-icon {
  width: 42px; height: 42px; border-radius: var(--sv-radius-md);
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0;
}
.i-blue { background: rgba(var(--sv-accent-rgb), 0.14); color: var(--sv-accent-strong); box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.08), 0 0 16px rgba(var(--sv-accent-rgb), 0.12); }
.i-green { background: rgba(var(--sv-success-rgb), 0.12); color: var(--sv-success); box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.08), 0 0 16px rgba(var(--sv-success-rgb), 0.1); }
.i-purple { background: rgba(var(--sv-accent2-rgb), 0.14); color: var(--sv-accent-2-strong); box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.08), 0 0 16px rgba(var(--sv-accent2-rgb), 0.12); }
.i-amber { background: rgba(var(--sv-warning-rgb), 0.12); color: var(--sv-warning); box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.08), 0 0 16px rgba(var(--sv-warning-rgb), 0.1); }
/* 统计大数字：28px 纯白 + 紧排等宽；标签弱一档 */
.stat-num {
  font-size: 28px;
  font-weight: 750;
  color: var(--sv-text);
  line-height: 1.1;
}
.stat-label { font-size: 12px; color: var(--sv-text-faint); margin-top: 3px; }
.skel-icon { width: 42px; height: 42px; border-radius: var(--sv-radius-md); flex-shrink: 0; }
.skel-stat-text { flex: 1; }

/* ---- 分节标题：品牌渐变短线 ---- */
.sec-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--sv-text);
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
  color: var(--sv-text-dim);
  font-size: 13px;
  line-height: 1.6;
}
.g-step b { color: var(--sv-text); margin-right: 4px; }
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
  box-shadow: 0 0 12px rgba(var(--sv-accent-rgb), 0.35);
}
.guide-actions { display: flex; gap: 12px; margin-top: 18px; }
.sec-head { display: flex; align-items: center; justify-content: space-between; }
.sec-head .sec-title { margin-bottom: 12px; }
.sec-link {
  border: none;
  background: none;
  color: var(--sv-accent-strong);
  font-size: 12.5px;
  cursor: pointer;
  padding: 4px 8px;
  margin-bottom: 6px;
  border-radius: 6px;
  transition: background var(--sv-dur-fast) ease;
}
.sec-link:hover { text-decoration: underline; background: var(--sv-accent-bg); }

/* ---- 硬件卡 ---- */
/* 轨道必须 minmax(0,·)：fr 默认 min-size=auto，卡内 nowrap 长名（CPU 型号）会把
   轨道撑破比例、横向溢出窗口（实测 1440 宽挤爆显卡卡），min-width:0 后交给省略号 */
.hw-grid { display: grid; grid-template-columns: minmax(0, 1.6fr) minmax(0, 1.2fr) minmax(0, 0.7fr); gap: 14px; }
.hw { padding: 18px 20px; display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.hw-sub { font-size: 12px; color: var(--sv-text-faint); }
.hw-tags { display: flex; gap: 8px; flex-wrap: wrap; }

/* 窄窗换行不挤压：显卡主卡独占一行，处理器/内存合一行；更窄全单列 */
@media (max-width: 1280px) {
  .hw-grid { grid-template-columns: minmax(0, 1.35fr) minmax(0, 0.65fr); }
  .hw-gpu { grid-column: 1 / -1; }
  .stat-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 819px) {
  .hw-grid { grid-template-columns: minmax(0, 1fr); }
  .hw-gpu { grid-column: auto; }
  .hero { padding: 26px 22px 24px; }
}

/* 处理器/内存卡：与显卡主卡同语言（小标签 + 金属名 + 实时条），辉光收敛让 GPU 当主角 */
.hw-cpu {
  background: var(--sv-cpu-grad);
  border-color: var(--sv-cpu-border);
}
.hw-mem {
  background: var(--sv-mem-grad);
  border-color: var(--sv-mem-border);
}
.chip-head { display: flex; align-items: center; gap: 11px; min-width: 0; }
.chip-icon { display: inline-flex; color: var(--sv-accent-strong); filter: drop-shadow(0 0 5px rgba(var(--sv-accent-rgb), 0.45)); flex-shrink: 0; }
.chip-icon.icon-amber { color: var(--sv-warning); filter: drop-shadow(0 0 5px rgba(var(--sv-warning-rgb), 0.4)); }
.chip-title { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.chip-kind {
  font-size: 9.5px;
  font-weight: 600;
  letter-spacing: 1.8px;
  color: var(--sv-text-faint);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
/* 金属名：静态银蓝渐变（流光是显卡卡专属，避免满屏动效） */
.chip-name {
  font-size: 14.5px;
  font-weight: 750;
  letter-spacing: 0.3px;
  line-height: 1.2;
  background: var(--sv-metal-grad-2);
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
  background: var(--sv-fill-3);
  overflow: hidden;
}
.live-fill {
  height: 100%;
  border-radius: 4px;
  transition: width 0.6s ease-out;
}
.fill-cpu { background: linear-gradient(90deg, var(--sv-accent), var(--sv-accent-cyan)); box-shadow: 0 0 9px rgba(var(--sv-accent-rgb), 0.5); }
.fill-mem { background: linear-gradient(90deg, var(--sv-warning-deep), var(--sv-warning)); box-shadow: 0 0 9px rgba(var(--sv-warning-rgb), 0.4); }
.live-text { font-size: 12px; color: var(--sv-text-dim); font-variant-numeric: tabular-nums; white-space: nowrap; }
.hw-cpu .live-text b { color: var(--sv-accent-strong); font-weight: 650; }
.hw-mem .live-text b { color: var(--sv-warning); font-weight: 650; }

/* 显卡主卡：整机门面——暗色电路底 + 金属渐变型号名 + 实时显存条 */
.hw-gpu {
  position: relative;
  background: var(--sv-gpu-grad);
  border-color: var(--sv-gpu-border);
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.06), 0 0 22px rgba(var(--sv-accent-rgb), 0.09);
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
  color: var(--sv-accent-strong);
  filter: drop-shadow(0 0 7px rgba(var(--sv-accent-rgb), 0.65));
  animation: gpu-breathe 2.6s ease-in-out infinite;
  flex-shrink: 0;
}
@keyframes gpu-breathe {
  0%, 100% { filter: drop-shadow(0 0 5px rgba(var(--sv-accent-rgb), 0.45)); }
  50% { filter: drop-shadow(0 0 10px rgba(var(--sv-accent-rgb), 0.85)); }
}
.gpu-title { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.gpu-kind {
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 2px;
  color: var(--sv-text-faint);
  /* 轨道被压窄时按整行省略，不许 CJK 逐字竖排（实测小窗一列一字） */
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
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
  background: var(--sv-metal-grad);
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
  color: var(--sv-accent-strong);
  padding: 5px 13px;
  border-radius: 999px;
  border: 1px solid rgba(var(--sv-accent-rgb), 0.45);
  background: linear-gradient(180deg, rgba(var(--sv-accent-rgb), 0.14), rgba(var(--sv-accent-rgb), 0.05));
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.08), 0 0 14px rgba(var(--sv-accent-rgb), 0.14);
}
.gpu-backend .gb-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--sv-accent);
  box-shadow: 0 0 8px rgba(var(--sv-accent-rgb), 0.9);
  animation: run-blink 1.8s ease-in-out infinite;
}
.gpu-backend.off { color: var(--sv-warning); border-color: rgba(var(--sv-warning-rgb), 0.4); background: rgba(var(--sv-warning-rgb), 0.07); box-shadow: none; }
.gpu-backend.off .gb-dot { background: var(--sv-warning); box-shadow: 0 0 8px rgba(var(--sv-warning-rgb), 0.8); animation: none; }
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
  background: var(--sv-fill-3);
  overflow: hidden;
}
.vram-fill {
  height: 100%;
  border-radius: 5px;
  background: var(--sv-grad);
  box-shadow: 0 0 10px rgba(var(--sv-accent-rgb), 0.55);
  transition: width 0.6s ease-out;
}
.vram-text {
  font-size: 12px;
  color: var(--sv-text-dim);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  overflow: hidden;
}
.vram-text b { color: var(--sv-accent-strong); font-weight: 650; }
</style>
