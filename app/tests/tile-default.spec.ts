import { describe, expect, it } from 'vitest'
import { nextTick } from 'vue'
import { useTileDefault } from '@src/composables/useTileDefault'

describe('useTileDefault', () => {
  it('未存储时默认 256（auto 全图推理易爆显存，用户拍板）', () => {
    localStorage.removeItem('sv-tile-manga')
    expect(useTileDefault('manga').value).toBe(256)
  })

  it('记忆上次选择：改值落盘，新实例恢复（跨重启语义）', async () => {
    localStorage.removeItem('sv-tile-image')
    const tile = useTileDefault('image')
    tile.value = 512
    await nextTick()
    expect(localStorage.getItem('sv-tile-image')).toBe('512')
    expect(useTileDefault('image').value).toBe(512)
  })

  it('垃圾存储值回落 256；0（自动）是合法记忆值', () => {
    localStorage.setItem('sv-tile-manga', 'abc')
    expect(useTileDefault('manga').value).toBe(256)
    localStorage.setItem('sv-tile-manga', '9999')
    expect(useTileDefault('manga').value).toBe(256)
    localStorage.setItem('sv-tile-manga', '0')
    expect(useTileDefault('manga').value).toBe(0)
  })

  it('漫画/图片两页各自独立记忆', async () => {
    localStorage.removeItem('sv-tile-manga')
    localStorage.removeItem('sv-tile-image')
    const manga = useTileDefault('manga')
    manga.value = 384
    await nextTick()
    expect(useTileDefault('image').value).toBe(256) // 互不串扰
    expect(useTileDefault('manga').value).toBe(384)
  })
})
