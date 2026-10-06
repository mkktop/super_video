import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, type App } from 'vue'
import { api, type WatermarkJob, type WatermarkPreview } from '@src/api'
import Watermark from '@src/pages/Watermark.vue'

const feedback = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn(), warning: vi.fn() }))
vi.mock('naive-ui', async (original) => ({
  ...await original<typeof import('naive-ui')>(), useMessage: () => feedback,
}))
let app: App
let state: any
const preview: WatermarkPreview = {
  width: 1100, height: 1645, box: [925, 1570, 1100, 1645], original: 'before', processed: 'after',
}
const job: WatermarkJob = {
  id: 'job', status: 'running', total: 2, completed: 0, succeeded: 0, failed: 0,
  elapsed_s: 0, current: '', output_dir: 'D:\\out', errors: [],
}
async function flush() { for (let i = 0; i < 8; i++) { await Promise.resolve(); await nextTick() } }
async function selectFiles() {
  await state.pickFiles(); await flush(); await vi.advanceTimersByTimeAsync(250); await flush()
}
beforeEach(() => {
  vi.useFakeTimers(); vi.clearAllMocks()
  Object.defineProperty(window, 'sv', { configurable: true, value: {
    pickImages: vi.fn().mockResolvedValue(['D:\\manga\\001.jpg', 'D:\\manga\\002.jpg']),
    pickDir: vi.fn().mockResolvedValue('D:\\manga'), openPath: vi.fn(),
  } })
  vi.spyOn(api, 'watermarkPreview').mockResolvedValue({ ...preview })
  vi.spyOn(api, 'startWatermark').mockResolvedValue({ ...job })
  vi.spyOn(api, 'watermarkJob').mockResolvedValue({ ...job, status: 'done', completed: 2, succeeded: 2 })
  app = createApp({ ...Watermark, render: () => null })
  state = (app.mount(document.createElement('div')) as any).$.setupState
})
afterEach(() => { app.unmount(); vi.restoreAllMocks(); vi.useRealTimers() })

describe('图片去水印', () => {
  it('只在当前区域预览成功后批量提交，并显示最终进度', async () => {
    expect(state.canStart).toBeFalsy()
    await selectFiles()
    expect(state.canStart).toBeTruthy()
    state.mask.height = 80
    await flush()
    expect(state.canStart).toBeFalsy()
    await vi.advanceTimersByTimeAsync(250); await flush()
    await state.start(); await flush()
    expect(api.startWatermark).toHaveBeenCalledWith({
      paths: ['D:\\manga\\001.jpg', 'D:\\manga\\002.jpg'], folder: undefined, output_dir: undefined,
      mask: { unit: 'px', width: 175, height: 80, right: 0, bottom: 0 },
      removal: 'auto',
    })
    expect(state.job.status).toBe('done')
    expect(state.active).toBe(false)
  })

  it('忽略旧图片的迟到预览，预览失败不会提交', async () => {
    let resolveOld: (value: WatermarkPreview) => void = () => {}
    vi.mocked(api.watermarkPreview).mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve }))
    await selectFiles()
    state.selected = 1; await flush()
    resolveOld({ ...preview, width: 999 }); await flush()
    expect(state.preview).toBeNull()
    vi.mocked(api.watermarkPreview).mockRejectedValueOnce(new Error('区域越界'))
    await vi.advanceTimersByTimeAsync(250); await flush()
    expect(state.previewError).toContain('区域越界')
    expect(state.canStart).toBeFalsy()
    await state.start()
    expect(api.startWatermark).not.toHaveBeenCalled()
  })

  it('请求失败后保留图片和参数，可重新提交', async () => {
    await selectFiles()
    vi.mocked(api.startWatermark).mockRejectedValueOnce(new Error('无法创建输出目录'))
    await state.start()
    expect(state.active).toBe(false)
    expect(state.files).toHaveLength(2)
    expect(state.canStart).toBeTruthy()
    expect(feedback.error).toHaveBeenCalled()
  })

  it('文件夹扫描提交源根目录；像素和百分比转换保持区域', async () => {
    vi.spyOn(api, 'scanImageFolder').mockResolvedValue({ folder: 'D:\\manga', total: 1, dirs: 1,
      files: [{ path: 'D:\\manga\\ch01\\001.jpg', rel: 'ch01/001.jpg' }] })
    await state.pickFolder(); await flush(); await vi.advanceTimersByTimeAsync(250); await flush()
    state.changeUnit('percent'); await flush()
    expect(state.mask.width).toBeCloseTo(15.909, 3)
    state.changeUnit('px'); await flush()
    expect(state.mask.width).toBe(175)
    await vi.advanceTimersByTimeAsync(250); await flush()
    await state.start()
    expect(api.startWatermark).toHaveBeenCalledWith(expect.objectContaining({ folder: 'D:\\manga' }))
  })
  it('选择多层文件夹后立即加载第一张示例，不需要手动选择或等待防抖', async () => {
    vi.spyOn(api, 'scanImageFolder').mockResolvedValue({ folder: 'D:\\manga', total: 2, dirs: 3,
      files: [
        { path: 'D:\\manga\\vol01\\ch01\\001.jpg', rel: 'vol01/ch01/001.jpg' },
        { path: 'D:\\manga\\vol02\\ch01\\001.jpg', rel: 'vol02/ch01/001.jpg' },
      ] })
    await state.pickFolder()
    expect(api.watermarkPreview).toHaveBeenCalledTimes(1)
    expect(api.watermarkPreview).toHaveBeenCalledWith('D:\\manga\\vol01\\ch01\\001.jpg', expect.any(Object), { removal: 'auto' })
    expect(state.selected).toBe(0)
    expect(state.preview).toEqual(preview)
    expect(state.canStart).toBeTruthy()
    expect(state.options.map((item: { label: string }) => item.label)).toEqual(['vol01/ch01/001.jpg', 'vol02/ch01/001.jpg'])
    await vi.advanceTimersByTimeAsync(300)
    expect(api.watermarkPreview).toHaveBeenCalledTimes(1)
  })
  it('首图加载失败后重选同一文件夹也会重新加载', async () => {
    vi.spyOn(api, 'scanImageFolder').mockResolvedValue({ folder: 'D:\\manga', total: 1, dirs: 1,
      files: [{ path: 'D:\\manga\\ch01\\001.jpg', rel: 'ch01/001.jpg' }] })
    vi.mocked(api.watermarkPreview).mockRejectedValueOnce(new Error('读取失败'))
    await state.pickFolder()
    expect(state.previewError).toContain('读取失败')
    await state.pickFolder()
    expect(api.watermarkPreview).toHaveBeenCalledTimes(2)
    expect(state.previewError).toBe('')
    expect(state.preview).toEqual(preview)
  })
  it('智能模式必须先设置样本，并在后续图片沿用样本进行检测', async () => {
    await selectFiles()
    state.mode = 'smart'; await flush(); await vi.advanceTimersByTimeAsync(250); await flush()
    expect(state.canStart).toBeFalsy()
    expect(state.canCapture).toBeTruthy()
    const detected = { ...preview, detected: true, score: .98 }
    vi.mocked(api.watermarkPreview).mockResolvedValue(detected)
    await state.captureSample()
    expect(state.canStart).toBeTruthy()
    expect(state.sample.path).toBe('D:\\manga\\001.jpg')
    expect(state.regionLocked).toBeTruthy()
    state.selected = 1; await flush(); await vi.advanceTimersByTimeAsync(250); await flush()
    expect(api.watermarkPreview).toHaveBeenLastCalledWith('D:\\manga\\002.jpg', expect.any(Object),
      expect.objectContaining({ mode: 'smart', sample: { path: 'D:\\manga\\001.jpg', mask: { unit: 'px', width: 175, height: 75, right: 0, bottom: 0 } } }))
    await state.start()
    expect(api.startWatermark).toHaveBeenCalledWith(expect.objectContaining({ mode: 'smart', sample: expect.objectContaining({ path: 'D:\\manga\\001.jpg' }) }))
  })
  it('未找到可靠匹配时不显示填白框且不允许提交，重设样本回到源图片', async () => {
    await selectFiles(); state.mode = 'smart'
    await flush(); await vi.advanceTimersByTimeAsync(250); await flush()
    vi.mocked(api.watermarkPreview).mockResolvedValue({ ...preview, detected: true, score: .99 })
    await state.captureSample()
    state.selected = 1
    vi.mocked(api.watermarkPreview).mockResolvedValue({ ...preview, detected: false, box: null, reason: '没有匹配' })
    await flush(); await vi.advanceTimersByTimeAsync(250); await flush()
    expect(state.canStart).toBeFalsy()
    expect(state.preview.box).toBeNull()
    expect(state.rectangle).toEqual({})
    vi.mocked(api.watermarkPreview).mockResolvedValue({ ...preview })
    await state.resetSample()
    expect(state.selected).toBe(0)
    expect(state.sample).toBeNull()
    expect(state.canCapture).toBeTruthy()
  })
  it('不合格样本提示错误，允许重新框选；新文件夹清除旧样本', async () => {
    await selectFiles(); state.mode = 'smart'
    await flush(); await vi.advanceTimersByTimeAsync(250); await flush()
    vi.mocked(api.watermarkPreview).mockRejectedValueOnce(new Error('样本可能包含漫画内容'))
    await state.captureSample(); await flush()
    expect(state.sample).toBeNull()
    expect(feedback.warning).toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(250); await flush()
    vi.mocked(api.watermarkPreview).mockResolvedValue({ ...preview, detected: true, score: .99 })
    await state.captureSample()
    expect(state.sample).not.toBeNull()
    vi.spyOn(api, 'scanImageFolder').mockResolvedValue({ folder: 'D:\\manga', total: 1, dirs: 0,
      files: [{ path: 'D:\\manga\\003.jpg', rel: '003.jpg' }] })
    await state.pickFolder()
    expect(state.sample).toBeNull()
  })
  it('切换清除方式使旧预览失效，预览和批量提交使用一致的修补方式', async () => {
    await selectFiles()
    state.removal = 'repair'; await flush()
    expect(state.canStart).toBeFalsy()
    await vi.advanceTimersByTimeAsync(250); await flush()
    expect(api.watermarkPreview).toHaveBeenLastCalledWith(expect.any(String), expect.any(Object), { removal: 'repair' })
    await state.start()
    expect(api.startWatermark).toHaveBeenCalledWith(expect.objectContaining({ removal: 'repair' }))
  })
  it('固定区域自动识别跳过时不会允许提交批量处理', async () => {
    vi.mocked(api.watermarkPreview).mockResolvedValue({ ...preview, detected: false, box: null, reason: '背景不均匀' })
    await selectFiles()
    expect(state.canStart).toBeFalsy()
    await state.start()
    expect(api.startWatermark).not.toHaveBeenCalled()
  })
  it('固定区域修补可设置样本，并把文字遮罩样本带入预览和批量处理', async () => {
    await selectFiles(); state.removal = 'repair'
    await flush(); await vi.advanceTimersByTimeAsync(250); await flush()
    expect(state.canCapture).toBeTruthy()
    await state.captureSample()
    expect(state.regionLocked).toBeTruthy()
    state.selected = 1; await flush(); await vi.advanceTimersByTimeAsync(250); await flush()
    expect(api.watermarkPreview).toHaveBeenLastCalledWith(expect.any(String), expect.any(Object),
      expect.objectContaining({ removal: 'repair', sample: expect.objectContaining({ path: 'D:\\manga\\001.jpg' }) }))
    await state.start()
    expect(api.startWatermark).toHaveBeenCalledWith(expect.objectContaining({ removal: 'repair', sample: expect.any(Object) }))
  })
})
