import { describe, expect, it } from 'vitest'
import { fmtBytes, fmtEta } from '@src/utils'

describe('fmtEta', () => {
  it('无效值与负数显示占位符', () => {
    expect(fmtEta(0)).toBe('—')
    expect(fmtEta(-5)).toBe('—')
    expect(fmtEta(Number.NaN)).toBe('—')
  })
  it('秒/分/时逐级进位', () => {
    expect(fmtEta(45)).toBe('45秒')
    expect(fmtEta(60)).toBe('1分0秒')
    expect(fmtEta(125)).toBe('2分5秒')
    expect(fmtEta(3600)).toBe('1时0分')
    expect(fmtEta(7325)).toBe('2时2分')
  })
  it('小数秒向下取整', () => {
    expect(fmtEta(59.9)).toBe('59秒')
  })
})

describe('fmtBytes', () => {
  it('按 1024 基准分级', () => {
    expect(fmtBytes(512)).toBe('512 B')
    expect(fmtBytes(2048)).toBe('2.0 KB')
    expect(fmtBytes(5 * 1024 ** 2)).toBe('5.0 MB')
    expect(fmtBytes(3.5 * 1024 ** 3)).toBe('3.50 GB')
  })
  it('边界：1024^2 恰好还在 KB 档（严格大于才进位）', () => {
    expect(fmtBytes(1024 ** 2)).toBe('1024.0 KB')
    expect(fmtBytes(1024 ** 2 + 1)).toBe('1.0 MB')
  })
})
