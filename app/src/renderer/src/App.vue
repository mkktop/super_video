<script setup lang="ts">
import {
  NConfigProvider,
  NDialogProvider,
  NGlobalStyle,
  NMessageProvider,
  darkTheme,
  dateZhCN,
  zhCN,
  type GlobalThemeOverrides,
} from 'naive-ui'
import { computed, defineAsyncComponent, onMounted, onUnmounted, type Component } from 'vue'
import { initStore, retryInit, store, ui } from './store'
import TitleBar from './components/TitleBar.vue'
import Sidebar from './components/Sidebar.vue'
// KeepAlive 常驻页保持静态导入：KeepAlive 对首次渲染时还未 resolve 的 async
// 包装组件不匹配 include（取 __asyncResolved.name），首切草稿会丢
import NewTask from './pages/NewTask.vue'
import Trim from './pages/Trim.vue'
import ImageSR from './pages/ImageSR.vue'
import CompareModels from './pages/CompareModels.vue'

/** 即挂即卸的页面异步加载：首屏不解析，首次切入才拉自己的 chunk */
function page(loader: () => Promise<Component>): Component {
  return defineAsyncComponent(loader)
}

/** 页面名 → 组件（动态 <component :is> 是 KeepAlive+Transition 的正确组合方式：
 *  v-if 链在 KeepAlive 内切换时外层 Transition 感知不到） */
const PAGES: Record<string, Component> = {
  newtask: NewTask,
  home: page(() => import('./pages/Home.vue')),
  trim: Trim,
  imagesr: ImageSR,
  mcompare: CompareModels,
  tasks: page(() => import('./pages/Tasks.vue')),
  models: page(() => import('./pages/Models.vue')),
  perf: page(() => import('./pages/Perf.vue')),
  logs: page(() => import('./pages/Logs.vue')),
  compare: page(() => import('./pages/Compare.vue')),
  settings: page(() => import('./pages/Settings.vue')),
}
const pageComp = computed(() => PAGES[ui.page] ?? PAGES.home)

const themeOverrides: GlobalThemeOverrides = {
  common: {
    bodyColor: '#141517',
    cardColor: '#1E2023',
    modalColor: '#1E2023',
    popoverColor: '#26282C',
    primaryColor: '#4F8CFF',
    primaryColorHover: '#6FA0FF',
    primaryColorPressed: '#3B78E8',
    successColor: '#34D399',
    warningColor: '#FBBF24',
    errorColor: '#F87171',
    borderColor: '#2A2D31',
    dividerColor: '#2A2D31',
    borderRadius: '8px',
    fontSizeMedium: '14px',
  },
}

onMounted(() => {
  initStore().catch((e) => {
    store.initError = String(e)
  })
  // 主进程请求的页面跳转（任务完成通知点击 → 任务页）
  offNavigate = window.sv.onNavigate((page) => {
    if (page === 'tasks') ui.page = 'tasks'
  })
})
let offNavigate: (() => void) | null = null
onUnmounted(() => offNavigate?.())
</script>

<template>
  <n-config-provider
    :theme="darkTheme"
    :theme-overrides="themeOverrides"
    :locale="zhCN"
    :date-locale="dateZhCN"
  >
    <n-global-style />
    <n-message-provider>
      <n-dialog-provider>
        <div class="app">
        <TitleBar />
        <div class="body">
          <Sidebar v-if="ui.page !== 'compare'" />
          <main class="page" :class="{ 'page-full': ui.page === 'compare' }">
            <!-- 后端初始化失败：给出原因与重试入口，替代无限 loading -->
            <div v-if="store.initError" class="init-error">
              <div class="init-error-title">后端服务连接失败</div>
              <div class="init-error-detail">{{ store.initError }}</div>
              <n-button type="primary" size="small" @click="retryInit">重试</n-button>
            </div>
            <!-- 表单/作业页 KeepAlive 常驻（NewTask/Trim/ImageSR/CompareModels）：
                 填一半切页草稿不丢、剪切/对比进行中切页回来结果还在；
                 其余页面照常即挂即卸。常驻页的全局监听须配 onActivated/onDeactivated 守卫 -->
            <Transition name="page" mode="out-in">
              <KeepAlive :include="['NewTask', 'Trim', 'ImageSR', 'CompareModels']">
                <component :is="pageComp" />
              </KeepAlive>
            </Transition>
          </main>
        </div>
      </div>
      </n-dialog-provider>
    </n-message-provider>
  </n-config-provider>
</template>

<style>
/* ---- 设计 token：页面/组件样式统一引用（存量 scoped hex 逐轮迁移至此） ---- */
:root {
  --sv-bg: #141517;        /* 应用背景 */
  --sv-panel: #1e2023;     /* 卡片/面板 */
  --sv-panel-deep: #0d0e10;/* 舞台/画布 */
  --sv-border: #2a2d31;    /* 描边 */
  --sv-text: #e8eaed;      /* 主文字 */
  --sv-text-dim: #9aa0a6;  /* 次文字 */
  --sv-text-faint: #7c838c;
  --sv-accent: #4f8cff;    /* 主色 */
  --sv-danger: #f87171;
  --sv-radius: 8px;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body, #app { height: 100%; overflow: hidden; }
body {
  font-family: system-ui, 'Segoe UI', 'Microsoft YaHei', sans-serif;
  background: var(--sv-bg);
  color: var(--sv-text);
  -webkit-font-smoothing: antialiased;
}
.app { display: flex; flex-direction: column; height: 100vh; }
.body { display: flex; flex: 1; min-height: 0; }
.page {
  flex: 1;
  overflow-y: auto;
  padding: 22px 26px;
  min-width: 0;
}
.page-full {
  padding: 12px 14px;
  overflow: hidden;
  display: flex;
}

/* ---- 页面切换过渡：淡入淡出（KeepAlive 缓存命中同样触发进入动画） ---- */
.page-enter-active { transition: opacity 0.16s ease; }
.page-leave-active { transition: opacity 0.1s ease; }
.page-enter-from, .page-leave-to { opacity: 0; }

/* ---- 通用骨架占位（shimmer）：NewTask probe 卡等加载态用 ---- */
.sv-skeleton {
  position: relative;
  overflow: hidden;
  background: #1d1f22;
  border-radius: 6px;
}
.sv-skeleton::after {
  content: '';
  position: absolute;
  inset: 0;
  transform: translateX(-100%);
  background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.055), transparent);
  animation: sv-shimmer 1.3s infinite;
}
@keyframes sv-shimmer { 100% { transform: translateX(100%); } }

.init-error {
  max-width: 560px;
  margin: 60px auto;
  padding: 24px;
  border: 1px solid #5c3a3a;
  border-radius: 8px;
  background: #241a1a;
  text-align: center;
}
.init-error-title { font-size: 16px; font-weight: 600; margin-bottom: 10px; }
.init-error-detail {
  font-size: 12px;
  color: #b8bcc2;
  margin-bottom: 16px;
  word-break: break-all;
  white-space: pre-wrap;
}
::-webkit-scrollbar { width: 10px; }
::-webkit-scrollbar-thumb { background: #33363b; border-radius: 5px; }
::-webkit-scrollbar-track { background: transparent; }
</style>
