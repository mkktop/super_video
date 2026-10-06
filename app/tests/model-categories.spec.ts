import { describe, expect, it } from 'vitest'
import type { ModelInfo } from '@src/api'
import { modelCategory, modelCategoryLabel, speedLabels } from '@src/composables/modelCategories'

const model = (props: Partial<ModelInfo>) => ({ id: 'custom', content: [], ...props }) as ModelInfo

describe('模型用途分类', () => {
  it.each([
    ['mangajanai', 'manga_bw'], ['illustrationjanai-4x-dat2', 'illustration'],
    ['animesharp-4x', 'illustration'], ['realesrgan-x4plus-anime', 'illustration'],
    ['real-cugan-pro', 'anime_restore'], ['artcnn-r8f64', 'anime_restore'],
    ['dis-2x-balanced', 'general_upscale'], ['seemore-b', 'general_upscale'],
  ])('旧后端也能将 %s 分到 %s', (id, category) => {
    expect(modelCategory(model({ id }))).toBe(category)
  })
  it('补帧独立于素材标签，不混入动漫超分', () => {
    expect(modelCategory(model({ kind: 'interp', content: ['anime', 'real'] }))).toBe('interp')
  })
  it('新后端分类优先，旧自定义模型按内容兜底', () => {
    expect(modelCategory(model({ category: 'general_upscale', content: ['anime'] }))).toBe('general_upscale')
    expect(modelCategory(model({ content: ['anime'] }))).toBe('anime_video')
    expect(modelCategory(model({ category: 'unknown', content: ['general'] }))).toBe('photo_restore')
  })
  it('标签区分黑白和彩漫，慢速标签不声称画质更高', () => {
    expect(modelCategoryLabel(model({ id: 'mangajanai' }))).toBe('黑白漫画')
    expect(modelCategoryLabel(model({ id: 'illustrationjanai-4x-dat2' }))).toBe('彩漫 / 插画')
    expect(speedLabels.fastest).toContain('极速')
    expect(speedLabels.slow).toBe('🐢 慢速')
  })
})
