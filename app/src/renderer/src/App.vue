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
import { themeResolved } from './theme'
import { pageDir } from './uiFx'
import TitleBar from './components/TitleBar.vue'
import Sidebar from './components/Sidebar.vue'
import CommandPalette from './components/CommandPalette.vue'
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

/** 方向感知过渡：向下导航新页从下方浮入，向上则从上方（uiFx.ts 维护方向） */
const pageName = computed(() => (pageDir.value > 0 ? 'page-down' : 'page-up'))

const isLight = computed(() => themeResolved.value === 'light')

// bodyColor 透明：让 App.vue 里带环境光的分层背景透出来
const darkOverrides: GlobalThemeOverrides = {
  common: {
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
const lightOverrides: GlobalThemeOverrides = {
  common: {
    bodyColor: 'transparent',
    cardColor: '#FFFFFF',
    modalColor: '#FFFFFF',
    popoverColor: '#FFFFFF',
    inputColor: 'rgba(16, 24, 40, 0.045)',
    actionColor: '#EEF1F6',
    hoverColor: 'rgba(16, 24, 40, 0.055)',
    primaryColor: '#4F8CFF',
    primaryColorHover: '#6FA0FF',
    primaryColorPressed: '#3F7EF2',
    primaryColorSuppl: '#6FA0FF',
    successColor: '#0FA968',
    warningColor: '#C77F0A',
    errorColor: '#DC4B4B',
    infoColor: '#4F8CFF',
    borderColor: '#DDE2EB',
    dividerColor: 'rgba(16, 24, 40, 0.08)',
    borderRadius: '10px',
    borderRadiusSmall: '8px',
    fontSizeMedium: '14px',
    textColorBase: '#1C2333',
    textColor1: '#1C2333',
    textColor2: '#3D4757',
    textColor3: '#79808C',
  },
  Card: {
    borderRadiusMedium: '14px',
    color: '#FFFFFF',
    borderColor: 'rgba(16, 24, 40, 0.08)',
  },
  Tag: {
    borderRadius: '6px',
  },
  Dialog: {
    borderRadius: '16px',
  },
}
const themeOverrides = computed<GlobalThemeOverrides>(() =>
  isLight.value ? lightOverrides : darkOverrides,
)

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
    :theme="isLight ? null : darkTheme"
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
            <Transition :name="pageName" mode="out-in">
              <KeepAlive :include="['NewTask', 'Trim', 'ImageSR', 'CompareModels']">
                <component :is="pageComp" />
              </KeepAlive>
            </Transition>
          </main>
        </div>
        <CommandPalette />
      </div>
      </n-dialog-provider>
    </n-message-provider>
  </n-config-provider>
</template>

<style>
/* ============================================================
   设计 token：全部颜色经 --sv-* 引用；浅色主题用
   [data-theme='light'] 覆盖同一组 token（theme.ts 写 data-theme）
   ============================================================ */
:root {
  /* —— 基底与面板 —— */
  --sv-bg: #0e1014;            /* 应用底色（环境光在上面几层渐变里） */
  --sv-bg-top: #12151c;        /* 顶部的纵深起点 */
  --sv-panel: #181b21;         /* 卡片/面板 */
  --sv-panel-hi: #1c2027;      /* 面板顶部亮一档（面板纵向渐变用） */
  --sv-panel-2: #1d2129;       /* 面板悬浮态/次级面板 */
  --sv-panel-deep: #0b0d10;    /* 舞台/画布 */
  --sv-panel-grad: linear-gradient(180deg, #1c2027, #181b21);
  --sv-well: rgba(0, 0, 0, 0.28); /* 内嵌深井：代码块/路径框底 */
  /* —— 线与填充（通用半透明，浅色主题翻转为黑系） —— */
  --sv-border: #2b2f37;
  --sv-border-soft: rgba(255, 255, 255, 0.06);
  --sv-border-mid: rgba(255, 255, 255, 0.09);
  --sv-border-strong: rgba(255, 255, 255, 0.12);
  --sv-fill-1: rgba(255, 255, 255, 0.025);
  --sv-fill-2: rgba(255, 255, 255, 0.045);
  --sv-fill-3: rgba(255, 255, 255, 0.06);
  /* —— 文字 —— */
  --sv-text: #e9ecf2;          /* 主文字 */
  --sv-text-dim: #9aa1ad;      /* 次文字 */
  --sv-text-faint: #7c838f;    /* 弱文字/标签 */
  --sv-text-code: #c9cdd6;     /* 等宽代码/日志 */
  /* —— 品牌与状态色 —— */
  --sv-accent: #4f8cff;
  --sv-accent-strong: #6fa0ff; /* 主色亮一档（hover/强调文字） */
  --sv-accent-2: #8b5cf6;      /* 品牌渐变第二色 */
  --sv-accent-2-strong: #a78bfa;
  --sv-grad: linear-gradient(135deg, #4f8cff, #8b5cf6);
  --sv-danger: #f87171;
  --sv-danger-strong: #e88080;
  --sv-success: #34d399;
  --sv-success-strong: #9fd8bd;
  --sv-warning: #fbbf24;
  --sv-warning-deep: #f59e0b;
  /* rgb 三连：rgba(var(--sv-*-rgb), a) 派生任意透明度（浅色主题同样可用） */
  --sv-accent-rgb: 79, 140, 255;
  --sv-accent2-rgb: 139, 92, 246;
  --sv-success-rgb: 52, 211, 153;
  --sv-warning-rgb: 251, 191, 36;
  --sv-danger-rgb: 248, 113, 113;
  /* 状态色柔和背景（tag/徽标底色，约 10% 档） */
  --sv-accent-bg: rgba(79, 140, 255, 0.1);
  --sv-success-bg: rgba(52, 211, 153, 0.1);
  --sv-warning-bg: rgba(251, 191, 36, 0.1);
  --sv-danger-bg: rgba(248, 113, 113, 0.1);
  /* —— 主按钮渐变（品牌渐变的按压层次） —— */
  --sv-btn-grad: linear-gradient(135deg, #4f8cff, #7d5cf0);
  --sv-btn-grad-hover: linear-gradient(135deg, #6199ff, #8d70f4);
  --sv-btn-grad-active: linear-gradient(135deg, #407ef2, #7050e6);
  /* —— 沉浸式面板画布（Hero/硬件区）：品牌氛围层叠，浅色主题换浅色映射 —— */
  --sv-hero-grad:
    radial-gradient(420px 260px at 82% -30%, rgba(79, 140, 255, 0.16), transparent 68%),
    radial-gradient(360px 240px at 55% 130%, rgba(139, 92, 246, 0.1), transparent 68%),
    linear-gradient(135deg, #1a2130 0%, #171920 55%, #191a26 100%);
  --sv-hero-border: rgba(96, 120, 180, 0.22);
  --sv-hero-fg: #f2f4f8;               /* Hero 大标题（深底亮字/浅底深字） */
  --sv-chip-bg: rgba(12, 16, 25, 0.62); /* 引擎状态胶囊底 */
  --sv-gpu-grad:
    radial-gradient(300px 150px at 86% -24%, rgba(79, 140, 255, 0.2), transparent 68%),
    radial-gradient(220px 140px at -6% 118%, rgba(139, 92, 246, 0.14), transparent 68%),
    linear-gradient(180deg, #1a1f2a, #171a21);
  --sv-gpu-border: rgba(96, 130, 200, 0.38);
  --sv-cpu-grad:
    radial-gradient(220px 120px at 92% -30%, rgba(79, 140, 255, 0.1), transparent 70%),
    linear-gradient(180deg, #1b202a, #171a21);
  --sv-cpu-border: rgba(96, 130, 200, 0.28);
  --sv-mem-grad:
    radial-gradient(180px 110px at 90% -30%, rgba(251, 191, 36, 0.09), transparent 70%),
    linear-gradient(180deg, #1c2027, #181b21);
  --sv-mem-border: rgba(180, 150, 90, 0.24);
  /* —— 像素重构示意画（Hero 艺术层，两种主题共用一幅"深夜场景"） —— */
  --sv-px-scene:
    radial-gradient(90px 70px at 74% 26%, rgba(255, 220, 150, 0.5), transparent 70%),
    linear-gradient(165deg, #35567e 0%, #2b3f68 45%, #1d2b4a 100%);
  --sv-px-detail:
    linear-gradient(180deg, transparent 62%, rgba(16, 22, 38, 0.85) 62.5%),
    linear-gradient(200deg, transparent 46%, rgba(20, 30, 52, 0.9) 46.5%);
  /* —— 金属渐变文字（型号名） —— */
  --sv-metal-grad: linear-gradient(100deg, #c8d6f2 0%, #8fb4ff 28%, #eef4ff 50%, #b39cff 72%, #c8d6f2 100%);
  --sv-metal-grad-2: linear-gradient(100deg, #d4ddf0 0%, #9db8e8 45%, #e6edf9 100%);
  --sv-accent-cyan: #22d3ee;
  /* —— 间距刻度 —— */
  --sv-space-1: 4px;
  --sv-space-2: 8px;
  --sv-space-3: 12px;
  --sv-space-4: 16px;
  --sv-space-5: 24px;
  --sv-space-6: 32px;
  /* —— 圆角刻度 —— */
  --sv-radius-sm: 8px;
  --sv-radius-md: 12px;
  --sv-radius-lg: 16px;
  --sv-radius: var(--sv-radius-md); /* 存量引用别名 */
  /* —— 动效 —— */
  --sv-ease: cubic-bezier(0.22, 1, 0.36, 1); /* 轻回弹 */
  --sv-dur-fast: 120ms;
  --sv-dur-normal: 200ms;
  /* —— 阴影与卡面高光 —— */
  --sv-card-inset: inset 0 1px 0 rgba(255, 255, 255, 0.05);
  --sv-shadow-card: 0 8px 28px rgba(0, 0, 0, 0.35);
  --sv-shadow-lift: 0 10px 30px rgba(0, 0, 0, 0.35);
  --sv-shadow-pop: 0 16px 48px rgba(0, 0, 0, 0.5);
  --sv-glow: 0 0 24px rgba(79, 140, 255, 0.28);
  /* —— 窗口 chrome（标题栏） —— */
  --sv-titlebar-grad: linear-gradient(180deg, rgba(30, 34, 42, 0.92), rgba(22, 25, 31, 0.96));
  --sv-titlebar-line: rgba(255, 255, 255, 0.055);
  --sv-title-grad: linear-gradient(90deg, #f2f4f7, #a8adb5);
  --sv-ctl-hover: rgba(255, 255, 255, 0.08);
  --sv-ctl-fg: #9aa0a6;
  --sv-ctl-fg-hover: #e8eaed;
  --sv-ctl-close: #e81123;
  /* —— 滚动条 —— */
  --sv-scroll-thumb: #2e333d;
  --sv-scroll-thumb-hover: #3d4450;
  /* —— 骨架屏底 —— */
  --sv-skel-bg: #1d2027;
}

/* 浅色主题：同一组 token 的浅色映射（hero/硬件画布等品牌氛围岛保持深色） */
[data-theme='light'] {
  --sv-bg: #f5f6f8;
  --sv-bg-top: #eceff4;
  --sv-panel: #ffffff;
  --sv-panel-hi: #ffffff;
  --sv-panel-2: #f3f5f8;
  --sv-panel-deep: #e8ebf0;
  --sv-panel-grad: linear-gradient(180deg, #ffffff, #fdfdfe);
  --sv-well: rgba(16, 24, 40, 0.045);
  --sv-border: #dce1ea;
  --sv-border-soft: rgba(16, 24, 40, 0.09);
  --sv-border-mid: rgba(16, 24, 40, 0.14);
  --sv-border-strong: rgba(16, 24, 40, 0.22);
  --sv-fill-1: rgba(16, 24, 40, 0.02);
  --sv-fill-2: rgba(16, 24, 40, 0.045);
  --sv-fill-3: rgba(16, 24, 40, 0.06);
  --sv-text: #1c2333;
  --sv-text-dim: #525c6d;
  --sv-text-faint: #6d7480;
  --sv-text-code: #333c4d;
  --sv-accent: #3a78f2;
  --sv-accent-strong: #2f66d9;
  --sv-accent-2: #7c4ef0;
  --sv-accent-2-strong: #6d3fe0;
  --sv-danger: #d64d4d;
  --sv-danger-strong: #c03e3e;
  --sv-success: #0e9f66;
  --sv-success-strong: #0b8757;
  --sv-warning: #b57d0b;
  --sv-warning-deep: #9a6a08;
  --sv-accent-rgb: 58, 120, 242;
  --sv-accent2-rgb: 124, 78, 240;
  --sv-success-rgb: 14, 159, 102;
  --sv-warning-rgb: 181, 125, 11;
  --sv-danger-rgb: 214, 77, 77;
  --sv-accent-bg: rgba(58, 120, 242, 0.09);
  --sv-success-bg: rgba(14, 159, 102, 0.09);
  --sv-warning-bg: rgba(181, 125, 11, 0.09);
  --sv-danger-bg: rgba(214, 77, 77, 0.08);
  --sv-card-inset: inset 0 1px 0 rgba(255, 255, 255, 0.85);
  --sv-shadow-card: 0 8px 24px rgba(16, 24, 40, 0.08);
  --sv-shadow-lift: 0 10px 30px rgba(16, 24, 40, 0.13);
  --sv-shadow-pop: 0 16px 48px rgba(16, 24, 40, 0.2);
  --sv-glow: 0 0 24px rgba(58, 120, 242, 0.2);
  --sv-titlebar-grad: linear-gradient(180deg, rgba(250, 251, 253, 0.95), rgba(241, 243, 247, 0.97));
  --sv-titlebar-line: rgba(16, 24, 40, 0.09);
  --sv-title-grad: linear-gradient(90deg, #232b3a, #4a5261);
  --sv-ctl-hover: rgba(16, 24, 40, 0.07);
  --sv-ctl-fg: #5a616c;
  --sv-ctl-fg-hover: #1c2333;
  --sv-scroll-thumb: #c6ccd7;
  --sv-scroll-thumb-hover: #aab2c0;
  --sv-skel-bg: #e6e9ef;
  --sv-metal-grad: linear-gradient(100deg, #3a465c 0%, #2c3950 45%, #1f2a3d 100%);
  --sv-metal-grad-2: linear-gradient(100deg, #333c4d 0%, #4a5261 45%, #2a3242 100%);
  --sv-accent-cyan: #0e8fa8;
  --sv-hero-grad:
    radial-gradient(420px 260px at 82% -30%, rgba(58, 120, 242, 0.1), transparent 68%),
    radial-gradient(360px 240px at 55% 130%, rgba(124, 78, 240, 0.07), transparent 68%),
    linear-gradient(135deg, #eef1fa 0%, #f7f8fc 55%, #f3f2fb 100%);
  --sv-hero-border: rgba(58, 120, 242, 0.18);
  --sv-hero-fg: #1c2333;
  --sv-chip-bg: rgba(255, 255, 255, 0.78);
  --sv-gpu-grad:
    radial-gradient(300px 150px at 86% -24%, rgba(58, 120, 242, 0.08), transparent 68%),
    radial-gradient(220px 140px at -6% 118%, rgba(124, 78, 240, 0.05), transparent 68%),
    linear-gradient(180deg, #ffffff, #f6f8fd);
  --sv-gpu-border: rgba(58, 120, 242, 0.22);
  --sv-cpu-grad:
    radial-gradient(220px 120px at 92% -30%, rgba(58, 120, 242, 0.06), transparent 70%),
    linear-gradient(180deg, #ffffff, #f5f8fd);
  --sv-cpu-border: rgba(58, 120, 242, 0.18);
  --sv-mem-grad:
    radial-gradient(180px 110px at 90% -30%, rgba(181, 125, 11, 0.06), transparent 70%),
    linear-gradient(180deg, #ffffff, #fdfbf4);
  --sv-mem-border: rgba(181, 125, 11, 0.24);
}

* { margin: 0; padding: 0; box-sizing: border-box; }
html {
  background: var(--sv-bg);
  color: var(--sv-text);
  /* 主题切换的 200ms 缓冲，避免生硬闪切 */
  transition: background-color var(--sv-dur-normal) ease, color var(--sv-dur-normal) ease;
}
html, body, #app { height: 100%; overflow: hidden; }
body {
  font-family: system-ui, 'Segoe UI', 'Microsoft YaHei', sans-serif;
  /* 顶栏到底部极淡的纵向深度，避免大面积径向渐变在低端 GPU 上的 banding */
  background: linear-gradient(180deg, var(--sv-bg-top) 0%, var(--sv-bg) 46%);
  color: var(--sv-text);
  -webkit-font-smoothing: antialiased;
}
.app { display: flex; flex-direction: column; height: 100vh; position: relative; z-index: 0; }
/* ---- 背景氛围层（固定在 .app 上，不随页面滚动重绘）----
   ① 双角极淡品牌辉光（静态，性能红线内）
   ② 2% 噪点纹理：消除暗色大面积渐变的 banding */
.app::before {
  content: '';
  position: absolute;
  inset: 0;
  pointer-events: none;
  background:
    radial-gradient(1100px 680px at 6% -12%, rgba(79, 140, 255, 0.05), transparent 62%),
    radial-gradient(900px 620px at 97% 110%, rgba(139, 92, 246, 0.04), transparent 62%);
}
.app::after {
  content: '';
  position: absolute;
  inset: 0;
  pointer-events: none;
  opacity: 0.02;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='128' height='128'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='128' height='128' filter='url(%23n)'/%3E%3C/svg%3E");
  background-size: 128px 128px;
}
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

/* ---- 页面切换过渡：方向感知（侧栏向下点=新页从下方浮入，反之上方） ---- */
.page-down-enter-active,
.page-up-enter-active {
  transition: opacity 0.18s var(--sv-ease), transform 0.18s var(--sv-ease);
}
.page-down-leave-active,
.page-up-leave-active {
  transition: opacity 0.14s ease-in, transform 0.14s ease-in;
}
.page-down-enter-from { opacity: 0; transform: translateY(12px); }
.page-down-leave-to { opacity: 0; transform: translateY(-6px); }
.page-up-enter-from { opacity: 0; transform: translateY(-12px); }
.page-up-leave-to { opacity: 0; transform: translateY(6px); }

/* ---- 统一卡片体系 ----
   基座：面板渐变底 + 软描边 + 圆角 + 顶部 1px 内嵌高光（磨砂玻璃叠深色的层次）；
   .hoverable：小卡片/可点卡的浮起态（大画布面板不加，避免满屏乱动） */
.sv-card {
  background: var(--sv-panel-grad);
  border: 1px solid var(--sv-border-soft);
  border-radius: var(--sv-radius-md);
  box-shadow: var(--sv-card-inset);
}
.sv-card.hoverable {
  transition:
    transform 0.18s var(--sv-ease),
    border-color 0.18s var(--sv-ease),
    box-shadow 0.18s var(--sv-ease);
}
.sv-card.hoverable:hover {
  transform: translateY(-2px);
  border-color: var(--sv-border-strong);
  box-shadow: var(--sv-card-inset), var(--sv-shadow-lift);
}

/* 跳变数字（fps/ETA/统计）：等宽 + 微收字距，防布局抖动 */
.sv-num {
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.02em;
}

/* ---- 主按钮：品牌渐变 + 柔光（ghost/secondary/quaternary 变体不受影响） ---- */
.n-button.n-button--primary-type:not(.n-button--ghost):not(.n-button--secondary):not(.n-button--tertiary):not(.n-button--quaternary) {
  background: var(--sv-btn-grad);
  border: 1px solid transparent;
  box-shadow: 0 2px 12px rgba(var(--sv-accent-rgb), 0.32), inset 0 1px 0 rgba(255, 255, 255, 0.14);
  color: #fff;
}
.n-button.n-button--primary-type:not(.n-button--ghost):not(.n-button--secondary):not(.n-button--tertiary):not(.n-button--quaternary):not(:disabled):hover {
  background: var(--sv-btn-grad-hover);
  box-shadow: 0 4px 18px rgba(var(--sv-accent-rgb), 0.42), inset 0 1px 0 rgba(255, 255, 255, 0.16);
}
.n-button.n-button--primary-type:not(.n-button--ghost):not(.n-button--secondary):not(.n-button--tertiary):not(.n-button--quaternary):not(:disabled):active {
  background: var(--sv-btn-grad-active);
}

/* 键盘可达性：焦点环 */
button:focus-visible, .n-button:focus-visible {
  outline: 2px solid rgba(var(--sv-accent-rgb), 0.65);
  outline-offset: 2px;
}
::selection { background: rgba(var(--sv-accent-rgb), 0.35); }

/* ---- 通用骨架占位（shimmer）：各页加载态用 ---- */
.sv-skeleton {
  position: relative;
  overflow: hidden;
  background: var(--sv-skel-bg);
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
  border: 1px solid rgba(var(--sv-danger-rgb), 0.45);
  border-radius: var(--sv-radius-md);
  background: var(--sv-danger-bg);
  text-align: center;
}
.init-error-title { font-size: 16px; font-weight: 600; margin-bottom: 10px; }
.init-error-detail {
  font-size: 12px;
  color: var(--sv-text-dim);
  margin-bottom: 16px;
  word-break: break-all;
  white-space: pre-wrap;
}
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-thumb {
  background: var(--sv-scroll-thumb);
  border-radius: 6px;
  border: 2px solid transparent;
  background-clip: padding-box;
}
::-webkit-scrollbar-thumb:hover { background: var(--sv-scroll-thumb-hover); background-clip: padding-box; }
::-webkit-scrollbar-track { background: transparent; }

/* 减少动态偏好：全部动画/过渡即刻静止（装饰性动效的统一退路） */
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
    scroll-behavior: auto !important;
  }
}
</style>
