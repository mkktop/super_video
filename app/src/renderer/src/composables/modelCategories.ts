import type { ModelInfo } from '../api'

/** 主用途与素材标签、任务场景独立；兼容旧后端与自定义模型。 */
export const categoryLabels = {
  anime_video: '动漫视频', anime_restore: '动漫重建 / 降噪',
  manga_bw: '黑白漫画', illustration: '彩漫 / 插画',
  photo_restore: '照片 / 通用修复', general_upscale: '轻量通用放大',
  interp: '补帧',
} as const
export type ModelCategory = keyof typeof categoryLabels
export const modelCategories = Object.keys(categoryLabels) as ModelCategory[]
export function modelCategory(m: ModelInfo): ModelCategory {
  if (m.kind === 'interp') return 'interp'
  if (m.category && modelCategories.includes(m.category as ModelCategory)) return m.category as ModelCategory
  if (m.id.startsWith('mangajanai')) return 'manga_bw'
  if (/^(illustrationjanai|animesharp|realesrgan-x4plus-anime)/.test(m.id)) return 'illustration'
  if (/^(artcnn|real-cugan)/.test(m.id)) return 'anime_restore'
  if (/^(dis-|seemore)/.test(m.id)) return 'general_upscale'
  if (m.content.includes('comic')) return 'illustration'
  if (m.content.includes('anime') && !m.content.includes('real') && !m.content.includes('general')) return 'anime_video'
  return 'photo_restore'
}
export const modelCategoryLabel = (m: ModelInfo) => categoryLabels[modelCategory(m)]
export const speedLabels: Record<string, string> = {
  fastest: '⚡ 极速', fast: '⚡ 快速', balanced: '⚖ 均衡', slow: '🐢 慢速',
}
