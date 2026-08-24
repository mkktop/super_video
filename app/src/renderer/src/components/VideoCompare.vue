<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { NButton, NSlider } from 'naive-ui'
import { mediaSrc } from '../api'

const props = defineProps<{ srcPath: string; outPath: string }>()

const srcUrl = mediaSrc(props.srcPath)
const outUrl = mediaSrc(props.outPath)

const srcV = ref<HTMLVideoElement | null>(null)
const outV = ref<HTMLVideoElement | null>(null)
const root = ref<HTMLElement | null>(null)

const pos = ref(50) // 分割线位置 %
const playing = ref(false)
const cur = ref(0)
const dur = ref(0)
const srcBroken = ref(false)
const outBroken = ref(false)

const fmt = (t: number) => {
  const m = Math.floor(t / 60)
  const s = Math.floor(t % 60)
  return `${m}:${String(s).padStart(2, '0')}`
}

// 以输出视频为主时钟，rAF 持续把源视频对齐（两路时长可能差 ±1 帧）
let raf = 0
let lastCur = -1
function tick() {
  const s = srcV.value
  const o = outV.value
  if (s && o) {
    if (Math.abs(s.currentTime - o.currentTime) > 0.05 && !s.seeking) s.currentTime = o.currentTime
    if (!o.paused && s.paused && !srcBroken.value) void s.play().catch(() => {})
    if (o.paused && !s.paused) s.pause()
    playing.value = !o.paused
    if (Math.abs(o.currentTime - lastCur) > 0.05) {
      lastCur = o.currentTime
      cur.value = o.currentTime
    }
  }
  raf = requestAnimationFrame(tick)
}

function toggle() {
  const s = srcV.value
  const o = outV.value
  if (!o) return
  if (o.paused) {
    void o.play().catch(() => {})
    if (s) void s.play().catch(() => {})
  } else {
    o.pause()
    s?.pause()
  }
}

function seek(v: number) {
  cur.value = v
  lastCur = v
  if (outV.value) outV.value.currentTime = v
  if (srcV.value) srcV.value.currentTime = v
}

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

onMounted(() => {
  window.addEventListener('keydown', onKey)
  raf = requestAnimationFrame(tick)
})
onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKey)
  cancelAnimationFrame(raf)
})
</script>

<template>
  <div class="vcompare">
    <div ref="root" class="stage" @mousedown="onDown">
      <video
        ref="outV"
        class="video"
        :src="outUrl"
        muted
        loop
        playsinline
        preload="auto"
        @loadedmetadata="dur = outV?.duration ?? 0"
        @error="outBroken = true"
      />
      <video
        ref="srcV"
        class="video top"
        :src="srcUrl"
        muted
        loop
        playsinline
        preload="auto"
        :style="{ clipPath: `inset(0 ${100 - pos}% 0 0)` }"
        @error="srcBroken = true"
      />
      <div class="handle" :style="{ left: pos + '%' }">
        <div class="line" />
        <div class="knob">⇄</div>
      </div>
      <span class="label label-l">源</span>
      <span class="label label-r">超分</span>
      <div v-if="srcBroken || outBroken" class="broken">
        {{ srcBroken ? '源' : '超分' }}视频的容器/编码浏览器不支持直接播放（如 MKV/AVI/HEVC），请用「静帧」模式对比
      </div>
    </div>
    <div class="bar">
      <NButton size="small" @click="toggle">{{ playing ? '暂停' : '播放' }}</NButton>
      <span class="time">{{ fmt(cur) }} / {{ fmt(dur) }}</span>
      <NSlider
        class="seek"
        :value="cur"
        :min="0"
        :max="Math.max(dur, 0.1)"
        :step="0.01"
        :format-tooltip="(v: number) => fmt(v)"
        :disabled="!dur"
        @update:value="seek"
      />
    </div>
  </div>
</template>

<style scoped>
.vcompare {
  height: 100%;
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-height: 0;
}
.stage {
  position: relative;
  flex: 1;
  min-height: 0;
  overflow: hidden;
  user-select: none;
  cursor: ew-resize;
  background: #0d0e10;
  border-radius: 6px;
}
/* 与静帧对比同构：两层同尺寸 contain，clip-path 裁剪，画面像素级对齐 */
.video {
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
  background: #4f8cff;
  box-shadow: 0 0 8px rgba(79, 140, 255, 0.8);
}
.knob {
  position: absolute;
  top: 50%;
  left: -16px;
  width: 32px;
  height: 32px;
  margin-top: -16px;
  border-radius: 50%;
  background: #4f8cff;
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.5);
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
.broken {
  position: absolute;
  left: 50%;
  bottom: 14px;
  transform: translateX(-50%);
  max-width: 86%;
  font-size: 12px;
  padding: 5px 12px;
  border-radius: 10px;
  background: rgba(120, 70, 0, 0.78);
  color: #fbbf24;
  pointer-events: none;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.bar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
  padding: 0 2px;
}
.time {
  font-size: 12px;
  color: #9aa0a6;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.seek { flex: 1; }
</style>
