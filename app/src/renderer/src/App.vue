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
    // bodyColor 透明：让 App.vue 里带环境光的分层背景透出来
    bodyColor: 'transparent',
    cardColor: '#181b21',
    modalColor: '#1b1f26',
    popoverColor: '#22262e',
    inputColor: 'rgba(255, 255, 255, 0.045)',
    actionColor: '#20242b',
    hoverColor: 'rgba(255, 255, 255, 0.06)',
    primaryColor: '#4F8CFF',
    primaryColorHover: '#6FA0FF',
    primaryColorPressed: '#3F7EF2',
    primaryColorSuppl: '#6FA0FF',
    successColor: '#34D399',
    warningColor: '#FBBF24',
    errorColor: '#F87171',
    infoColor: '#4F8CFF',
    borderColor: '#2B2F37',
    dividerColor: 'rgba(255, 255, 255, 0.07)',
    borderRadius: '10px',
    borderRadiusSmall: '8px',
    fontSizeMedium: '14px',
    textColorBase: '#E9ECF2',
    textColor1: '#E9ECF2',
    textColor2: '#C3C8D2',
    textColor3: '#8B919D',
  },
  Card: {
    borderRadiusMedium: '14px',
    color: '#181B21',
    borderColor: 'rgba(255, 255, 255, 0.06)',
  },
  Tag: {
    borderRadius: '6px',
  },
  Dialog: {
    borderRadius: '16px',
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
  --sv-bg: #0e1014;         /* 应用底色（环境光在上面几层渐变里） */
  --sv-panel: #181b21;      /* 卡片/面板 */
  --sv-panel-2: #1d2129;    /* 面板悬浮态/次级面板 */
  --sv-panel-deep: #0b0d10; /* 舞台/画布 */
  --sv-border: #2b2f37;     /* 描边 */
  --sv-border-soft: rgba(255, 255, 255, 0.06);
  --sv-text: #e9ecf2;       /* 主文字 */
  --sv-text-dim: #9aa1ad;   /* 次文字 */
  --sv-text-faint: #7c838f;
  --sv-accent: #4f8cff;     /* 主色 */
  --sv-accent-2: #8b5cf6;   /* 品牌渐变第二色 */
  --sv-grad: linear-gradient(135deg, #4f8cff, #8b5cf6);
  --sv-danger: #f87171;
  --sv-success: #34d399;
  --sv-warning: #fbbf24;
  --sv-radius: 12px;
  --sv-shadow-card: 0 8px 28px rgba(0, 0, 0, 0.35);
  --sv-glow: 0 0 24px rgba(79, 140, 255, 0.28);
}
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body, #app { height: 100%; overflow: hidden; }
body {
  font-family: system-ui, 'Segoe UI', 'Microsoft YaHei', sans-serif;
  /* 顶栏到底部极淡的纵向深度，避免大面积径向渐变在低端 GPU 上的 banding */
  background: linear-gradient(180deg, #12151c 0%, var(--sv-bg) 46%);
  color: var(--sv-text);
  -webkit-font-smoothing: antialiased;
}
.app { display: flex; flex-direction: column; height: 100vh; position: relative; z-index: 0; }
.body { display: flex; flex: 1; min-height: 0; }
.page {
  flex: 1;
  overflow-y: auto;
  padding: 24px 28px;
  min-width: 0;
}
.page-full {
  padding: 12px 14px;
  overflow: hidden;
  display: flex;
}

/* ---- 页面切换过渡：淡入 + 轻微上浮（KeepAlive 缓存命中同样触发） ---- */
.page-enter-active { transition: opacity 0.18s ease-out, transform 0.18s ease-out; }
.page-leave-active { transition: opacity 0.12s ease-in; }
.page-enter-from { opacity: 0; transform: translateY(7px); }
.page-leave-to { opacity: 0; }

/* ---- 主按钮：品牌渐变 + 柔光（ghost/secondary/quaternary 变体不受影响） ---- */
.n-button.n-button--primary-type:not(.n-button--ghost):not(.n-button--secondary):not(.n-button--tertiary):not(.n-button--quaternary) {
  background: linear-gradient(135deg, #4f8cff, #7d5cf0);
  border: 1px solid transparent;
  box-shadow: 0 2px 12px rgba(79, 140, 255, 0.32), inset 0 1px 0 rgba(255, 255, 255, 0.14);
  color: #fff;
}
.n-button.n-button--primary-type:not(.n-button--ghost):not(.n-button--secondary):not(.n-button--tertiary):not(.n-button--quaternary):not(:disabled):hover {
  background: linear-gradient(135deg, #6199ff, #8d70f4);
  box-shadow: 0 4px 18px rgba(79, 140, 255, 0.42), inset 0 1px 0 rgba(255, 255, 255, 0.16);
}
.n-button.n-button--primary-type:not(.n-button--ghost):not(.n-button--secondary):not(.n-button--tertiary):not(.n-button--quaternary):not(:disabled):active {
  background: linear-gradient(135deg, #407ef2, #7050e6);
}

/* 键盘可达性：焦点环 */
button:focus-visible, .n-button:focus-visible {
  outline: 2px solid rgba(79, 140, 255, 0.65);
  outline-offset: 2px;
}
::selection { background: rgba(79, 140, 255, 0.35); }

/* ---- 通用骨架占位（shimmer）：NewTask probe 卡等加载态用 ---- */
.sv-skeleton {
  position: relative;
  overflow: hidden;
  background: #1d2027;
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
  border-radius: 12px;
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
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-thumb {
  background: #2e333d;
  border-radius: 6px;
  border: 2px solid transparent;
  background-clip: padding-box;
}
::-webkit-scrollbar-thumb:hover { background: #3d4450; background-clip: padding-box; }
::-webkit-scrollbar-track { background: transparent; }
</style>
