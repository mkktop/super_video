<script setup lang="ts">
/** 四枚实时占用仪表环（CPU/内存/GPU/显存）：性能页与首页共用，数据来自全局 store（WS 每 2s 推送）。 */
import { computed } from 'vue'
import { store } from '../store'

const RING_C = 226.2 // 2πr, r=36

interface Ring {
  label: string
  color: string
  pct: number
  value: string
  sub: string
  na?: boolean
}

const latest = computed(() => store.perf.latest)

const rings = computed<Ring[]>(() => {
  const l = latest.value
  const hw = store.hardware
  const gpu0 = l?.gpus?.[0] ?? null
  const vramTotalGb = (gpu0?.mem_total_mb ?? 0) / 1024
  const vramUsedGb = (gpu0?.mem_used_mb ?? 0) / 1024
  return [
    {
      label: 'CPU 占用',
      color: 'var(--sv-accent)',
      pct: l?.cpu ?? 0,
      value: l ? `${Math.round(l.cpu)}%` : '—',
      sub: hw ? `${hw.cpu_cores} 核心` : '',
    },
    {
      label: '内存占用',
      color: 'var(--sv-warning-deep)',
      pct: l?.mem_pct ?? 0,
      value: l ? `${Math.round(l.mem_pct)}%` : '—',
      sub: l && hw ? `${l.mem_used_gb} / ${hw.ram_gb} GB` : '',
    },
    {
      label: 'GPU 占用',
      color: 'var(--sv-success)',
      pct: gpu0?.util ?? 0,
      value: gpu0 ? `${gpu0.util ?? 0}%` : '—',
      sub: store.gpuName || '',
      na: !gpu0,
    },
    {
      label: '显存占用',
      color: 'var(--sv-accent-2)',
      pct: vramTotalGb ? (vramUsedGb / vramTotalGb) * 100 : 0,
      value: gpu0 && vramTotalGb ? `${vramUsedGb.toFixed(1)} GB` : '—',
      sub: vramTotalGb ? `总 ${vramTotalGb.toFixed(1)} GB` : '',
      na: !gpu0,
    },
  ]
})
</script>

<template>
  <div class="gauge-grid">
    <div v-for="r in rings" :key="r.label" class="gauge sv-card hoverable">
      <div class="ring-wrap">
        <svg width="88" height="88" viewBox="0 0 88 88">
          <circle cx="44" cy="44" r="36" class="ring-track" />
          <circle
            cx="44"
            cy="44"
            r="36"
            class="ring-val"
            :stroke="r.color"
            :style="{ color: r.color }"
            :stroke-dasharray="`${(RING_C * Math.min(r.pct, 100)) / 100} ${RING_C}`"
          />
        </svg>
        <span class="ring-pct sv-num" :style="{ color: r.na ? 'var(--sv-text-faint)' : r.color }">
          {{ r.na ? '—' : `${Math.round(r.pct)}%` }}
        </span>
      </div>
      <div class="gauge-body">
        <div class="g-label">{{ r.label }}</div>
        <div class="g-value">{{ r.value }}</div>
        <div class="g-sub">{{ r.na ? '暂不支持采集' : r.sub }}</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 窄窗 4→2×2（与首页硬件区同断点），不出 3+1 孤行 */
.gauge-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; }
@media (max-width: 1280px) {
  .gauge-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
.gauge {
  padding: 16px 18px;
  display: flex;
  align-items: center;
  gap: 14px;
}
.ring-wrap { position: relative; width: 88px; height: 88px; flex-shrink: 0; }
.ring-track { fill: none; stroke: var(--sv-fill-3); stroke-width: 8; }
.ring-val {
  fill: none;
  stroke-width: 8;
  stroke-linecap: round;
  transform: rotate(-90deg);
  transform-origin: 44px 44px;
  transition: stroke-dasharray 0.5s ease-out;
  filter: drop-shadow(0 0 5px currentColor);
}
.ring-pct {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 15px;
  font-weight: 750;
  font-variant-numeric: tabular-nums;
}
.gauge-body { min-width: 0; }
.g-label { font-size: 12.5px; color: var(--sv-text-dim); }
.g-value {
  font-size: 22px;
  font-weight: 750;
  margin: 3px 0 2px;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.02em;
  white-space: nowrap;
}
.g-sub {
  font-size: 11.5px;
  color: var(--sv-text-faint);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
