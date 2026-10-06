import type { ModelInfo, RecommendInfo } from '../api'

/** 应用前以当前模型目录校验，避免推荐响应迟到或模型已被移除。 */
export function recommendationChoice(rec: RecommendInfo | null, models: ModelInfo[]) {
  const model = models.find((m) => m.id === rec?.model_id)
  if (!rec?.model_id) return { model: undefined, error: '当前没有可用的推荐模型，请手动选择' }
  if (!model || model.kind === 'interp') return { model: undefined, error: '推荐模型已不可用，请重新分析素材或手动选择' }
  if (!model.vram_ok) return { model: undefined, error: model.vram_note || '本机显存不足，无法应用该推荐模型' }
  if (rec.target_scale === null || !model.scale.includes(rec.target_scale)) return { model: undefined, error: '推荐倍率与当前模型不匹配，请重新分析素材或手动选择' }
  return { model, error: '' }
}
