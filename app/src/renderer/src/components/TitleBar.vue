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
      <span class="mark">
        <img class="logo" :src="logoUrl" alt="" draggable="false" />
        <!-- 启动微光扫过：软件醒来的仪式感，只播一次 -->
        <span class="mark-sheen" aria-hidden="true" />
      </span>
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
  background: var(--sv-titlebar-grad);
  border-bottom: 1px solid var(--sv-titlebar-line);
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
  background: linear-gradient(90deg, transparent 8%, rgba(var(--sv-accent-rgb), 0.4) 38%, rgba(var(--sv-accent2-rgb), 0.32) 62%, transparent 92%);
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
  position: relative;
  overflow: hidden;
}
.logo {
  display: block;
  width: 100%;
  height: 100%;
  border-radius: inherit;
}
/* 启动微光：一道高光斜面从左扫到右，600ms 一次性 */
.mark-sheen {
  position: absolute;
  inset: 0;
  border-radius: inherit;
  background: linear-gradient(115deg, transparent 30%, rgba(255, 255, 255, 0.55) 48%, transparent 62%);
  transform: translateX(-130%);
  pointer-events: none;
}
@media (prefers-reduced-motion: no-preference) {
  .mark-sheen { animation: mark-sheen 0.6s var(--sv-ease) 0.35s 1 both; }
}
@keyframes mark-sheen {
  from { transform: translateX(-130%); }
  to { transform: translateX(130%); }
}
.name {
  font-size: 13px;
  font-weight: 650;
  letter-spacing: 0.3px;
  background: var(--sv-title-grad);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
}
.ver {
  font-size: 11px;
  font-weight: 500;
  font-variant-numeric: tabular-nums;
  color: var(--sv-text-dim);
  background: var(--sv-fill-3);
  border: 1px solid var(--sv-border-mid);
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
  color: var(--sv-accent-strong);
  background: var(--sv-accent-bg);
  border: 1px solid rgba(var(--sv-accent-rgb), 0.45);
  border-radius: 999px;
  padding: 1px 10px;
  margin-left: 6px;
  cursor: pointer;
  -webkit-app-region: no-drag;
  transition: background var(--sv-dur-fast) ease, color var(--sv-dur-fast) ease, box-shadow var(--sv-dur-fast) ease;
}
.upd:hover {
  background: rgba(var(--sv-accent-rgb), 0.24);
  color: var(--sv-accent-strong);
}
/* 有更新时太容易错过：轻微呼吸辉光提示（box-shadow 2.5s 循环） */
@media (prefers-reduced-motion: no-preference) {
  .upd { animation: upd-breathe 2.5s ease-in-out infinite; }
}
@keyframes upd-breathe {
  0%, 100% { box-shadow: 0 0 0 0 rgba(var(--sv-accent-rgb), 0); }
  50% { box-shadow: 0 0 12px 1px rgba(var(--sv-accent-rgb), 0.45); }
}
.upd-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--sv-accent);
  box-shadow: 0 0 6px rgba(var(--sv-accent-rgb), 0.9);
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
  color: var(--sv-ctl-fg);
  transition: background 0.1s ease, color 0.1s ease;
}
.ctl:hover {
  background: var(--sv-ctl-hover);
  color: var(--sv-ctl-fg-hover);
}
/* Windows 惯例：关闭键 hover 红底白图标 */
.ctl.close:hover {
  background: var(--sv-ctl-close);
  color: #fff;
}
</style>
