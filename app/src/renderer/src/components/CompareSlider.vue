<script setup lang="ts">
import { onActivated, onBeforeUnmount, onDeactivated, onMounted, onUnmounted, ref } from 'vue'

defineProps<{ srcUrl: string; outUrl: string }>()
const pos = ref(50)
const root = ref<HTMLElement | null>(null)

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
</script>

<template>
  <div ref="root" class="compare" @mousedown="onDown">
    <img class="img" :src="outUrl" draggable="false" />
    <img
      class="img top"
      :src="srcUrl"
      draggable="false"
      :style="{ clipPath: `inset(0 ${100 - pos}% 0 0)` }"
    />
    <div class="handle" :style="{ left: pos + '%' }">
      <div class="line" />
      <div class="knob">⇄</div>
    </div>
    <span class="label label-l">处理前</span>
    <span class="label label-r">处理后</span>
    <span class="hint">拖动分割线 · ←/→ 微调（Shift 大步）</span>
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
  background: #0d0e10;
  border-radius: 8px;
}
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
</style>
