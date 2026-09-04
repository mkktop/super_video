/** 自定义目标分辨率（新建任务页）：纪律=只允许"原生超分后缩小"。 */
import { computed, watch } from 'vue'
import type { ComputedRef, Ref } from 'vue'
import type { ModelInfo, ProbeInfo } from '../api'

export const tileOptions = [
  { label: '自动（模型默认）', value: 0 },
  ...[128, 192, 256, 384, 512, 768, 1024].map((v) => ({ label: `${v} px`, value: v })),
]

export function useCustomResolution(
  inputs: Ref<string[]>,
  probeInfo: Ref<ProbeInfo | null>,
  selectedModel: ComputedRef<ModelInfo | undefined>,
  targetScale: Ref<number>,
  resMode: Ref<'scale' | 'custom'>,
  targetW: Ref<number>,
  targetH: Ref<number>,
) {
  const customAvailable = computed(
    () => inputs.value.length === 1 && !!probeInfo.value?.ok,
  )
  const srcW = computed(() => probeInfo.value?.width ?? 0)
  const srcH = computed(() => probeInfo.value?.height ?? 0)
  const maxScale = computed(() => Math.max(...(selectedModel.value?.scale ?? [1])))
  // yuv420 编码要求偶数，奇数向下取整（后端同规则）
  const effW = computed(() => Math.max(2, Math.floor((targetW.value || 0) / 2) * 2))
  const effH = computed(() => Math.max(2, Math.floor((targetH.value || 0) / 2) * 2))
  // 取能覆盖目标的最小原生倍率（省算力）
  const customScale = computed(() =>
    (selectedModel.value?.scale ?? []).find(
      (s) => srcW.value * s >= effW.value && srcH.value * s >= effH.value,
    ) ?? null,
  )
  const belowSrc = computed(() => effW.value < srcW.value || effH.value < srcH.value)
  const customOk = computed(
    () => customAvailable.value && !belowSrc.value && customScale.value !== null,
  )
  const aspectNote = computed(() => {
    if (!srcW.value || !srcH.value || !effW.value || !effH.value) return ''
    const a1 = srcW.value / srcH.value
    const a2 = effW.value / effH.value
    return Math.abs(a1 - a2) / a1 > 0.01
      ? `宽高比将由 ${a1.toFixed(2)} 变为 ${a2.toFixed(2)}（轻微拉伸）`
      : ''
  })

  watch(resMode, (m) => {
    if (m === 'custom' && probeInfo.value?.ok) {
      targetW.value = probeInfo.value.width * targetScale.value
      targetH.value = probeInfo.value.height * targetScale.value
    }
  })
  watch(
    () => inputs.value.length,
    (n) => {
      if (n !== 1) resMode.value = 'scale' // 批量无逐文件探测，只支持倍数模式
    },
  )

  return {
    customAvailable, srcW, srcH, maxScale, effW, effH,
    customScale, belowSrc, customOk, aspectNote,
  }
}
