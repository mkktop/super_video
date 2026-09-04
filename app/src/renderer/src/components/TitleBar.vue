<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { store, ui } from '../store'
// 与桌面图标(build/icon.png, B 方案定稿)同源,标题栏保持品牌一致
import logoUrl from '../assets/logo.png'

const maximized = ref(false)
const version = ref('')
let off: (() => void) | null = null

const minimize = () => window.sv.win.minimize()
const toggleMax = () => window.sv.win.toggleMaximize()
const close = () => window.sv.win.close()

// 启动检查发现新版本 → 版本号旁常驻提示,点击去设置页处理
const hasUpdate = computed(() => store.update.status === 'available')

onMounted(async () => {
  off = window.sv.win.onMaximized((m) => (maximized.value = m))
  version.value = await window.sv.appVersion()
})
onUnmounted(() => off?.())
</script>

<template>
  <div class="titlebar" @dblclick="toggleMax">
    <div class="brand">
      <span class="mark"><img class="logo" :src="logoUrl" alt="" draggable="false" /></span>
      <span class="name">super_video</span>
      <span class="ver">v{{ version }}</span>
      <button
        v-if="hasUpdate"
        class="upd"
        title="发现新版本,点击前往设置页下载"
        @click="ui.page = 'settings'"
      >
        <span class="upd-dot" />
        v{{ store.update.version }} 可更新
      </button>
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
  background: linear-gradient(180deg, rgba(30, 34, 42, 0.92), rgba(22, 25, 31, 0.96));
  border-bottom: 1px solid rgba(255, 255, 255, 0.055);
  position: relative;
  -webkit-app-region: drag;
  user-select: none;
  flex-shrink: 0;
}
/* 底缘品牌流光：极低饱和，只给一条 1px 的呼吸感 */
.titlebar::after {
  content: '';
  position: absolute;
  left: 0;
  right: 0;
  bottom: -1px;
  height: 1px;
  background: linear-gradient(90deg, transparent 8%, rgba(79, 140, 255, 0.4) 38%, rgba(139, 92, 246, 0.32) 62%, transparent 92%);
  opacity: 0.55;
  pointer-events: none;
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
  width: 23px;
  height: 23px;
  border-radius: 7px;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.35);
}
.logo {
  display: block;
  width: 100%;
  height: 100%;
  border-radius: inherit;
}
.name {
  font-size: 13px;
  font-weight: 650;
  letter-spacing: 0.3px;
  background: linear-gradient(90deg, #f2f4f7, #a8adb5);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
}
.ver {
  font-size: 11px;
  font-weight: 500;
  font-variant-numeric: tabular-nums;
  color: #8f959d;
  background: rgba(255, 255, 255, 0.055);
  border: 1px solid rgba(255, 255, 255, 0.09);
  border-radius: 999px;
  padding: 1px 9px;
  margin-left: 2px;
  letter-spacing: 0.2px;
  line-height: 1.5;
}
.upd {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: #4f8cff;
  background: rgba(79, 140, 255, 0.12);
  border: 1px solid rgba(79, 140, 255, 0.45);
  border-radius: 999px;
  padding: 1px 10px;
  margin-left: 6px;
  cursor: pointer;
  -webkit-app-region: no-drag;
  transition: background 0.15s, color 0.15s;
}
.upd:hover {
  background: rgba(79, 140, 255, 0.24);
  color: #6fa0ff;
}
.upd-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #4f8cff;
  box-shadow: 0 0 6px rgba(79, 140, 255, 0.9);
  animation: upd-pulse 2s ease-in-out infinite;
}
@keyframes upd-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.35; }
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
