/** 展示层小工具（此前 fmtEta/fmtBytes 在三个页面各有一份且换算基准不一致） */

import { onBeforeUnmount, onMounted, ref, type Ref } from 'vue'

/** 秒 → 中文时长（够大才显示小时，秒取整） */
export function fmtEta(sec: number): string {
  if (!sec || sec < 0) return '—'
  const s = Math.floor(sec)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  return h ? `${h}时${m}分` : m ? `${m}分${s % 60}秒` : `${s % 60}秒`
}

/** 字节数 → 可读大小（统一 1024 基准：KiB/MiB/GiB 按页面习惯写作 KB/MB/GB） */
export function fmtBytes(b: number): string {
  if (b > 1024 ** 3) return `${(b / 1024 ** 3).toFixed(2)} GB`
  if (b > 1024 ** 2) return `${(b / 1024 ** 2).toFixed(1)} MB`
  if (b > 1024) return `${(b / 1024).toFixed(1)} KB`
  return `${b} B`
}

/** 元素级全屏切换（Electron=Chromium 原生支持，ESC 可退出）。
 *  用户按 ESC 原生退出时靠 fullscreenchange 同步状态。 */
export function useFullscreen(target: Ref<HTMLElement | null>) {
  const isFullscreen = ref(false)
  const onChange = () => {
    const el = target.value
    const fsEl = document.fullscreenElement
    isFullscreen.value = !!(el && fsEl && (fsEl === el || el.contains(fsEl)))
  }
  async function toggle() {
    const el = target.value
    if (!el) return
    try {
      if (document.fullscreenElement) await document.exitFullscreen()
      else await el.requestFullscreen()
    } catch {
      /* 全屏被拒绝（权限/已退出）：状态由 fullscreenchange 兜底纠正 */
    }
  }
  onMounted(() => document.addEventListener('fullscreenchange', onChange))
  onBeforeUnmount(() => document.removeEventListener('fullscreenchange', onChange))
  return { isFullscreen, toggle }
}
