import { afterEach, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive, type App } from 'vue'
import { api } from '@src/api'
import SubtitleSettings from '@src/components/SubtitleSettings.vue'
import WatermarkSettings from '@src/components/WatermarkSettings.vue'

const feedback = vi.hoisted(() => ({ error: vi.fn(), warning: vi.fn() }))
vi.mock('naive-ui', async (original) => ({
  ...await original<typeof import('naive-ui')>(),
  useMessage: () => feedback,
}))
let app: App | undefined
afterEach(() => {
  app?.unmount()
  document.body.innerHTML = ''
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

it('uses a CSP-compatible image and reports image load failures', async () => {
  const png = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aMWQAAAAASUVORK5CYII='
  const blob = new Blob([Uint8Array.from(atob(png), c => c.charCodeAt(0))], { type: 'image/png' })
  vi.spyOn(api, 'subtitlePreview').mockResolvedValue({
    ok: true, blob: async () => blob,
    headers: new Headers({ 'X-Subtitle-Time': '9.000' }),
  } as Response)
  const host = document.createElement('div')
  document.body.appendChild(host)
  app = createApp({ ...SubtitleSettings, render: () => null }, {
    modelValue: { source: 'external', path: 'C:/test.srt' },
    inputs: ['C:/test.mkv'], probe: null, width: 3840, height: 2160,
  })
  const state = (app.mount(host) as any).$.setupState
  await state.preview()
  await nextTick()
  expect(state.previewUrl).toBe(`data:image/png;base64,${png}`)
  expect(state.renderedTime).toBe('9.000')
  expect(state.previewing).toBe(false)
  state.imageFailed()
  expect(state.previewUrl).toBe('')
  expect(feedback.error).toHaveBeenCalledWith('字幕预览图片加载失败，请重新预览')
})

it('previews a timed watermark with configured subtitles and invalidates changed settings', async () => {
  const request = vi.spyOn(api, 'subtitlePreview').mockResolvedValue({
    ok: true, blob: async () => new Blob(['preview'], { type: 'image/png' }),
    headers: new Headers(),
  } as Response)
  const host = document.createElement('div')
  document.body.appendChild(host)
  const props = { modelValue: { kind: 'text', text: '雨帧', start_s: 2, duration_s: 3 },
    inputs: ['C:/test.mkv'], subtitle: { source: 'embedded', stream: 0 }, width: 3840, height: 2160 }
  app = createApp({ ...WatermarkSettings, render: () => null }, props)
  const vm = app.mount(host) as any
  const state = vm.$.setupState
  expect(state.ready).toBe(true)
  await state.preview()
  expect(request).toHaveBeenCalledWith(expect.objectContaining({ watermark: expect.objectContaining({ ...props.modelValue, position: 'top-right' }), subtitle: props.subtitle, time_s: 2.2 }))
  expect(state.image).toMatch(/^data:image\/png;base64,/)
  state.previewTime = 6
  await nextTick()
  expect(state.image).toBe('')
})

it('keeps a default-position preview when a control emits an unchanged value', async () => {
  let respond!: (value: Response) => void
  const request = vi.spyOn(api, 'subtitlePreview').mockImplementation(() => new Promise(resolve => { respond = resolve }))
  const props = reactive({ modelValue: { kind: 'text' as const, text: '雨帧' },
    inputs: ['C:/test.mkv'], width: 3840, height: 2160 })
  const host = document.createElement('div')
  document.body.appendChild(host)
  let child: any
  const component = { ...WatermarkSettings, render: () => null }
  app = createApp({ render: () => h(component, { ...props, ref: v => { child = v } }) })
  app.mount(host)
  const state = child.$.setupState
  const pending = state.preview()
  await nextTick()
  props.modelValue = { ...props.modelValue }
  await nextTick()
  respond({ ok: true, blob: async () => new Blob(['preview'], { type: 'image/png' }), headers: new Headers() } as Response)
  await pending
  expect(state.image).toMatch(/^data:image\/png;base64,/)
  expect(request).toHaveBeenCalledWith(expect.objectContaining({
    watermark: expect.objectContaining({ position: 'top-right' }), time_s: .2,
  }))
  props.modelValue = { ...props.modelValue, text: '改变文字' }
  await nextTick()
  expect(state.image).toBe('')
})
