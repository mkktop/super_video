/** TRT 可选组件卡：状态展示（store.trt 由 WS 事件维护）+ 安装/卸载。 */
import { computed, ref } from 'vue'
import { useMessage } from 'naive-ui'
import { api } from '../api'
import { refreshTrt, store } from '../store'

/** TRT 下载当前渠道文案：后端按实际命中的 URL 透出（主源 ModelScope，回落 GitHub 镜像） */
export const trtSrcText = (s: string | null | undefined) =>
  s === 'modelscope' ? 'ModelScope' : s === 'github' ? 'GitHub 镜像' : ''

export function fmtGB(b: number): string {
  return (b / 1e9).toFixed(2) + ' GB'
}

export function useTrtComponent() {
  const message = useMessage()
  const trcBusy = ref(false)

  /** 安装需下载的体积（core + 匹配架构的 builder 包） */
  const trcDownloadBytes = computed(() => {
    const t = store.trt
    if (!t || t.installed || t.installing) return 0
    const a = t.assets?.assets ?? {}
    const core = a.core?.size ?? 0
    const key = t.gpu_arch && a[`builder-${t.gpu_arch}`] ? `builder-${t.gpu_arch}` : 'builder-ptx'
    return core + (a[key]?.size ?? 0)
  })

  async function installTrc() {
    trcBusy.value = true
    const r = await api.installTrtComponent()
    if (!r.ok) message.error(`无法开始安装: ${(await r.json()).detail ?? r.status}`)
    // 成功则进度走 WS trt_component 事件;失败恢复按钮
    trcBusy.value = r.ok
  }

  async function uninstallTrc() {
    trcBusy.value = true
    const r = await api.uninstallTrtComponent()
    trcBusy.value = false
    if (r.ok) {
      message.success('已卸载，推理回退 DirectML')
      await refreshTrt()
    } else {
      message.error(`${(await r.json()).detail ?? r.status}`)
    }
  }

  return { trcBusy, trcDownloadBytes, installTrc, uninstallTrc }
}
