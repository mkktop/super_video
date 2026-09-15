/** 对比滑块分割线换算：视口竖线位置 → 源图层 clip 的图片本地比例。
 *
 * 不变量：裁剪缝经 pan/zoom 映射回视口后必须落在竖线上（seamX = panX +
 * frac·dispW ≡ dividerX）；线滑出图片显示区（黑边）时整图取一侧。
 * 该 bug 的实锤形态：修复前 clip 直接拿 pos%，缝与竖线的偏差 =
 * (pos−50%)·(视口宽−图显宽)，中点对齐、越往两边滑偏得越多。
 */
import { describe, expect, it } from 'vitest'
import { viewportSplitToFrac } from '@src/components/compareSliderMath'

const CASES: Array<{ name: string; viewW: number; panX: number; dispW: number }> = [
  { name: '适配居中（图窄于视口）', viewW: 1000, panX: 300, dispW: 400 },
  { name: '1:1 图宽于视口（已平移）', viewW: 700, panX: -1500, dispW: 4000 },
  { name: '图恰好铺满视口', viewW: 800, panX: 0, dispW: 800 },
  { name: '放大 4:1 图远大于视口', viewW: 600, panX: -8200, dispW: 12000 },
]

describe('viewportSplitToFrac：缝与竖线重合', () => {
  for (const c of CASES) {
    it(`${c.name}：全行程各点位缝=竖线`, () => {
      for (const pos of [0, 7.3, 20, 50, 73.5, 95, 100]) {
        const frac = viewportSplitToFrac(pos, c.viewW, c.panX, c.dispW)
        expect(frac).toBeGreaterThanOrEqual(0)
        expect(frac).toBeLessThanOrEqual(1)
        const dividerX = (pos / 100) * c.viewW
        const seamX = c.panX + frac * c.dispW
        if (dividerX >= c.panX && dividerX <= c.panX + c.dispW) {
          // 竖线在图片显示区内：缝必须正好落在竖线上
          expect(Math.abs(seamX - dividerX)).toBeLessThan(1e-9)
        } else {
          // 竖线在图外黑边上：整图全源(0)或全出(1)，缝贴图边缘
          expect(seamX).toBe(Math.max(c.panX, Math.min(c.panX + c.dispW, dividerX)))
        }
      }
    })
  }

  it('修复前的 bug 形态回归：图窄于视口时 pos% 直接当 clip 会偏离竖线', () => {
    // 适配居中 viewW=1000 dispW=400 panX=300（图显示区 [300,700]）：
    // 旧算法 frac=pos/100，缝=300+pos%·400，与竖线 pos%·1000 系统性偏离
    const pos = 60
    const oldSeamX = 300 + (pos / 100) * 400 // 540 ≠ 竖线 600，差 60px
    const frac = viewportSplitToFrac(pos, 1000, 300, 400)
    const seamX = 300 + frac * 400
    expect(Math.abs(oldSeamX - 600)).toBeGreaterThan(50) // 旧法明显偏
    expect(Math.abs(seamX - 600)).toBeLessThan(1e-9) // 新法重合
  })

  it('dispW 非法时回落 0.5 不 NaN', () => {
    expect(viewportSplitToFrac(50, 1000, 0, 0)).toBe(0.5)
  })
})
