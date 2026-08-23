<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'

const maximized = ref(false)
let off: (() => void) | null = null

const minimize = () => window.sv.win.minimize()
const toggleMax = () => window.sv.win.toggleMaximize()
const close = () => window.sv.win.close()

onMounted(() => {
  off = window.sv.win.onMaximized((m) => (maximized.value = m))
})
onUnmounted(() => off?.())
</script>

<template>
  <div class="titlebar" @dblclick="toggleMax">
    <div class="brand">
      <span class="mark">⬆</span>
      <span class="name">super_video</span>
    </div>
    <div class="controls">
      <button class="ctl" title="最小化" @click="minimize">
        <svg width="11" height="11" viewBox="0 0 11 11"><path d="M1 5.5h9" stroke="currentColor" stroke-width="1.2" /></svg>
      </button>
      <button class="ctl" title="最大化" @click="toggleMax">
        <svg v-if="!maximized" width="11" height="11" viewBox="0 0 11 11">
          <rect x="1.5" y="1.5" width="8" height="8" fill="none" stroke="currentColor" stroke-width="1.2" />
        </svg>
        <svg v-else width="11" height="11" viewBox="0 0 11 11">
          <rect x="1.5" y="3.2" width="6.3" height="6.3" fill="none" stroke="currentColor" stroke-width="1.2" />
          <path d="M3.2 3.2V1.5h6.3v6.3H7.8" fill="none" stroke="currentColor" stroke-width="1.2" />
        </svg>
      </button>
      <button class="ctl close" title="关闭" @click="close">
        <svg width="11" height="11" viewBox="0 0 11 11">
          <path d="M1.5 1.5l8 8M9.5 1.5l-8 8" stroke="currentColor" stroke-width="1.2" />
        </svg>
      </button>
    </div>
  </div>
</template>

<style scoped>
.titlebar {
  height: 40px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: linear-gradient(180deg, #191c20, #16181c);
  border-bottom: 1px solid #24272c;
  -webkit-app-region: drag;
  user-select: none;
  flex-shrink: 0;
}
.brand {
  display: flex;
  align-items: center;
  gap: 8px;
  padding-left: 14px;
}
.mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  border-radius: 6px;
  font-size: 12px;
  background: linear-gradient(135deg, #4f8cff, #8b5cf6);
  color: #fff;
}
.name {
  font-size: 13px;
  font-weight: 600;
  letter-spacing: 0.4px;
  background: linear-gradient(90deg, #e8eaed, #9aa0a6);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
}
.controls {
  display: flex;
  height: 100%;
  -webkit-app-region: no-drag;
}
.ctl {
  width: 44px;
  height: 100%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  color: #9aa0a6;
  transition: background 0.12s, color 0.12s;
}
.ctl:hover {
  background: #26292e;
  color: #e8eaed;
}
.ctl.close:hover {
  background: #e81123;
  color: #fff;
}
</style>
