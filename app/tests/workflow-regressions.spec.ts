import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, KeepAlive, nextTick, ref, type App, type Component } from 'vue'
import { api, type ModelInfo } from '@src/api'
import { initStore, store, ui } from '@src/store'
import NewTask from '@src/pages/NewTask.vue'
import ImageSR from '@src/pages/ImageSR.vue'
import MangaSR from '@src/pages/MangaSR.vue'

const feedback = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn(), info: vi.fn(), warning: vi.fn() }))
const warningDialog = vi.hoisted(() => vi.fn())
vi.mock('naive-ui', async (original) => ({
  ...await original<typeof import('naive-ui')>(),
  useMessage: () => feedback,
  useDialog: () => ({ warning: warningDialog }),
}))

const model: ModelInfo = {
  id: 'model', name: 'Model', scale: [2, 4], content: ['anime'], scenes: ['video', 'image', 'manga'],
  speed: 'fast', vram_gb: 1, description: '', tile_hint: 256, installed: true, vram_ok: true,
}
const probe = { ok: true, width: 640, height: 360, fps: 24, duration_s: 10, total_frames: 240 }
const apps: App[] = []
async function flush() {
  for (let i = 0; i < 8; i++) { await Promise.resolve(); await nextTick() }
}

// 保留真实 setup/生命周期，省去与本次行为无关的组件库渲染。
function mount(page: Component) {
  const app = createApp({ ...page, render: () => null })
  const host = document.createElement('div')
  document.body.appendChild(host)
  apps.push(app)
  return (app.mount(host) as any).$.setupState
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  Object.assign(ui, { page: 'newtask', pendingInput: null, pendingModel: null, pendingScale: null, pendingTaskParams: null })
  Object.assign(store, { ready: false, tasks: [], models: [model], presets: [], hardware: null, settings: {}, initError: '' })
  Object.defineProperty(window, 'sv', { configurable: true, value: {
    fsExists: vi.fn().mockResolvedValue(true), taskProgress: vi.fn(),
  } })
  vi.spyOn(api, 'probe').mockImplementation(async () => new Response(JSON.stringify(probe)))
  vi.spyOn(api, 'suggestOutput').mockResolvedValue({ output: 'C:\\out\\template_model.mp4' })
  vi.spyOn(api, 'tasks').mockResolvedValue([])
})
afterEach(() => {
  apps.splice(0).forEach((app) => app.unmount())
  document.body.innerHTML = ''
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('新建视频任务跨页预填', () => {
  it.each(['removed', 'vram', 'scale'])('无效推荐（%s）不修改配置或误报成功', (reason) => {
    store.models = [model, { ...model, id: 'recommended', scale: reason === 'scale' ? [2] : [4],
      vram_ok: reason !== 'vram' }].filter((m) => reason !== 'removed' || m.id !== 'recommended')
    const state = mount(NewTask)
    state.modelId = 'model'; state.targetScale = 2
    state.probeInfo = { ...probe, recommend: { model_id: 'recommended', model_name: 'Recommended',
      target_scale: 4, deinterlace: true, deband: true, interp: 'off', animated: true, reasons: [] } }
    state.applyRecommendation()
    expect(state.modelId).toBe('model')
    expect(state.targetScale).toBe(2)
    expect(state.deinterlace).toBe(false)
    expect(state.deband).toBe(false)
    expect(feedback.success).not.toHaveBeenCalled()
    expect(feedback.warning).toHaveBeenCalled()
  })
  it('有效推荐应用真实模型、倍率和预处理，成功提示使用当前名称', () => {
    const state = mount(NewTask)
    state.probeInfo = { ...probe, recommend: { model_id: 'model', model_name: '过时名称',
      target_scale: 4, deinterlace: true, deband: true, interp: 'off', animated: true, reasons: [] } }
    state.applyRecommendation()
    expect(state.modelId).toBe('model')
    expect(state.targetScale).toBe(4)
    expect(state.deinterlace).toBe(true)
    expect(state.deband).toBe(true)
    expect(feedback.success).toHaveBeenCalledWith('已应用推荐配置：Model · x4')
  })
  it('切换倍率或模型时清除不支持的旧降噪选择', async () => {
    store.models = [{ ...model, denoise_levels: [0, 1, 2, 3],
      denoise_levels_by_scale: { '2': [0, 1, 2, 3], '4': [0, 3] } }]
    const state = mount(NewTask)
    state.modelId = 'model'; state.denoise = 1
    await flush()
    expect(state.denoise).toBe(1)
    state.targetScale = 4
    await flush()
    expect(state.denoise).toBeNull()
    state.denoise = 3
    await flush()
    expect(state.denoise).toBe(3)
    state.modelId = ''
    await flush()
    expect(state.denoise).toBeNull()
  })
  it('首次从剪切/模型对比直达，消费输入、模型和倍率且只探测一次', async () => {
    Object.assign(ui, { pendingInput: 'C:\\clip.mp4', pendingModel: 'model', pendingScale: 4 })
    const state = mount(NewTask)
    await flush()
    expect(state.inputs).toEqual(['C:\\clip.mp4'])
    expect(state.modelId).toBe('model')
    expect(state.targetScale).toBe(4)
    expect(api.probe).toHaveBeenCalledTimes(1)
    expect(ui.pendingInput).toBeNull()
    expect(ui.pendingModel).toBeNull()
  })

  it('KeepAlive 再次激活时更新素材，不保留上次预填', async () => {
    const visible = ref(true)
    const child = ref<any>()
    const page = { ...NewTask, render: () => null }
    const app = createApp({ render: () => h(KeepAlive, null, {
      default: () => visible.value ? h(page, { ref: child }) : h('div'),
    }) })
    apps.push(app)
    app.mount(document.createElement('div'))
    visible.value = false
    ui.page = 'trim'
    await flush()
    Object.assign(ui, { pendingInput: 'C:\\second.mp4', pendingModel: 'model', pendingScale: 2, page: 'newtask' })
    visible.value = true
    await flush()
    expect(child.value.$.setupState.inputs).toEqual(['C:\\second.mp4'])
    expect(api.probe).toHaveBeenCalledTimes(1)
  })
})

describe('创建请求失败恢复', () => {
  it.each([['视频', NewTask, 'newtask'], ['图片', ImageSR, 'imagesr'], ['漫画', MangaSR, 'mangasr']] as const)(
    '%s 请求拒绝后解除加载并保留素材，可重试', async (_name, page, route) => {
      ui.page = route
      const state = mount(page)
      state.modelId = 'model'
      if (route === 'newtask') { state.inputs = ['C:\\clip.mp4']; state.probeInfo = probe }
      else state.files = ['C:\\page.png']
      vi.spyOn(api, 'createTask').mockRejectedValue(new TypeError('Failed to fetch'))
      await state.submit()
      expect(state.submitting).toBe(false)
      expect(state.canSubmit).toBe(true)
      expect(route === 'newtask' ? state.inputs : state.files).toHaveLength(1)
      expect(feedback.error).toHaveBeenCalledWith(expect.stringContaining('Failed to fetch'))
    },
  )

  it('部分成功后请求失败，重试只保留尚未成功的输入', async () => {
    const state = mount(NewTask)
    state.inputs = ['C:\\a.mp4', 'C:\\b.mp4', 'C:\\c.mp4']
    state.modelId = 'model'
    vi.spyOn(api, 'createTask').mockResolvedValueOnce(new Response('{}', { status: 201 }))
      .mockRejectedValueOnce(new TypeError('offline'))
    await state.submit()
    expect(state.inputs).toEqual(['C:\\b.mp4', 'C:\\c.mp4'])
    expect(state.submitting).toBe(false)
  })

  it('活动任务输出冲突不弹覆盖确认，并恢复按钮', async () => {
    const state = mount(NewTask)
    state.inputs = ['C:\\clip.mp4']; state.probeInfo = probe; state.modelId = 'model'
    vi.spyOn(api, 'createTask').mockResolvedValue(new Response(JSON.stringify({ detail: '已有排队/运行中的任务将写入同一路径' }), { status: 409 }))
    await state.submit()
    expect(warningDialog).not.toHaveBeenCalled()
    expect(state.submitting).toBe(false)
  })

  it('探测网络失败也解除加载状态', async () => {
    const state = mount(NewTask)
    vi.mocked(api.probe).mockRejectedValueOnce(new TypeError('offline'))
    await state.setInput(['C:\\clip.mp4'])
    expect(state.probing).toBe(false)
    expect(state.canSubmit).toBe(false)
  })
})

describe('默认输出命名', () => {
  it('默认路径只作为预览，创建时由后端采用命名模板；手选路径保留', async () => {
    const state = mount(NewTask)
    state.modelId = 'model'
    await state.setInput(['C:\\clip.mp4'])
    await flush()
    expect(state.output).toBe('C:\\out\\template_model.mp4')
    expect(state.buildCreateBody('C:\\clip.mp4', false).output).toBeUndefined()
    state.outputTouched = true
    state.output = 'C:\\manual.mp4'
    expect(state.buildCreateBody('C:\\clip.mp4', false).output).toBe('C:\\manual.mp4')
    await state.autoFillOutput()
    expect(state.output).toBe('C:\\manual.mp4')
  })

  it('命名模板改变时重新取后端路径预览', async () => {
    const state = mount(NewTask)
    state.inputs = ['C:\\clip.mp4']; state.probeInfo = probe
    await flush()
    vi.mocked(api.suggestOutput).mockClear()
    store.settings = { output_name_template: '{name}_{model}' }
    await flush()
    expect(api.suggestOutput).toHaveBeenCalled()
  })

  it('快速重选素材后，旧路径预览不能覆盖新素材的预览', async () => {
    const state = mount(NewTask)
    state.inputs = ['C:\\old.mp4']; state.probeInfo = probe
    await flush()
    let resolveOld!: (value: { output: string }) => void
    vi.mocked(api.suggestOutput).mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve }))
    const oldPreview = state.autoFillOutput()
    await state.setInput(['C:\\new.mp4'])
    await flush()
    resolveOld({ output: 'C:\\old_output.mp4' })
    await oldPreview
    expect(state.output).toBe('C:\\out\\template_model.mp4')
  })
})

it('WS 重连后补回断线期间的模型与统计快照', async () => {
  vi.useFakeTimers()
  class Socket {
    static instances: Socket[] = []
    onopen?: () => void
    onclose?: () => void
    close() { this.onclose?.() }
    constructor() { Socket.instances.push(this) }
  }
  vi.stubGlobal('WebSocket', Socket)
  Object.assign(window.sv, {
    backendInfo: vi.fn().mockResolvedValue({ baseUrl: 'http://127.0.0.1:9999', token: '' }),
    win: { setCloseToTray: vi.fn() }, setUpdateSource: vi.fn(),
    onUpdateProgress: vi.fn(), onUpdateReady: vi.fn(), updateState: vi.fn().mockResolvedValue({}),
  })
  vi.spyOn(api, 'models').mockResolvedValue([model])
  vi.spyOn(api, 'presets').mockResolvedValue([])
  vi.spyOn(api, 'hardware').mockResolvedValue({ gpus: [] })
  vi.spyOn(api, 'engine').mockResolvedValue({ backend: 'cpu', python: '', detail: '' })
  vi.spyOn(api, 'settings').mockResolvedValue({ auto_update_check: false })
  vi.spyOn(api, 'stats').mockResolvedValue({ total: 0, done: 0, frames: 0, bytes: 0 })
  vi.spyOn(api, 'perfHistory').mockResolvedValue({ interval_s: 2, samples: [] })
  vi.spyOn(api, 'trtComponent').mockResolvedValue(null as any)
  await initStore()
  Socket.instances[0].onclose?.()
  await vi.advanceTimersByTimeAsync(2000)
  vi.mocked(api.models).mockResolvedValue([{ ...model, id: 'downloaded' }])
  vi.mocked(api.stats).mockResolvedValue({ total: 5, done: 5, frames: 100, bytes: 1000 })
  Socket.instances[1].onopen?.()
  await flush()
  expect(store.models[0].id).toBe('downloaded')
  expect(store.stats.done).toBe(5)
})
