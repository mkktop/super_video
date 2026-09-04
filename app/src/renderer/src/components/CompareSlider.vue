<script setup lang="ts">
import { computed, onActivated, onBeforeUnmount, onDeactivated, onMounted, onUnmounted, ref } from 'vue'

const props = withDefaults(
  defineProps<{ srcUrl: string; outUrl: string; labelLeft?: string; labelRight?: string }>(),
  { labelLeft: '处理前', labelRight: '处理后' },
)
const pos = ref(50)
const root = ref<HTMLElement | null>(null)
const outImg = ref<HTMLImageElement | null>(null)

function setFromX(clientX: number) {
  const rect = root.value?.getBoundingClientRect()
  if (!rect || rect.width === 0) return
  pos.value = Math.min(100, Math.max(0, ((clientX - rect.left) / rect.width) * 100))
}

function onDown(e: MouseEvent) {
  e.preventDefault()
  setFromX(e.clientX)
  const move = (ev: MouseEvent) => setFromX(ev.clientX)
  const up = () => {
    window.removeEventListener('mousemove', move)
    window.removeEventListener('mouseup', up)
  }
  window.addEventListener('mousemove', move)
  window.addEventListener('mouseup', up)
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

// ---- 局部放大镜：悬浮跟随光标的放大视窗，左右内容与主分割线同步 ----
// 两层图 object-fit:contain，真实显示区可能 letterbox——必须先把光标位置
// 换算到图片像素坐标（contain 逆映射），放大视窗才对得上像素。
const LOUPE = 200 // 视窗边长（px）
const loupeOn = ref(false)
const zoom = ref(3)
const cursor = ref<{ x: number; y: number } | null>(null) // 图片像素坐标
const natVer = ref(0) // 图片加载完成计数：naturalWidth 非响应式，靠 bump 触发 fit 重算

/** contain 适配参数：图片自然尺寸 → 组件内显示矩形（offX/offY/scale） */
const fit = computed(() => {
  void natVer.value
  const el = root.value
  const img = outImg.value
  if (!el || !img || !img.naturalWidth) return null
  const r = el.getBoundingClientRect()
  const s = Math.min(r.width / img.naturalWidth, r.height / img.naturalHeight)
  if (s <= 0) return null
  return {
    scale: s,
    offX: (r.width - img.naturalWidth * s) / 2,
    offY: (r.height - img.naturalHeight * s) / 2,
    w: img.naturalWidth,
    h: img.naturalHeight,
    rect: r,
  }
})

/** 放大视窗内的分割比例：主分割线（组件宽度百分比）换算到图片显示宽度上 */
const loupeSplit = computed(() => {
  const f = fit.value
  if (!f) return 0.5
  const dispW = f.w * f.scale
  if (dispW <= 0) return 0.5
  const frac = ((pos.value / 100) * f.rect.width - f.offX) / dispW
  return Math.min(1, Math.max(0, frac))
})

function onMove(e: MouseEvent) {
  if (!loupeOn.value) return
  const f = fit.value
  if (!f) return
  const cx = e.clientX - f.rect.left
  const cy = e.clientY - f.rect.top
  // 只在图片显示区内激活（letterbox 黑边上不显示，避免误导性放大空白）
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
  if (!loupeOn.value) return
  e.preventDefault()
  zoom.value = Math.min(8, Math.max(1.5, zoom.value * (e.deltaY < 0 ? 1.25 : 0.8)))
}

/** 放大视窗定位：光标右下角，贴边翻转 */
const loupeStyle = computed(() => {
  const f = fit.value
  const c = cursor.value
  if (!f || !c) return null
  // 图片坐标 → 组件坐标（contain 正映射）
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
  const z = zoom.value
  const tx = LOUPE / 2 - c.x * z
  const ty = LOUPE / 2 - c.y * z
  return `translate(${tx}px, ${ty}px) scale(${z})`
}
</script>

<template>
  <div
    ref="root"
    class="compare"
    :class="{ louping: loupeOn }"
    @mousedown="onDown"
    @mousemove="onMove"
    @mouseleave="cursor = null"
    @wheel.prevent="onWheel"
  >
    <img ref="outImg" class="img" :src="props.outUrl" draggable="false" @load="natVer++" />
    <img
      class="img top"
      :src="props.srcUrl"
      draggable="false"
      :style="{ clipPath: `inset(0 ${100 - pos}% 0 0)` }"
    />
    <div class="handle" :style="{ left: pos + '%' }">
      <div class="line" />
      <div class="knob">⇄</div>
    </div>
    <span class="label label-l">{{ props.labelLeft }}</span>
    <span class="label label-r">{{ props.labelRight }}</span>
    <span class="hint">拖动分割线 · ←/→ 微调（Shift 大步）</span>
    <button class="loupe-btn" :class="{ on: loupeOn }" @mousedown.stop @click="loupeOn = !loupeOn">
      🔍 放大镜
    </button>
    <div v-if="loupeOn && loupeStyle" class="loupe" :style="loupeStyle">
      <img class="loupe-img" :src="props.outUrl" draggable="false" :style="{ transform: loupeTransform() }" />
      <img
        class="loupe-img top"
        :src="props.srcUrl"
        draggable="false"
        :style="{ transform: loupeTransform(), clipPath: `inset(0 ${(1 - loupeSplit) * 100}% 0 0)` }"
      />
      <div class="loupe-split" :style="{ left: `${loupeSplit * 100}%` }" />
      <span class="loupe-zoom">{{ zoom.toFixed(1).replace(/\.0$/, '') }}×</span>
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
  cursor: ew-resize;
  background: var(--sv-panel-deep);
  border-radius: 10px;
}
.compare.louping { cursor: crosshair; }
/* 两层同尺寸 contain，clip-path 裁剪——像素级对齐，letterbox 也一致 */
.img {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: contain;
  pointer-events: none;
}
.handle {
  position: absolute;
  top: 0;
  bottom: 0;
  width: 0;
  pointer-events: none;
}
.line {
  position: absolute;
  top: 0;
  bottom: 0;
  left: -1px;
  width: 2px;
  background: linear-gradient(180deg, transparent, #6fa0ff 12%, #c4d5ff 50%, #6fa0ff 88%, transparent);
  box-shadow: 0 0 10px rgba(79, 140, 255, 0.8);
}
.knob {
  position: absolute;
  top: 50%;
  left: -16px;
  width: 32px;
  height: 32px;
  margin-top: -16px;
  border-radius: 50%;
  background: linear-gradient(135deg, #4f8cff, #7d5cf0);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.5), 0 0 14px rgba(79, 140, 255, 0.5), inset 0 1px 0 rgba(255, 255, 255, 0.25);
}
.label {
  position: absolute;
  top: 10px;
  font-size: 12px;
  padding: 3px 10px;
  border-radius: 12px;
  background: rgba(0, 0, 0, 0.55);
  color: #e8eaed;
  pointer-events: none;
}
.label-l { left: 10px; }
.label-r { right: 10px; }
.hint {
  position: absolute;
  bottom: 10px;
  right: 10px;
  font-size: 11.5px;
  padding: 3px 10px;
  border-radius: 12px;
  background: rgba(0, 0, 0, 0.55);
  color: #9aa0a6;
  pointer-events: none;
}
.loupe-btn {
  position: absolute;
  bottom: 10px;
  left: 10px;
  padding: 3px 12px;
  border-radius: 12px;
  border: 1px solid #33373d;
  background: rgba(0, 0, 0, 0.55);
  color: #9aa0a6;
  font-size: 11.5px;
  cursor: pointer;
  transition: color 0.15s, border-color 0.15s;
}
.loupe-btn:hover { color: #e8eaed; }
.loupe-btn.on {
  color: #4f8cff;
  border-color: rgba(79, 140, 255, 0.6);
  background: rgba(79, 140, 255, 0.12);
}
/* 放大视窗：双层图按图片像素坐标平移缩放，分割比例与主视图同步 */
.loupe {
  position: absolute;
  overflow: hidden;
  border: 2px solid #4f8cff;
  border-radius: 6px;
  background: #0d0e10;
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
  background: #4f8cff;
  box-shadow: 0 0 6px rgba(79, 140, 255, 0.8);
}
.loupe-zoom {
  position: absolute;
  bottom: 6px;
  right: 8px;
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 10px;
  background: rgba(0, 0, 0, 0.6);
  color: #9aa0a6;
}
</style>
