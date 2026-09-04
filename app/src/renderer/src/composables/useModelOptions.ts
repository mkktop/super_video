/** 超分模型选择状态：场景筛选 + 已装优先排序 + 倍率/降噪选项（新建任务页用）。 */
import { computed, ref } from 'vue'
import type { ModelInfo } from '../api'
import { store } from '../store'
import type { Ref } from 'vue'

export const SCENES = ['video', 'manga', 'image'] as const
export const sceneLabel: Record<string, string> = { video: '视频', manga: '漫画', image: '图片' }
export const hasScene = (m: ModelInfo, s: string) => (m.scenes ?? ['video', 'image']).includes(s)

export const denoiseLabel = {
  0: '不降噪（no-denoise）',
  1: '轻度 denoise 1',
  2: '中度 denoise 2',
  3: 'denoise 3（去压缩噪，适合老片）',
} as Record<number, string>

export function useModelOptions(modelId: Ref<string>, targetScale: Ref<number>) {
  // 场景筛选（视频任务默认「全部」；漫画/图片向模型选了也能跑，只是不快）
  const scene = ref<'all' | 'video' | 'manga' | 'image'>('all')
  // 已下载（含内置）排最前（与图片超分/模型对比页同款）：稳定排序，组内保持注册表顺序
  const srModels = computed(() =>
    store.models
      .filter((m) => m.kind !== 'interp')
      .filter((m) => scene.value === 'all' || hasScene(m, scene.value))
      // !! 归一防 NaN：bundled 缺省（undefined）时 Number(undefined||false) 会让比较器失效、排序静默不生效
      .sort((a, b) => Number(!!(b.installed || b.bundled)) - Number(!!(a.installed || a.bundled))))
  const selectedModel = computed(() => store.models.find((m) => m.id === modelId.value))
  const scaleOptions = computed(() =>
    (selectedModel.value?.scale ?? []).map((s) => ({ label: `x${s}`, value: s })),
  )
  const interpOptions = computed(() => [
    { label: '关闭', value: 'off' },
    { label: 'RIFE 2×（帧率翻倍，需下载 23MB 模型）', value: 'rife2x' },
  ])
  // 降噪档位随模型注册表动态出（real-cugan 专属；缺省回退保守/3 两档）
  const denoiseOptions = computed(() => {
    const levels = selectedModel.value?.denoise_levels ?? [0, 3]
    return levels.map((n) => ({ label: denoiseLabel[n] ?? `denoise ${n}`, value: n }))
  })
  const hasDenoiseVariants = computed(
    () => (selectedModel.value?.denoise_levels?.length ?? 0) > 0,
  )

  /** 选模型：新模型支持当前倍率则保留，否则回落到最小倍率 */
  function selectModel(id: string) {
    const spec = store.models.find((m) => m.id === id)
    if (!spec || !spec.vram_ok) return
    modelId.value = id
    if (!spec.scale.includes(targetScale.value)) targetScale.value = Math.min(...spec.scale)
  }

  return {
    scene, srModels, selectedModel, scaleOptions,
    interpOptions, denoiseOptions, hasDenoiseVariants, selectModel,
  }
}
