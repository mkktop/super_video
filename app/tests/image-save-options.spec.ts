import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, type App } from 'vue'
import { api, type ModelInfo } from '@src/api'
import { store, ui } from '@src/store'
import ImageSR from '@src/pages/ImageSR.vue'
import MangaSR from '@src/pages/MangaSR.vue'

vi.mock('naive-ui', async (original) => ({
  ...await original<typeof import('naive-ui')>(),
  useMessage: () => ({ error: vi.fn(), success: vi.fn(), info: vi.fn() }),
}))
const apps: App[] = []
const model: ModelInfo = {
  id: 'model', name: 'Model', scale: [2], content: ['comic'], scenes: ['image', 'manga'],
  speed: 'fast', vram_gb: 1, description: '', tile_hint: 256, installed: true, vram_ok: true,
}
beforeEach(() => {
  localStorage.clear()
  Object.assign(store, { models: [model], settings: {}, tasks: [] })
  Object.assign(ui, { pendingModel: null, pendingScale: null, pendingTaskParams: null })
  Object.defineProperty(window, 'sv', { configurable: true, value: { taskProgress: vi.fn() } })
  vi.spyOn(api, 'createTask').mockResolvedValue(new Response('{}', { status: 201 }))
  vi.spyOn(api, 'tasks').mockResolvedValue([])
})
afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  vi.restoreAllMocks()
})

describe.each([
  ['图片', ImageSR, 'imagesr'], ['漫画', MangaSR, 'mangasr'],
] as const)('%s保存选项', (_name, page, route) => {
  function mount() {
    ui.page = route
    const app = createApp({ ...page, render: () => null })
    apps.push(app)
    return (app.mount(document.createElement('div')) as any).$.setupState
  }
  it('默认关闭，并将选中的保存方式提交到任务', async () => {
    const s = mount()
    expect(s.pngFast).toBe(false)
    expect(s.asyncSave).toBe(false)
    s.modelId = 'model'; s.files = ['C:\\page.png']
    s.pngFast = true; s.asyncSave = true
    await s.submit()
    expect(api.createTask).toHaveBeenCalledWith(expect.objectContaining({
      params: expect.objectContaining({ png_fast: true, async_save: true }),
    }))
  })
  it('JPG任务不启用PNG低压缩，但保留后台保存', async () => {
    const s = mount()
    s.modelId = 'model'; s.files = ['C:\\page.png']
    s.format = 'jpg'; s.pngFast = true; s.asyncSave = true
    await s.submit()
    expect(api.createTask).toHaveBeenCalledWith(expect.objectContaining({
      params: expect.objectContaining({ format: 'jpg', png_fast: false, async_save: true }),
    }))
  })
  it('重试任务恢复选项，旧任务没有参数时重置为关闭', async () => {
    const s = mount()
    ui.pendingTaskParams = {
      input_path: 'C:\\page.png', model_id: 'model',
      params: { png_fast: true, async_save: true },
    } as any
    await nextTick()
    expect(s.pngFast).toBe(true)
    expect(s.asyncSave).toBe(true)
    ui.pendingTaskParams = { input_path: 'C:\\page.png', model_id: 'model', params: {} } as any
    await nextTick()
    expect(s.pngFast).toBe(false)
    expect(s.asyncSave).toBe(false)
  })
})
