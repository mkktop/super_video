/** 对比滑块的分割线坐标换算（CompareSlider 主视图与放大镜共用）。
 *
 * 竖线画在视口空间（left: pos% 挂舞台容器），而源图层的 clip-path 百分比
 * 按图片自身盒解析、随 translate(pan) scale(zoom) 一起映射——两套参考系
 * 不能直接混用：直接拿 pos% 裁剪会在 1:1/适配下「中点对齐、越往两边滑
 * 偏差越大」。必须把视口 x 换算到图片本地比例再施加裁剪。
 */

/** 视口分割线 → 图片本地裁剪比例：frac = (pos%·viewW − panX) / dispW。
 * dispW = 图片原生宽 × zoom；线滑到图外（黑边上）时钳 0/1（整图全出/全源）。 */
export function viewportSplitToFrac(
  posPct: number, viewW: number, panX: number, dispW: number,
): number {
  if (dispW <= 0) return 0.5
  const frac = ((posPct / 100) * viewW - panX) / dispW
  return Math.min(1, Math.max(0, frac))
}
