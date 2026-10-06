<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { store, ui } from '../store'
import { versionParts } from '../utils'
// 与桌面图标(build/icon.png, B 方案定稿)同源,标题栏保持品牌一致
import logoUrl from '../assets/logo.png'

const maximized = ref(false)
const version = ref('')
let off: (() => void) | null = null

// 当前版本保持低调；完整版本号通过悬停提示查看。
const ver = computed(() => versionParts(version.value))

const minimize = () => window.sv.win.minimize()
const toggleMax = () => window.sv.win.toggleMaximize()
const close = () => window.sv.win.close()

// 更新状态只突出需要关注的操作，点击统一进入设置页。
const updateHint = computed(() => {
  const u = store.update
  if (u.ready) return { text: '更新已就绪', title: `v${u.ready} 已下载，前往设置安装更新`, ready: true }
  if (u.downloading) return { text: `下载中 ${Math.floor(u.percent)}%`, title: '正在下载更新，前往设置查看进度', ready: false }
  if (u.status === 'available') return { text: '发现新版本', title: `可更新至 v${u.version}，前往设置查看更新`, ready: false }
  return null
})

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
      <span class="name">雨帧</span>
      <button
        v-if="version"
        class="version"
        :title="`当前版本 v${version} · 查看版本与更新`"
        :aria-label="`当前版本 v${version}，查看版本与更新`"
        @click="ui.page = 'settings'"
        @dblclick.stop
      >
        <span class="version-number"><span class="version-prefix">v</span><span>{{ ver.base }}</span></span>
        <span v-if="ver.pre" class="version-channel">{{ ver.pre }}</span>
      </button>
      <button
        v-if="updateHint"
        class="update-hint"
        :class="{ ready: updateHint.ready }"
        :title="updateHint.title"
        :aria-label="updateHint.title"
        @click="ui.page = 'settings'"
        @dblclick.stop
      >
        <svg width="12" height="12" viewBox="0 0 16 16" fill="none" aria-hidden="true">
          <path d="M8 11V3m0 0L5 6m3-3 3 3M3 10v3h10v-3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
        <span>{{ updateHint.text }}</span>
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
/* 版本为次级信息；更新提示使用独立的静态强调色。 */
.version, .update-hint {
  display: inline-flex;
  align-items: center;
  flex-shrink: 0;
  gap: 7px;
  height: 24px;
  padding: 0 8px;
  border: 1px solid transparent;
  border-radius: 6px;
  font: inherit;
  font-size: 11px;
  line-height: 1;
  font-variant-numeric: tabular-nums;
  cursor: pointer;
  -webkit-app-region: no-drag;
  transition: background var(--sv-dur-fast) ease, border-color var(--sv-dur-fast) ease, color var(--sv-dur-fast) ease;
}
.version {
  margin-left: 2px;
  color: var(--sv-text-dim);
  background: var(--sv-fill-1);
  border-color: var(--sv-border-soft);
}
.version-number { display: inline-flex; align-items: baseline; gap: 3px; font-weight: 500; letter-spacing: 0.2px; }
.version-prefix { font-size: 10px; }
.version-channel {
  color: var(--sv-warning);
  border-left: 1px solid var(--sv-border);
  padding-left: 7px;
  font-size: 10px;
}
.version:hover { color: var(--sv-text); background: var(--sv-fill-3); }
.update-hint {
  color: var(--sv-accent-strong);
  background: var(--sv-accent-bg);
  font-weight: 500;
}
.update-hint:hover { border-color: rgba(var(--sv-accent-rgb), 0.4); }
.update-hint.ready { color: var(--sv-success); background: var(--sv-success-bg); }
.version:focus-visible, .update-hint:focus-visible {
  outline: 2px solid var(--sv-accent);
  outline-offset: 2px;
}
.brand { min-width: 0; }
.controls { flex-shrink: 0; }
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
