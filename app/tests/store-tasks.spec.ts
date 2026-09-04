import { beforeEach, describe, expect, it } from 'vitest'
import { toRaw } from 'vue'
import { mergeTasks, sameTask, store } from '@src/store'
import type { Task } from '@src/api'

function mkTask(id: string, over: Partial<Task> = {}): Task {
  return {
    id,
    input_path: `C:/v/${id}.mp4`,
    output_path: `C:/o/${id}.mkv`,
    model_id: 'm1',
    params: { scale: 2 },
    status: 'queued',
    src_w: 1920,
    src_h: 1080,
    fps: 24,
    total_frames: 100,
    progress_frames: 0,
    fps_run: 0,
    fps_avg: 0,
    eta_sec: 0,
    error: null,
    preview_path: null,
    out_bytes: 0,
    elapsed_s: 0,
    queue_position: null,
    updated_at: 1,
    ...over,
  }
}

beforeEach(() => {
  store.tasks = []
})

describe('sameTask', () => {
  it('同引用直接相等', () => {
    const t = mkTask('a')
    expect(sameTask(t, t)).toBe(true)
  })
  it('逐字段相等为 true', () => {
    expect(sameTask(mkTask('a'), mkTask('a'))).toBe(true)
  })
  it('params 深比较：同内容不同对象为 true', () => {
    const a = mkTask('a', { params: { scale: 2, codec: 'h264' } })
    const b = mkTask('a', { params: { scale: 2, codec: 'h264' } })
    expect(sameTask(a, b)).toBe(true)
  })
  it('params 内容不同为 false', () => {
    const a = mkTask('a', { params: { scale: 2 } })
    const b = mkTask('a', { params: { scale: 4 } })
    expect(sameTask(a, b)).toBe(false)
  })
  it('进度字段变化为 false', () => {
    expect(sameTask(mkTask('a'), mkTask('a', { progress_frames: 5 }))).toBe(false)
  })
})

describe('mergeTasks', () => {
  it('未变化的任务保留旧对象引用（TaskCard 整卡跳过重渲染的依据）', () => {
    const old = mkTask('a')
    store.tasks = [old]
    mergeTasks([mkTask('a')])
    expect(toRaw(store.tasks[0])).toBe(old)
  })
  it('变化的任务换成新对象', () => {
    const old = mkTask('a')
    store.tasks = [old]
    mergeTasks([mkTask('a', { status: 'done' })])
    expect(store.tasks[0]).not.toBe(old)
    expect(store.tasks[0].status).toBe('done')
  })
  it('params 序列化串不同（含键序）视为变化——后端同任务键序稳定，无抖动', () => {
    const old = mkTask('a', { params: { scale: 2, codec: 'h264' } })
    store.tasks = [old]
    const incoming = mkTask('a', { params: { codec: 'h264', scale: 2 } })
    mergeTasks([incoming])
    expect(toRaw(store.tasks[0])).toBe(incoming)
  })
  it('消失的任务被移除、新任务被追加且顺序跟随后端', () => {
    store.tasks = [mkTask('a'), mkTask('b')]
    mergeTasks([mkTask('b', { status: 'done' }), mkTask('c')])
    expect(store.tasks.map((t) => t.id)).toEqual(['b', 'c'])
  })
})
