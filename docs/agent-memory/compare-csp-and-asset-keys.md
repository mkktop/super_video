---
name: compare-csp-and-asset-keys
description: 对比功能三个静默坏透的存量雷（成片 CSP 黑屏/图片资产 key 404/失败徽标 off-by-one），v0.4.6 全修；资产
  key 按作业形态分流的约定与排障指纹
metadata:
  node_type: memory
  type: project
  originSessionId: sess_f9e027fd-a96b-402a-bf3c-a90f940f6206
---

2026-09-03 用户连报两个对比功能不显示，挖出**三个叠加的存量缺陷**，全部随 v0.4.6（commit 8d13571 + 73655d0）修复：

1. **成片模式黑屏（v0.2.6~v0.4.5，纯黑 0:00/0:00 无报错）**：index.html CSP 是 v0.1.9 file:// 直读改造时写的，`media-src 'self' file:` 没放行 http——v0.2.6 模型对比的 `<video>` 改吃 `http://127.0.0.1:8730` 对比产物后被 CSP 在**发请求前**拦截（img-src 早有 127.0.0.1 豁免所以静帧一直好）。修=media-src 补 `http://127.0.0.1:*`。
2. **失败徽标 off-by-one（帮凶）**：VideoCompare 用 `fails > maxFails` 判「无法播放」，但计数到 maxFails 即停不再自增，判定恒 false——提示从未显示过，黑屏完全静默。修=预算耗尽后最后一次失败置独立 gaveUp 标志。
3. **图片对比静帧/总览破图（v0.2.10~v0.4.5，404）**：视频静帧迁 stills/ 子目录+key 带索引时图片作业形态没跟随——图片产物是**平铺 src_still.png + `<mid>.png`**，且平铺源图在白名单里无 key 可取。修=后端 asset_path 补 `src_still` 平铺 key；前端抽 `srcKey/outKey` 按作业 kind 分流（图片=`src_still`/`still/<mid>`，视频=带索引），静帧视图与总览网格共用。

**Why**: 前端资产消费端与后端产物形态是两处独立演化，改其中一头必须核对另一头的全部消费 key（grep 墯端 compareAssetUrl 的所有 key 构造）；CSP 改动要遍历 `img-src/media-src/connect-src` 三个指令的每一类资源源。
**How to apply**: 排障指纹——渲染层怪象先 grep sidecar.log：**零请求=发不出去（CSP/前端不构造），404=key 错位，200=媒体本身问题**（curl + token + Range 复现 video 元素行为）；给 `<video>/<img>` 加新源先查 CSP 指令；给资产 key 体系加形态时前后端+测试三处同步（test_compare 有图片 key 404/200 断言）。关联 [[d-soft-install-healthy]]（装版排障工具箱）。

**第四雷：对比滑块参考系错位（2026-09-15 用户实机报修，已修未发布）**：全页对比（CompareSlider）竖线画在**视口空间**（`left: pos%` 挂舞台），但源图层 `clip-path: inset()` 百分比按**图片自身盒**（原生像素宽）解析、随 `translate(pan) scale(zoom)` 一起映射——两套参考系错位，症状=「中点对齐、越往两边滑偏得越多」（偏差∝|pos−50%|·|视口宽−图显宽|）。修=把视口 x 换算到图片本地比例再裁剪（`components/compareSliderMath.ts::viewportSplitToFrac`，主视图与放大镜共用；放大镜 loupeSplit 本来就对，主视图漏了换算）。竖线滑到图外黑边时缝贴图缘、整图取一侧是**正确钳制语义**勿当 bug 修。VideoCompare 不受影响（视频层 `inset:0` 铺满舞台，clip 参考系=视口）。DOM 级验证法：`.line` 实测 x vs `panX + frac·dispW`，注意键盘派发后要等一帧 Vue 刷新再读。
