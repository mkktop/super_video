<script setup lang="ts">
import { computed, onActivated, onBeforeUnmount, onDeactivated, onMounted, onUnmounted, ref } from 'vue'
import { viewportSplitToFrac } from './compareSliderMath'

const props = withDefaults(
  defineProps<{ srcUrl: string; outUrl: string; labelLeft?: string; labelRight?: string }>(),
  { labelLeft: '处理前', labelRight: '处理后' },
)
const pos = ref(50)
const root = ref<HTMLElement | null>(null)
const outImg = ref<HTMLImageElement | null>(null)

/* ---- 主视图：1:1 像素舞台（默认） ----
 * 整图 contain 适配会把成片一步大比例下采样（如 3564x5120 塞进 ~700px 视口
 * ≈ 1/6 尺寸），超分增加的高频细节恰好在下采样里被平均掉——分割线两侧
 * “一眼看去跟没效果似的”。默认 1:1：成片按原生像素显示、源图平滑放大补齐
 * 到同尺寸，锐利与模糊的分界直接可见；拖动平移、滚轮缩放（光标锚定）、
 * 手柄/←→ 移动分割线，双击在 1:1 与整图适配间切换。 */
const nat = ref<{ w: number; h: number } | null>(null)
const zoom = ref(1)             // 显示像素 / 成片原生像素
const pan = ref({ x: 0, y: 0 }) // 成片左上角在舞台内的偏移（px）
const sizeVer = ref(0)          // 舞台尺寸变化（ResizeObserver）触发重算
const Z_MIN = 0.08
const Z_MAX = 8

const rect = computed(() => {
  void sizeVer.value
  const r = root.value?.getBoundingClientRect()
  return r && r.width > 0 ? r : null
})

const fitScale = computed(() => {
  const r = rect.value
  const n = nat.value
  if (!r || !n) return null
  return Math.min(r.width / n.w, r.height / n.h)
})
const isFit = computed(
  () => fitScale.value !== null && Math.abs(zoom.value - fitScale.value) < 1e-4)
const zoomLabel = computed(() =>
  isFit.value ? '适配' : `${Math.round(zoom.value * 100)}%`)

function clampPan() {
  const r = rect.value
  const n = nat.value
  if (!r || !n) return
  const w = n.w * zoom.value
  const h = n.h * zoom.value
  // 图大于舞台：贴边钳制不露底；小于舞台：居中
  pan.value.x = w <= r.width ? (r.width - w) / 2 : Math.min(0, Math.max(r.width - w, pan.value.x))
  pan.value.y = h <= r.height ? (r.height - h) / 2 : Math.min(0, Math.max(r.height - h, pan.value.y))
}

function onOutLoad() {
  const el = outImg.value
  if (!el?.naturalWidth) return
  nat.value = { w: el.naturalWidth, h: el.naturalHeight }
  zoom.value = 1
  const r = rect.value
  if (r) pan.value = { x: (r.width - el.naturalWidth) / 2, y: (r.height - el.naturalHeight) / 2 }
  clampPan()
}

function setZoom(z: number, anchor?: { x: number; y: number }) {
  if (!nat.value) return
  const nz = Math.min(Z_MAX, Math.max(Z_MIN, z))
  if (anchor && nz !== zoom.value) {
    // 光标锚定：该点的图片像素坐标保持不动 → pan' = a - (a - pan)·k
    const k = nz / zoom.value
    pan.value = {
      x: anchor.x - (anchor.x - pan.value.x) * k,
      y: anchor.y - (anchor.y - pan.value.y) * k,
    }
  }
  zoom.value = nz
  clampPan()
}

function goFit() {
  const s = fitScale.value
  if (s === null) return
  zoom.value = s
  clampPan()
}
function goZoom(z: number) {
  zoom.value = z
  clampPan()
}

function onDbl() {
  if (isFit.value) goZoom(1)
  else goFit()
}

const layerStyle = computed(() => {
  const n = nat.value
  if (!n) return { visibility: 'hidden' as const }
  return {
    width: `${n.w}px`,
    height: `${n.h}px`,
    transform: `translate(${pan.value.x}px, ${pan.value.y}px) scale(${zoom.value})`,
  }
})

/* ---- 平移（舞台拖拽）与分割线（手柄拖拽）分家：1:1 下平移是高频主手势 ---- */
let drag: { mode: 'pan' | 'split'; lx: number; ly: number } | null = null
const panning = ref(false) // 模板光标样式用（drag 本身非响应式）

function onStageDown(e: PointerEvent) {
  if (e.button !== 0) return
  e.preventDefault()
  drag = { mode: 'pan', lx: e.clientX, ly: e.clientY }
  panning.value = true
  root.value?.setPointerCapture(e.pointerId)
}

function onHandleDown(e: PointerEvent) {
  if (e.button !== 0) return
  e.preventDefault()
  e.stopPropagation()
  drag = { mode: 'split', lx: e.clientX, ly: e.clientY }
  root.value?.setPointerCapture(e.pointerId) // 捕获统一挂舞台，move/up 都走舞台处理器
}

function onDragMove(e: PointerEvent) {
  if (!drag) return
  if (drag.mode === 'pan') {
    pan.value = { x: pan.value.x + (e.clientX - drag.lx), y: pan.value.y + (e.clientY - drag.ly) }
    drag.lx = e.clientX
    drag.ly = e.clientY
    clampPan()
  } else {
    setFromX(e.clientX)
  }
}
function onDragUp() {
  drag = null
  panning.value = false
}

function setFromX(clientX: number) {
  const r = rect.value
  if (!r) return
  pos.value = Math.min(100, Math.max(0, ((clientX - r.left) / r.width) * 100))
}

function onKey(e: KeyboardEvent) {
  const step = e.shiftKey ? 5 : 1
  if (e.key === 'ArrowLeft') pos.value = Math.max(0, pos.value - step)
  else if (e.key === 'ArrowRight') pos.value = Math.min(100, pos.value + step)
}
onMounted(() => window.addEventListener('keydown', onKey))
onUnmounted(() => window.removeEventListener('keydown', onKey))
// 父页面（模型对比）KeepAlive 常驻：切页时摘掉全局键盘监听
// （对比页 Compare 走正常卸载路径，两套钩子都挂，addEventListener 同函数幂等）
onActivated(() => window.addEventListener('keydown', onKey))
onDeactivated(() => window.removeEventListener('keydown', onKey))
onBeforeUnmount(() => window.removeEventListener('keydown', onKey))

let ro: ResizeObserver | null = null
onMounted(() => {
  if (root.value && 'ResizeObserver' in window) {
    ro = new ResizeObserver(() => {
      sizeVer.value++
      clampPan()
    })
    ro.observe(root.value)
  }
})
onBeforeUnmount(() => {
  ro?.disconnect()
  ro = null
})

/* ---- 局部放大镜：悬浮跟随光标的放大视窗，分割线与主视图同步 ----
 * 光标 → 图片像素坐标：舞台坐标减 pan 除 zoom（旧版按 contain 逆映射，
 * 主视图改缩放舞台后同一形状的映射换数据源即可）。 */
const LOUPE = 200 // 视窗边长（px）
const loupeOn = ref(false)
const loupeZoom = ref(3)
const cursor = ref<{ x: number; y: number } | null>(null) // 图片像素坐标

const loupeMap = computed(() => {
  const r = rect.value
  const n = nat.value
  if (!r || !n) return null
  return { scale: zoom.value, offX: pan.value.x, offY: pan.value.y, w: n.w, h: n.h, rect: r }
})

/** 分割线在图片本地坐标的比例：竖线画在视口空间（left: pos%），而 clip-path
 *  百分比按图片自身盒解析、随 transform 一起映射——两套参考系不能直接混用，
 *  否则 1:1/适配下中点对齐、越往两边滑偏差越大（∝|pos−50%|·|视口宽−图显宽|）。
 *  主视图裁剪与放大镜共用这一个换算。 */
const splitFrac = computed(() => {
  const f = loupeMap.value
  if (!f) return 0.5
  return viewportSplitToFrac(pos.value, f.rect.width, f.offX, f.w * f.scale)
})

function onMove(e: MouseEvent) {
  if (!loupeOn.value) return
  const f = loupeMap.value
  if (!f) return
  const cx = e.clientX - f.rect.left
  const cy = e.clientY - f.rect.top
  // 只在图片显示区内激活（图外黑底上不显示，避免误导性放大空白）
  if (cx < f.offX || cx > f.offX + f.w * f.scale || cy < f.offY || cy > f.offY + f.h * f.scale) {
    cursor.value = null
    return
  }
  cursor.value = {
    x: Math.min(f.w, Math.max(0, (cx - f.offX) / f.scale)),
    y: Math.min(f.h, Math.max(0, (cy - f.offY) / f.scale)),
  }
}

function onWheel(e: WheelEvent) {
  if (loupeOn.value) {
    loupeZoom.value = Math.min(8, Math.max(1.5, loupeZoom.value * (e.deltaY < 0 ? 1.25 : 0.8)))
    return
  }
  const r = rect.value
  const anchor = r ? { x: e.clientX - r.left, y: e.clientY - r.top } : undefined
  setZoom(zoom.value * (e.deltaY < 0 ? 1.25 : 0.8), anchor)
}

/** 放大视窗定位：光标右下角，贴边翻转 */
const loupeStyle = computed(() => {
  const f = loupeMap.value
  const c = cursor.value
  if (!f || !c) return null
  const px = f.offX + c.x * f.scale
  const py = f.offY + c.y * f.scale
  const gap = 18
  let x = px + gap
  let y = py + gap
  if (x + LOUPE > f.rect.width - 4) x = px - LOUPE - gap
  if (y + LOUPE > f.rect.height - 4) y = py - LOUPE - gap
  return { left: `${Math.max(4, x)}px`, top: `${Math.max(4, y)}px`, width: `${LOUPE}px`, height: `${LOUPE}px` }
})

/** 视窗内图片变换：让图片像素 (cursor) 落在视窗中心（tx = L/2 - Z*px） */
function loupeTransform() {
  const c = cursor.value
  if (!c) return ''
  const z = loupeZoom.value
  const tx = LOUPE / 2 - c.x * z
  const ty = LOUPE / 2 - c.y * z
  return `translate(${tx}px, ${ty}px) scale(${z})`
}
</script>

<template>
  <div
    ref="root"
    class="compare"
    :class="{ louping: loupeOn, grabbing: panning }"
    @pointerdown="onStageDown"
    @pointermove="onDragMove"
    @pointerup="onDragUp"
    @pointercancel="onDragUp"
    @mousemove="onMove"
    @mouseleave="cursor = null"
    @wheel.prevent="onWheel"
    @dblclick="onDbl"
  >
    <img
      ref="outImg"
      class="img"
      :class="{ px: zoom >= 1 }"
      :src="props.outUrl"
      draggable="false"
      :style="layerStyle"
      @load="onOutLoad"
    />
    <img
      class="img top"
      :src="props.srcUrl"
      draggable="false"
      :style="[layerStyle, { clipPath: `inset(0 ${(1 - splitFrac) * 100}% 0 0)` }]"
    />
    <div class="handle" :style="{ left: pos + '%' }" @pointerdown="onHandleDown">
      <div class="line" />
      <div class="knob">⇄</div>
    </div>
    <span class="label label-l">{{ props.labelLeft }}</span>
    <span class="label label-r">{{ props.labelRight }}</span>

    <div class="zoombar" @pointerdown.stop>
      <button :class="{ on: isFit }" @click="goFit">适配</button>
      <button :class="{ on: zoom === 1 && !isFit }" @click="goZoom(1)">1:1</button>
      <button :class="{ on: zoom === 2 }" @click="goZoom(2)">2:1</button>
      <button :class="{ on: zoom === 4 }" @click="goZoom(4)">4:1</button>
      <span class="z-now">{{ zoomLabel }}</span>
    </div>
    <span class="hint">拖动平移 · 滚轮缩放 · 手柄/←→ 移动分割线 · 双击 1:1↔适配</span>
    <button class="loupe-btn" :class="{ on: loupeOn }" @pointerdown.stop @click="loupeOn = !loupeOn">
      🔍 放大镜
    </button>
    <div v-if="loupeOn && loupeStyle" class="loupe" :style="loupeStyle">
      <img class="loupe-img" :src="props.outUrl" draggable="false" :style="{ transform: loupeTransform() }" />
      <img
        class="loupe-img top"
        :src="props.srcUrl"
        draggable="false"
        :style="{ transform: loupeTransform(), clipPath: `inset(0 ${(1 - splitFrac) * 100}% 0 0)` }"
      />
      <div class="loupe-split" :style="{ left: `${splitFrac * 100}%` }" />
      <span class="loupe-zoom">{{ loupeZoom.toFixed(1).replace(/\.0$/, '') }}×</span>
    </div>
  </div>
</template>

<style scoped>
.compare {
  position: relative;
  width: 100%;
  height: 100%;
  overflow: hidden;
  user-select: none;
  cursor: grab;
  background: var(--sv-panel-deep);
  border-radius: 10px;
  touch-action: none;
}
.compare.louping { cursor: crosshair; }
.compare.grabbing { cursor: grabbing; }
/* 两层图同尺寸同变换（成片原生像素 × zoom）；竖线在视口空间定位，
 * 裁剪按 splitFrac 换算到图片本地比例后施加（clip-path 参考系是图片盒） */
.img {
  position: absolute;
  left: 0;
  top: 0;
  max-width: none;
  transform-origin: 0 0;
  pointer-events: none;
}
/* 成片 ≥1:1 时按像素显示（放大检视锯齿/细节是对比本意）；源图恒平滑
 * （它代表“普通放大器能到的程度”，块状硬放大反而失真） */
.img.px { image-rendering: pixelated; }
.handle {
  position: absolute;
  top: 0;
  bottom: 0;
  width: 24px;
  margin-left: -12px;
  cursor: ew-resize;
}
.line {
  position: absolute;
  top: 0;
  bottom: 0;
  left: 11px;
  width: 2px;
  background: linear-gradient(180deg, transparent, var(--sv-accent-strong) 12%, #c4d5ff 50%, var(--sv-accent-strong) 88%, transparent);
  box-shadow: 0 0 10px rgba(var(--sv-accent-rgb), 0.8);
  pointer-events: none;
}
.knob {
  position: absolute;
  top: 50%;
  left: 50%;
  width: 32px;
  height: 32px;
  margin: -16px 0 0 -16px;
  border-radius: 50%;
  background: var(--sv-btn-grad);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.5), 0 0 14px rgba(var(--sv-accent-rgb), 0.5), inset 0 1px 0 rgba(255, 255, 255, 0.25);
  pointer-events: none;
}
.label {
  position: absolute;
  top: 10px;
  font-size: 12px;
  padding: 3px 10px;
  border-radius: 12px;
  background: rgba(0, 0, 0, 0.55);
  color: var(--sv-text);
  pointer-events: none;
  z-index: 3;
}
.label-l { left: 10px; }
.label-r { right: 10px; }
.zoombar {
  position: absolute;
  bottom: 10px;
  left: 50%;
  transform: translateX(-50%);
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 3px 6px;
  border-radius: 14px;
  background: rgba(0, 0, 0, 0.6);
  border: 1px solid var(--sv-border);
  z-index: 3;
}
.zoombar button {
  padding: 2px 10px;
  border: none;
  border-radius: 10px;
  background: transparent;
  color: var(--sv-text-faint);
  font-size: 11.5px;
  cursor: pointer;
  transition: color 0.15s, background 0.15s;
}
.zoombar button:hover { color: var(--sv-text); }
.zoombar button.on {
  color: var(--sv-accent);
  background: rgba(var(--sv-accent-rgb), 0.16);
}
.z-now {
  font-size: 11px;
  color: var(--sv-text-faint);
  padding: 0 6px 0 4px;
  min-width: 34px;
  text-align: right;
}
.hint {
  position: absolute;
  bottom: 10px;
  right: 10px;
  font-size: 11.5px;
  padding: 3px 10px;
  border-radius: 12px;
  background: rgba(0, 0, 0, 0.55);
  color: var(--sv-text-faint);
  pointer-events: none;
  z-index: 3;
}
.loupe-btn {
  position: absolute;
  bottom: 10px;
  left: 10px;
  padding: 3px 12px;
  border-radius: 12px;
  border: 1px solid var(--sv-border);
  background: rgba(0, 0, 0, 0.55);
  color: var(--sv-text-faint);
  font-size: 11.5px;
  cursor: pointer;
  transition: color 0.15s, border-color 0.15s;
  z-index: 3;
}
.loupe-btn:hover { color: var(--sv-text); }
.loupe-btn.on {
  color: var(--sv-accent);
  border-color: rgba(var(--sv-accent-rgb), 0.6);
  background: rgba(var(--sv-accent-rgb), 0.12);
}
/* 放大视窗：双层图按图片像素坐标平移缩放，分割比例与主视图同步 */
.loupe {
  position: absolute;
  overflow: hidden;
  border: 2px solid var(--sv-accent);
  border-radius: 6px;
  background: var(--sv-panel-deep);
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.6);
  pointer-events: none;
  z-index: 6;
}
.loupe-img {
  position: absolute;
  top: 0;
  left: 0;
  max-width: none;
  transform-origin: 0 0;
  image-rendering: pixelated; /* 放大看像素/锯齿差异是对比的本意 */
}
.loupe-split {
  position: absolute;
  top: 0;
  bottom: 0;
  width: 2px;
  margin-left: -1px;
  background: var(--sv-accent);
  box-shadow: 0 0 6px rgba(var(--sv-accent-rgb), 0.8);
}
.loupe-zoom {
  position: absolute;
  bottom: 6px;
  right: 8px;
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 10px;
  background: rgba(0, 0, 0, 0.6);
  color: var(--sv-text-faint);
}
</style>
