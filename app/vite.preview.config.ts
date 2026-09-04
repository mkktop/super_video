import { defineConfig, type Plugin } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'

// <img>/<video> 的资源加载不走页面 fetch，mock 拦不到——预览资源（任务预览图、
// 对比静帧等）由这里的 dev 中间件直接回 SVG 占位图。JSON 类 /api 应答在
// src/renderer/src/preview/mock.ts 里由 fetch 替身处理。

function svgFrame(seedStr: string, w = 480, h = 270): string {
  let seed = 0
  for (const ch of seedStr) seed = (seed * 31 + ch.charCodeAt(0)) % 997
  const hA = (seed * 137) % 360
  const hB = (hA + 70 + (seed % 60)) % 360
  const sunX = 60 + (seed % 260)
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${w} ${h}">
  <defs><linearGradient id="sky" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="hsl(${hA},55%,38%)"/><stop offset="1" stop-color="hsl(${hB},60%,13%)"/>
  </linearGradient></defs>
  <rect width="${w}" height="${h}" fill="url(#sky)"/>
  <circle cx="${sunX}" cy="${h * 0.34}" r="26" fill="hsl(${hA},85%,72%)" opacity="0.85"/>
  <path d="M0 ${h * 0.72} L${w * 0.22} ${h * 0.44} L${w * 0.42} ${h * 0.68} L${w * 0.62} ${h * 0.4} L${w * 0.84} ${h * 0.7} L${w} ${h * 0.55} V${h} H0 Z" fill="hsl(${hB},45%,22%)"/>
  <path d="M0 ${h * 0.85} L${w * 0.3} ${h * 0.6} L${w * 0.55} ${h * 0.82} L${w * 0.78} ${h * 0.58} L${w} ${h * 0.78} V${h} H0 Z" fill="hsl(${hB},50%,15%)"/>
</svg>`
}

function imgPlugin(): Plugin {
  return {
    name: 'sv-preview-mock-images',
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const url = req.url ?? ''
        if (
          /^\/api\/(tasks\/[^/]+\/(preview|stills\/\d+|share-card\/file)|compare\/[^/]+\/asset\/)/.test(url)
        ) {
          const seed = decodeURIComponent(url.split('?')[0])
          const portrait = url.includes('manga') // 无语义，仅让部分图竖构图
          res.setHeader('Content-Type', 'image/svg+xml')
          res.end(svgFrame(seed, portrait ? 300 : 480, portrait ? 450 : 270))
          return
        }
        next()
      })
    },
  }
}

// 纯浏览器 UI 预览：不起 Electron / Python 后端即可走查渲染层样式。
// 用法：pnpm preview → http://localhost:5199/preview.html
// 注意：仅开发走查用，构建入口只有 index.html，本配置与 preview.html 均不进产物。
export default defineConfig({
  root: resolve(__dirname, 'src/renderer'),
  plugins: [vue(), imgPlugin()],
  server: { port: 5199, strictPort: true },
})
