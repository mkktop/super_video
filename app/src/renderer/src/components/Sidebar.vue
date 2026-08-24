<script setup lang="ts">
import { computed } from 'vue'
import { store, ui } from '../store'

const items = computed(() => [
  { key: 'home', label: '首页', icon: 'home' },
  { key: 'trim', label: '视频剪切', icon: 'cut' },
  {
    key: 'tasks',
    label: '任务',
    icon: 'tasks',
    badge: store.tasks.filter((t) => t.status === 'running' || t.status === 'queued').length,
  },
  { key: 'models', label: '模型市场', icon: 'cube' },
  { key: 'perf', label: '性能', icon: 'pulse' },
  { key: 'logs', label: '日志', icon: 'log' },
  { key: 'settings', label: '设置', icon: 'gear' },
])

// 侧栏底部迷你占用指示:任何页面都能瞄一眼当前负载
const miniPerf = computed(() => {
  const l = store.perf.latest
  if (!l) return ''
  const gpu = l.gpus?.[0]?.util
  return gpu == null
    ? `CPU ${Math.round(l.cpu)}% · 内存 ${Math.round(l.mem_pct)}%`
    : `CPU ${Math.round(l.cpu)}% · GPU ${gpu}%`
})
</script>

<template>
  <aside class="sidebar">
    <nav class="nav">
      <button
        v-for="it in items"
        :key="it.key"
        class="nav-item"
        :class="{ active: ui.page === it.key }"
        @click="ui.page = it.key as typeof ui.page"
      >
        <span class="icon">
          <svg v-if="it.icon === 'home'" width="16" height="16" viewBox="0 0 16 16">
            <path d="M2.5 7L8 2.5 13.5 7v6a1 1 0 0 1-1 1h-3v-4h-3v4h-3a1 1 0 0 1-1-1V7z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round" />
          </svg>
          <svg v-else-if="it.icon === 'cut'" width="16" height="16" viewBox="0 0 16 16">
            <circle cx="4" cy="12" r="1.9" fill="none" stroke="currentColor" stroke-width="1.3" />
            <circle cx="12" cy="12" r="1.9" fill="none" stroke="currentColor" stroke-width="1.3" />
            <path d="M5.4 10.6L12 2M10.6 10.6L4 2" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" />
          </svg>
          <svg v-else-if="it.icon === 'tasks'" width="16" height="16" viewBox="0 0 16 16">
            <path d="M5.5 3.5h8M5.5 8h8M5.5 12.5h8M2.5 3.5h.01M2.5 8h.01M2.5 12.5h.01" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
          </svg>
          <svg v-else-if="it.icon === 'cube'" width="16" height="16" viewBox="0 0 16 16">
            <path d="M8 1.8l5.5 3v6.4L8 14.2 2.5 11.2V4.8L8 1.8zM2.5 4.8L8 7.8l5.5-3M8 7.8v6.4" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round" />
          </svg>
          <svg v-else-if="it.icon === 'pulse'" width="16" height="16" viewBox="0 0 16 16">
            <path d="M1.5 8h3L6.5 3l3 10 2-5h3" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
          <svg v-else-if="it.icon === 'log'" width="16" height="16" viewBox="0 0 16 16">
            <rect x="2" y="2.5" width="12" height="11" rx="1.6" fill="none" stroke="currentColor" stroke-width="1.2" />
            <path d="M4.5 6l1.8 1.5L4.5 9M7.8 9.5h3.4" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
          <svg v-else width="16" height="16" viewBox="0 0 16 16">
            <circle cx="8" cy="8" r="2.2" fill="none" stroke="currentColor" stroke-width="1.3" />
            <path d="M8 1.5v2M8 12.5v2M1.5 8h2M12.5 8h2M3.4 3.4l1.4 1.4M11.2 11.2l1.4 1.4M12.6 3.4l-1.4 1.4M4.8 11.2l-1.4 1.4" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" />
          </svg>
        </span>
        <span class="label">{{ it.label }}</span>
        <span v-if="it.badge" class="badge">{{ it.badge }}</span>
      </button>
    </nav>
    <div class="foot-col">
      <div v-if="miniPerf" class="foot-perf">{{ miniPerf }}</div>
      <div class="foot">
        <span class="dot" :class="store.connected ? 'on' : 'off'" />
        <span class="foot-text">{{ store.connected ? '后端已连接' : '连接中断' }}</span>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.sidebar {
  width: 190px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  background: #17191d;
  border-right: 1px solid #232629;
  padding: 14px 10px;
}
.nav {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 12px;
  border: none;
  border-radius: 8px;
  background: transparent;
  color: #9aa0a6;
  font-size: 13.5px;
  cursor: pointer;
  position: relative;
  transition: background 0.15s, color 0.15s;
  text-align: left;
}
.nav-item:hover:not(:disabled) {
  background: #1f2328;
  color: #e8eaed;
}
.nav-item.active {
  background: linear-gradient(90deg, rgba(79, 140, 255, 0.16), rgba(139, 92, 246, 0.08));
  color: #fff;
}
.nav-item.active::before {
  content: '';
  position: absolute;
  left: 0;
  top: 22%;
  height: 56%;
  width: 3px;
  border-radius: 2px;
  background: linear-gradient(180deg, #4f8cff, #8b5cf6);
}
.nav-item:disabled {
  opacity: 0.42;
  cursor: default;
}
.icon { display: inline-flex; }
.label { flex: 1; }
.badge {
  background: #4f8cff;
  color: #fff;
  font-size: 11px;
  min-width: 18px;
  height: 18px;
  border-radius: 9px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0 5px;
}
.soon {
  font-size: 10px;
  color: #6b7280;
  border: 1px solid #3a3f45;
  border-radius: 4px;
  padding: 1px 4px;
}
.foot-col {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.foot-perf {
  font-size: 11px;
  color: #6b7280;
  padding: 0 12px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.foot {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 8px 12px;
  font-size: 12px;
  color: #6b7280;
}
.dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
}
.dot.on {
  background: #34d399;
  box-shadow: 0 0 6px rgba(52, 211, 153, 0.8);
}
.dot.off {
  background: #f87171;
}
</style>
