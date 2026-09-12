/**
 * 外观主题（纯 UI 状态，localStorage 持久化，不经后端设置）：
 * 深色 / 浅色 / 跟随系统。解析结果写 <html data-theme>，
 * App.vue 据此切换 CSS token 层与 naive-ui 的 darkTheme/null。
 */
import { ref, watch } from 'vue'

export type ThemeMode = 'dark' | 'light' | 'system'
const KEY = 'sv-theme'

function readMode(): ThemeMode {
  const v = localStorage.getItem(KEY)
  return v === 'light' || v === 'system' ? v : 'dark'
}

export const themeMode = ref<ThemeMode>(readMode())
/** 解析后的实际主题（system 跟随 matchMedia） */
export const themeResolved = ref<'dark' | 'light'>('dark')

const mq = window.matchMedia('(prefers-color-scheme: light)')

function apply() {
  themeResolved.value =
    themeMode.value === 'system' ? (mq.matches ? 'light' : 'dark') : themeMode.value
  const html = document.documentElement
  if (themeResolved.value === 'light') html.dataset.theme = 'light'
  else delete html.dataset.theme
}

mq.addEventListener('change', () => {
  if (themeMode.value === 'system') apply()
})

watch(themeMode, (m) => {
  localStorage.setItem(KEY, m)
  apply()
})

// 模块加载即生效：首帧前 html 已带 data-theme，避免深浅闪切
apply()
