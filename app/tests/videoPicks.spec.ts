import { beforeEach, describe, expect, it } from 'vitest'
import { VIDEO_EXT, useRecentVideos } from '@src/composables/videoPicks'

beforeEach(() => {
  localStorage.clear()
})

describe('VIDEO_EXT', () => {
  it('覆盖支持的视频容器', () => {
    for (const name of ['a.mp4', 'b.MKV', 'c.mov', 'd.webm', 'e.ts']) {
      expect(VIDEO_EXT.test(name), name).toBe(true)
    }
  })
  it('非视频扩展名不命中', () => {
    for (const name of ['a.jpg', 'b.png', 'c.srt', 'mp4', 'd.mp4.txt']) {
      expect(VIDEO_EXT.test(name), name).toBe(false)
    }
  })
})

describe('useRecentVideos.pushRecent', () => {
  it('新条目置顶、去重、截断到上限', () => {
    const { recents, pushRecent } = useRecentVideos(3)
    pushRecent(['a.mp4', 'b.mp4'])
    pushRecent(['c.mp4'])
    pushRecent(['a.mp4']) // 已存在：提顶不重复
    expect(recents.value).toEqual(['a.mp4', 'c.mp4', 'b.mp4'])
    pushRecent(['d.mp4', 'e.mp4']) // 超限截断：新组在前，旧项 a 仍占一席
    expect(recents.value).toEqual(['d.mp4', 'e.mp4', 'a.mp4'])
  })
  it('写入 localStorage 供双页共享（键 sv_recent_videos）', () => {
    const a = useRecentVideos()
    a.pushRecent(['x.mp4'])
    expect(JSON.parse(localStorage.getItem('sv_recent_videos') ?? '[]')).toEqual(['x.mp4'])
  })
})
