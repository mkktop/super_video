/** 对比产物缓存（手动清理）+ 对比静帧样本数设置。 */
import { ref } from 'vue'
import { useMessage } from 'naive-ui'
import { api } from '../api'
import { fmtBytes } from '../utils'

export function useCompareCache() {
  const message = useMessage()
  const cacheStats = ref<{ jobs: number; bytes: number } | null>(null)
  const clearingCache = ref(false)
  // 对比静帧样本数（1~8）：模型对比创建作业时快照；任务对比页打开时按当前值即时构建
  const stillCount = ref(4)
  const savingStillCount = ref(false)

  function apply(s: Record<string, unknown>) {
    stillCount.value = Math.min(8, Math.max(1, Math.round(Number(s.compare_still_count) || 4)))
  }

  async function saveStillCount() {
    const v = Math.round(stillCount.value)
    if (!Number.isFinite(v) || v < 1 || v > 8) {
      message.error('静帧样本数需在 1~8 之间')
      return
    }
    savingStillCount.value = true
    const r = await api.saveSettings({ compare_still_count: v })
    savingStillCount.value = false
    if (r.ok) {
      stillCount.value = v
      message.success('已保存，下次开始对比时生效')
    } else {
      message.error(`保存失败: ${(await r.json()).detail ?? r.status}`)
    }
  }

  async function loadCacheStats() {
    try {
      cacheStats.value = await api.compareCacheStats()
    } catch {
      /* 统计失败不致命：按钮显示禁用态 */
    }
  }

  async function doClearCache() {
    clearingCache.value = true
    try {
      const r = await api.clearCompareCache()
      message.success(
        r.removed_jobs > 0
          ? `已清理 ${r.removed_jobs} 个对比作业，释放 ${fmtBytes(r.freed_bytes)}`
          : '没有可清理的对比产物',
      )
      void loadCacheStats()
    } catch (e) {
      message.error(`清理失败: ${(e as Error).message}`)
    } finally {
      clearingCache.value = false
    }
  }

  return { cacheStats, clearingCache, stillCount, savingStillCount, saveStillCount, loadCacheStats, doClearCache, apply }
}
