/**
 * 图片/漫画超分页的分块大小：默认 256（auto 全图推理在大图上易爆显存，
 * 用户拍板 9-30），localStorage 记忆上次选择——tile 是逐任务参数，
 * 按仓库纪律不进后端 settings，与主题同走「纯 UI 偏好」路线。
 */
import { ref, watch } from 'vue'

function readStored(key: string): number {
  const raw = localStorage.getItem(key)
  if (raw == null) return 256 // 无记忆：默认 256（注意 Number('')===0，不能拿空串喂 Number）
  const v = Number(raw)
  // 0=自动 也允许记忆；垃圾值（手动改 localStorage）回落默认
  return Number.isInteger(v) && v >= 0 && v <= 4096 ? v : 256
}

/** pageKey 区分漫画/图片两页各自记忆（场景不同，分块习惯可能不同） */
export function useTileDefault(pageKey: 'image' | 'manga') {
  const key = `sv-tile-${pageKey}`
  const tile = ref(readStored(key))
  watch(tile, (v) => {
    try {
      localStorage.setItem(key, String(v))
    } catch {
      /* 存储不可用（隐私模式等）：降级为本次会话内有效 */
    }
  })
  return tile
}
