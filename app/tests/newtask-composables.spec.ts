import { describe, expect, it } from 'vitest'
import { computed, ref } from 'vue'
import type { ModelInfo, ProbeInfo } from '@src/api'
import { useCustomResolution } from '@src/composables/useCustomResolution'
import { hasScene, useModelOptions } from '@src/composables/useModelOptions'
import { store } from '@src/store'

function mkModel(over: Partial<ModelInfo>): ModelInfo {
  return {
    id: 'm', name: 'M', scale: [2], content: [], speed: 'balanced',
    vram_gb: 4, description: '', tile_hint: 0, installed: false,
    ...over,
  } as ModelInfo
}

describe('useModelOptions', () => {
  it('已安装模型排最前（组内保持注册表顺序）', () => {
    store.models = [
      mkModel({ id: 'a', installed: false }),
      mkModel({ id: 'b', installed: true }),
      mkModel({ id: 'c', bundled: true, installed: false }),
      mkModel({ id: 'd', installed: true }),
    ]
    const modelId = ref('a')
    const { srModels } = useModelOptions(modelId, ref(2))
    expect(srModels.value.map((m) => m.id)).toEqual(['b', 'c', 'd', 'a']) // 稳定排序：已装组内保持注册表顺序
  })
  it('场景筛选命中默认场景（无 scenes 标签的模型按 video/image 处理）', () => {
    expect(hasScene(mkModel({ id: 'x', scenes: ['manga'] }), 'manga')).toBe(true)
    expect(hasScene(mkModel({ id: 'x', scenes: ['manga'] }), 'video')).toBe(false)
    expect(hasScene(mkModel({ id: 'y' }), 'video')).toBe(true)
  })
})

describe('useCustomResolution', () => {
  const model = mkModel({ id: 'm', scale: [2, 4] })
  function setup(probe: Partial<ProbeInfo>, targetW: number, targetH: number) {
    const inputs = ref(['C:/v/a.mp4'])
    const probeInfo = ref({
      ok: true, width: 640, height: 360, error: null, fps: 24, duration_s: 1,
      total_frames: 24, codec: 'h264', pix_fmt: 'yuv420p', has_audio: false,
      ...probe,
    } as ProbeInfo)
    const resMode = ref<'scale' | 'custom'>('custom')
    const r = useCustomResolution(
      inputs, probeInfo, computed(() => model), ref(2), resMode, ref(targetW), ref(targetH))
    return r
  }
  it('偶数化与最小覆盖倍率（只缩不放纪律）', () => {
    const r = setup({}, 1281, 719) // 奇数 → 1280x718
    expect(r.effW.value).toBe(1280)
    expect(r.effH.value).toBe(718)
    expect(r.customScale.value).toBe(2) // x2 已覆盖（720≥718），取最小
    expect(r.customOk.value).toBe(true)
  })
  it('低于源分辨率视为非法', () => {
    const r = setup({}, 320, 180)
    expect(r.belowSrc.value).toBe(true)
    expect(r.customOk.value).toBe(false)
  })
  it('超出模型原生上限视为非法', () => {
    const r = setup({}, 8192, 4320)
    expect(r.customScale.value).toBeNull()
    expect(r.customOk.value).toBe(false)
  })
  it('批量（多文件）不支持自定义', () => {
    const inputs = ref(['a.mp4', 'b.mp4'])
    const probeInfo = ref({
      ok: true, width: 640, height: 360, error: null, fps: 24, duration_s: 1,
      total_frames: 24, codec: 'h264', pix_fmt: 'yuv420p', has_audio: false,
    } as ProbeInfo)
    const r = useCustomResolution(
      inputs, probeInfo, computed(() => model), ref(2),
      ref('custom'), ref(1280), ref(720))
    expect(r.customAvailable.value).toBe(false)
    expect(r.customOk.value).toBe(false)
  })
})
