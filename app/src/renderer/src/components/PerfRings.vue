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
      color: '#4f8cff',
      pct: l?.cpu ?? 0,
      value: l ? `${Math.round(l.cpu)}%` : '—',
      sub: hw ? `${hw.cpu_cores} 核心` : '',
    },
    {
      label: '内存占用',
      color: '#f59e0b',
      pct: l?.mem_pct ?? 0,
      value: l ? `${Math.round(l.mem_pct)}%` : '—',
      sub: l && hw ? `${l.mem_used_gb} / ${hw.ram_gb} GB` : '',
    },
    {
      label: 'GPU 占用',
      color: '#34d399',
      pct: gpu0?.util ?? 0,
      value: gpu0 ? `${gpu0.util ?? 0}%` : '—',
      sub: store.gpuName || '',
      na: !gpu0,
    },
    {
      label: '显存占用',
      color: '#8b5cf6',
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
    <div v-for="r in rings" :key="r.label" class="gauge">
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
        <span class="ring-pct" :style="{ color: r.na ? '#767d88' : r.color }">
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
.gauge-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; }
.gauge {
  padding: 16px 18px;
  display: flex;
  align-items: center;
  gap: 14px;
  background: linear-gradient(180deg, #1c2027, #181b21);
  border: 1px solid rgba(255, 255, 255, 0.06);
  border-radius: 14px;
  transition: border-color 0.18s, transform 0.18s, box-shadow 0.18s;
}
.gauge:hover {
  border-color: rgba(255, 255, 255, 0.12);
  transform: translateY(-2px);
  box-shadow: 0 8px 22px rgba(0, 0, 0, 0.3);
}
.ring-wrap { position: relative; width: 88px; height: 88px; flex-shrink: 0; }
.ring-track { fill: none; stroke: rgba(255, 255, 255, 0.07); stroke-width: 8; }
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
.g-label { font-size: 12.5px; color: #9aa1ad; }
.g-value {
  font-size: 20px;
  font-weight: 750;
  margin: 3px 0 2px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.g-sub {
  font-size: 11.5px;
  color: #8a919d;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
@media (max-width: 900px) {
  .gauge-grid { grid-template-columns: repeat(2, 1fr); }
}
</style>
