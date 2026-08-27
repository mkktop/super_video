<script lang="ts">
import type { PerfSample } from '../api'

/** 趋势图系列定义:get 从样本取值,null 表示该拍无数据(线在此断开) */
export interface ChartSeries {
  key: string
  label: string
  color: string
  dashed?: boolean
  fill?: boolean
  get: (s: PerfSample) => number | null
}
</script>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

const props = withDefaults(
  defineProps<{
    samples: PerfSample[]
    rangeMin: number
    series: ChartSeries[]
    max?: number
    unit?: string
    height?: number
  }>(),
  { max: 100, unit: '%', height: 190 },
)

const PAD_L = 36
const PAD_R = 12
const PAD_T = 12
const PAD_B = 20

// 容器实测宽度:网格/刻度按像素渲染,文字不因拉伸变形
const wrap = ref<HTMLDivElement | null>(null)
const width = ref(600)
let ro: ResizeObserver | null = null

onMounted(() => {
  if (!wrap.value) return
  width.value = Math.max(320, wrap.value.clientWidth)
  ro = new ResizeObserver((entries) => {
    width.value = Math.max(320, entries[0].contentRect.width)
  })
  ro.observe(wrap.value)
})
onBeforeUnmount(() => ro?.disconnect())

// ---- 图例开关 ----
const hidden = ref(new Set<string>())

function toggle(key: string) {
  const s = new Set(hidden.value)
  if (s.has(key)) s.delete(key)
  else s.add(key)
  hidden.value = s
}

// ---- 数据窗口与抽稀 ----
const rangeS = computed(() => props.rangeMin * 60)
const nowT = computed(() => props.samples[props.samples.length - 1]?.t ?? Date.now() / 1000)
const t0 = computed(() => nowT.value - rangeS.value)
const vis = computed(() => props.samples.filter((s) => s.t >= t0.value - 2))

/** 可见样本抽稀到 ≤720 点(1 小时 1800 点时 stride=3),末点始终保留 */
const shown = computed(() => {
  const step = Math.max(1, Math.ceil(vis.value.length / 720))
  const out: PerfSample[] = []
  for (let i = 0; i < vis.value.length; i += step) out.push(vis.value[i])
  const last = vis.value[vis.value.length - 1]
  if (last && out[out.length - 1] !== last) out.push(last)
  return out
})

const iw = computed(() => width.value - PAD_L - PAD_R)
const ih = computed(() => props.height - PAD_T - PAD_B)

function xAt(t: number): number {
  return PAD_L + ((t - t0.value) / rangeS.value) * iw.value
}
function yAt(v: number): number {
  return PAD_T + (1 - Math.min(v, props.max) / props.max) * ih.value
}

/** 每系列 SVG path:数据缺失处断笔(M 起新段),fill 系列依赖隐式闭合在各段下方着色 */
const paths = computed(() =>
  props.series.map((se) => {
    let d = ''
    let pen = false
    for (const s of shown.value) {
      const v = se.get(s)
      if (v == null || Number.isNaN(v)) {
        pen = false
        continue
      }
      d += `${pen ? 'L' : 'M'}${xAt(s.t).toFixed(1)},${yAt(v).toFixed(1)}`
      pen = true
    }
    return { ...se, d }
  }),
)

// ---- 网格与刻度 ----
const gridYs = computed(() => {
  const step = props.max / 4
  return [0, 1, 2, 3, 4].map((i) => {
    const v = step * i
    return {
      y: yAt(v),
      label: props.max <= 10 ? v.toFixed(1) : String(Math.round(v)),
    }
  })
})

const gridXs = computed(() => {
  const cand = [30, 60, 120, 300, 600, 900, 1800, 3600]
  const step = cand.find((s) => rangeS.value / s <= 6) ?? 3600
  const out: { x: number; label: string }[] = []
  for (let t = Math.ceil(t0.value / step) * step; t <= nowT.value; t += step) {
    out.push({ x: xAt(t), label: fmtTime(t) })
  }
  return out
})

function fmtTime(t: number): string {
  return new Date(t * 1000).toTimeString().slice(0, 5)
}

// ---- 悬停十字线与数值 ----
const hoverI = ref(-1)

function onMove(e: MouseEvent) {
  const rect = (e.currentTarget as SVGSVGElement).getBoundingClientRect()
  const px = e.clientX - rect.left
  let best = -1
  let bd = Infinity
  shown.value.forEach((s, i) => {
    const d = Math.abs(xAt(s.t) - px)
    if (d < bd) {
      bd = d
      best = i
    }
  })
  hoverI.value = best
}

const hoverSample = computed(() =>
  hoverI.value >= 0 ? (shown.value[hoverI.value] ?? null) : null,
)
const hoverX = computed(() => (hoverSample.value ? xAt(hoverSample.value.t) : 0))
const tipLeft = computed(() => Math.min(Math.max(hoverX.value, 80), width.value - 80))

/** 悬停时刻各可见系列的落点与 tooltip 行 */
const hoverDots = computed(() => {
  const s = hoverSample.value
  if (!s) return []
  return props.series
    .filter((se) => !hidden.value.has(se.key))
    .map((se) => {
      const v = se.get(s)
      return { key: se.key, color: se.color, y: v == null || Number.isNaN(v) ? -99 : yAt(v) }
    })
})
const tipRows = computed(() => {
  const s = hoverSample.value
  if (!s) return []
  return props.series
    .filter((se) => !hidden.value.has(se.key))
    .map((se) => ({ key: se.key, color: se.color, label: se.label, val: fmtVal(se.get(s)) }))
})

function fmtVal(v: number | null): string {
  return v == null || Number.isNaN(v) ? '—' : `${Math.round(v * 10) / 10}${props.unit}`
}
</script>

<template>
  <div ref="wrap" class="tc-wrap">
    <div class="tc-legend">
      <button
        v-for="se in series"
        :key="se.key"
        class="lg"
        :class="{ off: hidden.has(se.key) }"
        @click="toggle(se.key)"
      >
        <span
          class="swatch"
          :style="{ background: hidden.has(se.key) ? '#3a3f45' : se.color }"
        />
        {{ se.label }}
      </button>
    </div>
    <svg
      :width="width"
      :height="height"
      class="tc-svg"
      @mousemove="onMove"
      @mouseleave="hoverI = -1"
    >
      <!-- 网格 -->
      <line
        v-for="g in gridYs"
        :key="'y' + g.y"
        :x1="PAD_L"
        :x2="width - PAD_R"
        :y1="g.y"
        :y2="g.y"
        class="grid-line"
      />
      <text v-for="g in gridYs" :key="'yt' + g.y" :x="PAD_L - 6" :y="g.y + 3.5" class="tick">
        {{ g.label }}
      </text>
      <text v-for="g in gridXs" :key="'x' + g.x" :x="g.x" :y="height - 6" class="tick tick-x">
        {{ g.label }}
      </text>

      <!-- 曲线:fill 系列先铺底色 -->
      <template v-for="p in paths" :key="p.key">
        <path v-if="p.fill && !hidden.has(p.key)" :d="p.d" :fill="p.color" fill-opacity="0.1" />
      </template>
      <template v-for="p in paths" :key="'l' + p.key">
        <path
          v-if="!hidden.has(p.key)"
          :d="p.d"
          fill="none"
          :stroke="p.color"
          stroke-width="1.6"
          stroke-linejoin="round"
          stroke-linecap="round"
          :stroke-dasharray="p.dashed ? '5 4' : undefined"
        />
      </template>

      <!-- 悬停十字线与各系列落点 -->
      <g v-if="hoverSample">
        <line
          :x1="hoverX"
          :x2="hoverX"
          :y1="PAD_T"
          :y2="height - PAD_B"
          class="crosshair"
        />
        <circle
          v-for="d in hoverDots"
          :key="'d' + d.key"
          :cx="hoverX"
          :cy="d.y"
          r="3"
          :fill="d.color"
        />
      </g>

      <text v-if="vis.length < 2" :x="width / 2" :y="height / 2" class="empty">
        暂无采样数据
      </text>
    </svg>

    <div v-if="hoverSample" class="tc-tip" :style="{ left: `${tipLeft}px` }">
      <div class="tip-time">{{ fmtTime(hoverSample.t) }}</div>
      <div v-for="row in tipRows" :key="row.key" class="tip-row">
        <span class="tip-dot" :style="{ background: row.color }" />
        <span class="tip-label">{{ row.label }}</span>
        <span class="tip-val">{{ row.val }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.tc-wrap { position: relative; }
.tc-legend { display: flex; gap: 6px; margin-bottom: 6px; flex-wrap: wrap; }
.lg {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: none;
  background: transparent;
  color: #9aa0a6;
  font-size: 12px;
  padding: 3px 8px;
  border-radius: 6px;
  cursor: pointer;
}
.lg:hover { background: #23262b; color: #e8eaed; }
.lg.off { color: #767d88; text-decoration: line-through; }
.swatch { width: 9px; height: 9px; border-radius: 2.5px; }
.tc-svg { display: block; }
.grid-line { stroke: #24272c; stroke-width: 1; }
.tick { fill: #8a919c; font-size: 10.5px; text-anchor: end; }
.tick-x { text-anchor: middle; }
.crosshair { stroke: #4a4f57; stroke-width: 1; stroke-dasharray: 3 3; }
.empty { fill: #767d88; font-size: 12.5px; text-anchor: middle; }
.tc-tip {
  position: absolute;
  top: 8px;
  transform: translateX(-50%);
  background: #26282c;
  border: 1px solid #33363b;
  border-radius: 8px;
  padding: 8px 10px;
  pointer-events: none;
  min-width: 128px;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.45);
  z-index: 5;
}
.tip-time { font-size: 11px; color: #9aa0a6; margin-bottom: 5px; }
.tip-row { display: flex; align-items: center; gap: 7px; font-size: 12px; line-height: 1.7; }
.tip-dot { width: 8px; height: 8px; border-radius: 2px; flex-shrink: 0; }
.tip-label { color: #c6cad0; }
.tip-val { margin-left: auto; font-variant-numeric: tabular-nums; color: #e8eaed; }
</style>
