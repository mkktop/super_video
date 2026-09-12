/**
 * 纯表现层 UI 状态（不碰 store.ts 的数据流）：
 * 页面切换方向——侧栏从上往下点（索引增大）新页从下方浮入，反之上方。
 * App.vue 的 <Transition :name> 按此值切换过渡类名。
 */
import { ref } from 'vue'

/** 1 = 向下导航（新页从下方 12px 浮入）；-1 = 向上导航 */
export const pageDir = ref<1 | -1>(1)

/** 侧栏导航项顺序（与 Sidebar items 一致），点击时按索引差写方向 */
export function navDirection(from: string, to: string): 1 | -1 {
  const ORDER = [
    'home', 'tasks', 'newtask', 'models', 'mcompare',
    'trim', 'imagesr', 'perf', 'logs', 'settings',
  ]
  const a = ORDER.indexOf(from)
  const b = ORDER.indexOf(to)
  if (a < 0 || b < 0 || a === b) return 1
  return b > a ? 1 : -1
}
