<script setup lang="ts">
import { ref } from 'vue'

defineProps<{ srcUrl: string; outUrl: string }>()
const pos = ref(50)

function onDrag(e: MouseEvent) {
  const el = e.currentTarget as HTMLElement
  const move = (ev: MouseEvent) => {
    const rect = el.getBoundingClientRect()
    pos.value = Math.min(100, Math.max(0, ((ev.clientX - rect.left) / rect.width) * 100))
  }
  const up = () => {
    window.removeEventListener('mousemove', move)
    window.removeEventListener('mouseup', up)
  }
  window.addEventListener('mousemove', move)
  window.addEventListener('mouseup', up)
}
</script>

<template>
  <div class="compare" @mousedown="onDrag">
    <img class="img" :src="outUrl" draggable="false" />
    <div class="clip" :style="{ width: pos + '%' }">
      <img class="img" :src="srcUrl" draggable="false" />
    </div>
    <div class="handle" :style="{ left: pos + '%' }">
      <div class="line" />
      <div class="knob">⇄</div>
    </div>
    <span class="label label-l">处理前</span>
    <span class="label label-r">处理后</span>
  </div>
</template>

<style scoped>
.compare {
  position: relative;
  width: 100%;
  border-radius: 8px;
  overflow: hidden;
  border: 1px solid #2a2d31;
  user-select: none;
  cursor: ew-resize;
  background: #0d0e10;
}
.img {
  display: block;
  width: 100%;
  height: auto;
  max-height: 420px;
  object-fit: contain;
}
.clip {
  position: absolute;
  inset: 0;
  overflow: hidden;
}
.clip .img {
  width: auto;
  height: 100%;
  max-width: none;
  object-fit: cover;
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
}
.label-l { left: 10px; }
.label-r { right: 10px; }
</style>
