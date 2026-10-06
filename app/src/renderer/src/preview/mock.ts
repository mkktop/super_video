/**
 * 浏览器预览脚手架的本地 mock（仅 preview.html 入口使用，不进打包产物）：
 * - window.sv：Electron preload 桥的全部方法替身
 * - fetch：/api/* 后端路由的造数应答（含任务预览图等 SVG 占位资源）
 * - WebSocket：事件流替身（perf 2s 一拍 + 运行中任务进度 1s 一拍，循环推进）
 *
 * 造数覆盖全部任务状态与主流程页面，供样式走查；数据仅存在内存，刷新即还原。
 */

// BASE 指向 vite 预览服务器自身：<img>/<video> 的资源请求不走页面 fetch，
import { DEFAULT_ANIME_MODEL } from '../composables/modelDefaults'

// 由 vite.preview.config.ts 的中间件应答 SVG；JSON 类 /api 仍被下方 fetch 替身拦截。
const BASE = window.location.origin

// ---------------- 造数：静态清单 ----------------

const now = () => Math.floor(Date.now() / 1000)

const MODELS = [
  {
    id: DEFAULT_ANIME_MODEL, name: 'AnimeJaNai V3.1 HD Balanced Sharp（均衡·锐利）',
    scale: [2], kind: 'sr', content: ['anime'], category: 'anime_video',
    speed: 'fast', scenes: ['video', 'image'], vram_gb: 2, tile_hint: 0, engine: 'onnx',
    description: '动漫 HD 2x 默认推荐，偏锐利线条；建议先试跑片段检查噪声与轮廓。',
    installed: true, bundled: true, size_mb: 2, vram_ok: true,
  },
  {
    id: 'anj-hd-x2', name: 'AnimeJaNai V2 HD x2', scale: [2], kind: 'sr', content: ['anime'],
    speed: 'fast', scenes: ['video', 'image'], vram_gb: 2, tile_hint: 0, engine: 'onnx',
    description: '动漫 1080p 及以下素材的极速首选，原生 fp16，锐利且不失笔触',
    installed: true, bundled: true, size_mb: 62, vram_ok: true,
  },
  {
    id: 'anj-hd-x4', name: 'AnimeJaNai V2 HD x4', scale: [4], kind: 'sr', content: ['anime'],
    speed: 'fast', scenes: ['video', 'image'], vram_gb: 3, tile_hint: 0, engine: 'onnx',
    description: '动漫素材一步到 4K 的主力模型，线条干净、噪点控制好',
    installed: true, bundled: true, size_mb: 62, vram_ok: true,
  },
  {
    id: 'mangajanai-x4', name: 'MangaJaNai x4', scale: [4], kind: 'sr', content: ['comic'],
    speed: 'medium', scenes: ['manga', 'image'], vram_gb: 4, tile_hint: 256, engine: 'onnx',
    description: '漫画/扫图修复首选：网点纸与排线保留出色，灰阶过渡平滑',
    installed: true, bundled: false, size_mb: 128, vram_ok: true,
  },
  {
    id: 'illustrationjanai-x2', name: 'IllustrationJaNai x2', scale: [2], kind: 'sr',
    content: ['comic'],
    speed: 'fast', scenes: ['manga', 'image'], vram_gb: 2, tile_hint: 0, engine: 'onnx',
    description: '彩色漫画页 / 插画向：轻薄快速的 2 倍放大',
    installed: false, bundled: false, size_mb: 36, vram_ok: true,
  },
  {
    id: 'realesr-x4', name: 'RealESRGAN x4plus', scale: [4], kind: 'sr', content: ['real'],
    speed: 'medium', scenes: ['video', 'image'], vram_gb: 4, tile_hint: 256, engine: 'onnx',
    description: '真人/实拍通用超分，老片修复常客，对压缩噪声鲁棒',
    installed: true, bundled: false, size_mb: 134, vram_ok: true,
  },
  {
    id: 'cugan-x2', name: 'CUGAN 保守 x2', scale: [2], kind: 'sr', content: ['anime'],
    speed: 'slow', scenes: ['video', 'image'], vram_gb: 6, tile_hint: 256, engine: 'onnx',
    description: '重降噪路线：适合噪点密集的老动画源，速度换干净',
    installed: false, bundled: false, size_mb: 256, vram_ok: true,
    denoise_levels: [0, 1, 2, 3],
  },
  {
    id: 'artcnn-x4', name: 'ArtCNN x4', scale: [4], kind: 'sr', content: ['anime'],
    speed: 'fast', scenes: ['video', 'image'], vram_gb: 1.5, tile_hint: 0, engine: 'onnx',
    description: '轻量 CNN，低配显卡也能跑得动的动漫 x4',
    installed: false, bundled: false, size_mb: 24, vram_ok: true,
  },
  {
    id: 'swinir-x4', name: 'SwinIR-real x4', scale: [4], kind: 'sr', content: ['real'],
    speed: 'slow', scenes: ['video', 'image'], vram_gb: 8, tile_hint: 192, engine: 'onnx',
    description: '实拍修复画质天花板之一，代价是显存与速度',
    installed: false, bundled: false, size_mb: 512, vram_ok: false,
    vram_note: '推荐 ≥12GB 显存；本机 12GB 可跑但需小分块',
  },
  {
    id: 'rife-v4', name: 'RIFE 补帧 x2', scale: [2], kind: 'interp', content: ['anime', 'real'],
    speed: 'fast', scenes: [], vram_gb: 2, tile_hint: 0, engine: 'onnx',
    description: '帧率翻倍插帧模型，随「补帧」选项自动启用',
    installed: true, bundled: true, size_mb: 80, vram_ok: true,
  },
]

const PRESETS = [
  {
    id: 'p-anime4k', name: '动漫 4K', icon: '🎞️', desc: 'AnimeJaNai x4 + HEVC 高画质',
    model_id: 'anj-hd-x4', target_scale: 4, codec: 'hevc_nvenc', crf: 18,
  },
  {
    id: 'p-oldfilm', name: '老片修复', icon: '📼', desc: 'RealESR x4 + 去色带 + 反交错',
    model_id: 'realesr-x4', target_scale: 4, codec: 'hevc_nvenc', crf: 17, deband: true, deinterlace: true,
  },
  {
    id: 'p-fast', name: '极速预览', icon: '⚡', desc: 'x2 快速看效果',
    model_id: 'anj-hd-x2', target_scale: 2, codec: 'h264_nvenc', crf: 22,
  },
  {
    id: 'p-mine', name: '我的 B 站投稿', icon: '⭐', desc: '用户自定义',
    model_id: 'anj-hd-x4', target_scale: 4, codec: 'hevc_nvenc', crf: 20, user: true,
  },
]

// ---------------- 造数：任务（运行中那条会被 WS 泵推进） ----------------

const RUN_ID = 't-run'
let runFrames = 9860
const RUN_TOTAL = 21600

function buildTasks(): unknown[] {
  return [
    {
      id: RUN_ID,
      input_path: 'D:\\videos\\anime_ep01_1080p.mkv',
      output_path: 'D:\\output\\anime_ep01_4K.mkv',
      model_id: 'anj-hd-x4',
      params: { scale: 4, codec: 'hevc_nvenc', crf: 18, interp: 'off' },
      status: 'running',
      src_w: 1920, src_h: 1080, fps: 23.976,
      total_frames: RUN_TOTAL, progress_frames: runFrames,
      fps_run: 12.4, fps_avg: 0, eta_sec: Math.max(0, Math.round((RUN_TOTAL - runFrames) / 12.4)),
      error: null, preview_path: 'x', preview_src: 'x',
      out_bytes: 0, elapsed_s: 0, queue_position: null, updated_at: now(), input_exists: true,
    },
    {
      id: 't-q1',
      input_path: 'D:\\videos\\老电影_1987_修复版.mp4',
      output_path: 'D:\\output\\老电影_1987_4K.mp4',
      model_id: 'realesr-x4',
      params: { scale: 4, codec: 'hevc_nvenc', crf: 17, deband: true },
      status: 'queued',
      src_w: 960, src_h: 540, fps: 24,
      total_frames: 129600, progress_frames: 0,
      fps_run: 0, fps_avg: 0, eta_sec: 0,
      error: null, preview_path: null, preview_src: null,
      out_bytes: 0, elapsed_s: 0, queue_position: 1, updated_at: now() - 300, input_exists: true,
    },
    {
      id: 't-q2',
      input_path: 'D:\\videos\\vlog_tokyo_night.mp4',
      output_path: 'D:\\output\\vlog_tokyo_night_4K.mp4',
      model_id: 'realesr-x4',
      params: { scale: 4, codec: 'hevc_nvenc', crf: 18, interp: 'rife2x' },
      status: 'queued',
      src_w: 1920, src_h: 1080, fps: 30,
      total_frames: 54000, progress_frames: 0,
      fps_run: 0, fps_avg: 0, eta_sec: 0,
      error: null, preview_path: null, preview_src: null,
      out_bytes: 0, elapsed_s: 0, queue_position: 2, updated_at: now() - 240, input_exists: true,
    },
    {
      id: 't-done1',
      input_path: 'D:\\videos\\concert_2008.mp4',
      output_path: 'D:\\output\\concert_2008_4K.mp4',
      model_id: 'realesr-x4',
      params: { scale: 4, codec: 'hevc_nvenc', crf: 17, interp: 'rife2x' },
      status: 'done',
      src_w: 640, src_h: 360, fps: 25,
      total_frames: 86400, progress_frames: 86400,
      fps_run: 0, fps_avg: 9.83, eta_sec: 0,
      error: null, preview_path: 'x', preview_src: 'x', has_sr_log: true,
      out_bytes: 4294967296, elapsed_s: 8790, queue_position: null, updated_at: now() - 7200, input_exists: true,
    },
    {
      id: 't-done2',
      input_path: 'D:\\videos\\sakura_amv.mov',
      output_path: 'D:\\output\\sakura_amv_4K.mkv',
      model_id: 'anj-hd-x4',
      params: { scale: 4, codec: 'hevc_nvenc', crf: 16, container: 'mkv' },
      status: 'done',
      src_w: 1280, src_h: 720, fps: 23.976,
      total_frames: 12600, progress_frames: 12600,
      fps_run: 0, fps_avg: 14.2, eta_sec: 0,
      error: null, preview_path: 'x', preview_src: 'x', has_sr_log: false,
      out_bytes: 1825361100, elapsed_s: 887, queue_position: null, updated_at: now() - 14400, input_exists: true,
    },
    {
      id: 't-img',
      input_path: 'D:\\pics\\comic_p01.png',
      output_path: 'D:\\pics\\upscaled',
      model_id: 'mangajanai-x4',
      params: { kind: 'image', images: [{ in: 'D:\\pics\\comic_p01.png' }, { in: 'D:\\pics\\comic_p02.png' }, { in: 'D:\\pics\\comic_p03.png' }], scale: 4, out_kind: 'png' },
      status: 'done',
      src_w: 1400, src_h: 2100, fps: 0,
      total_frames: 3, progress_frames: 3,
      fps_run: 0, fps_avg: 0.42, eta_sec: 0,
      error: null, preview_path: 'x', preview_src: 'x',
      out_bytes: 98566200, elapsed_s: 7, queue_position: null, updated_at: now() - 86400, input_exists: true,
    },
    {
      id: 't-fail',
      input_path: 'D:\\videos\\documentary_10bit.mov',
      output_path: 'D:\\output\\documentary_4K.mp4',
      model_id: 'swinir-x4',
      params: { scale: 4, codec: 'hevc_nvenc', crf: 18 },
      status: 'failed',
      src_w: 1920, src_h: 1080, fps: 29.97,
      total_frames: 108000, progress_frames: 3412,
      fps_run: 0, fps_avg: 0, eta_sec: 0,
      error: 'ONNX Runtime 错误：DirectML 显存分配失败 (0x8007000E)，分块已自动降至 128 仍不足。\n建议：设置 → 高级 中将分块调小，或换用显存需求更低的模型。',
      preview_path: null, preview_src: null,
      out_bytes: 0, elapsed_s: 246, queue_position: null, updated_at: now() - 43200, input_exists: true,
    },
    {
      // 失败的混装漫画任务：走查「改参数重试」按类型分流到漫画超分页（folder_src 重建 + 双模型回填）
      id: 't-manga-fail',
      input_path: 'D:\\manga\\naruto_vol01',
      output_path: 'D:\\manga\\naruto_vol01_2x\\naruto_vol01.pdf',
      model_id: 'mangajanai-x4',
      params: {
        kind: 'manga', scale: 2, target_scale: 2, format: 'png',
        folder_src: 'D:\\manga\\naruto_vol01', merge_pdf: true,
        model_id_color: 'illustrationjanai-x2',
        images: [
          { in: 'D:\\manga\\naruto_vol01\\ch01\\p001.png', out: 'D:\\manga\\naruto_vol01_2x\\ch01\\p001.png' },
          { in: 'D:\\manga\\naruto_vol01\\ch01\\p002.png', out: 'D:\\manga\\naruto_vol01_2x\\ch01\\p002.png', lane: 'color' },
        ],
      },
      status: 'failed',
      src_w: 1400, src_h: 2100, fps: 0,
      total_frames: 2, progress_frames: 1,
      fps_run: 0, fps_avg: 0, eta_sec: 0,
      error: '彩色页模型引擎加载失败：DirectML 会话创建异常 (0x8007000E)。',
      preview_path: null, preview_src: null,
      out_bytes: 0, elapsed_s: 18, queue_position: null, updated_at: now() - 3600, input_exists: true,
    },
  ]
}

// ---------------- 造数：SVG 占位图（预览/静帧/对比资源统一应答） ----------------

function svgFrame(seedStr: string, w = 480, h = 270): string {
  let seed = 0
  for (const ch of seedStr) seed = (seed * 31 + ch.charCodeAt(0)) % 997
  const hA = (seed * 137) % 360
  const hB = (hA + 70 + (seed % 60)) % 360
  const sunX = 60 + (seed % 260)
  const label = seedStr.length > 26 ? `${seedStr.slice(0, 26)}…` : seedStr
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${w} ${h}">
  <defs>
    <linearGradient id="sky" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="hsl(${hA},55%,38%)"/>
      <stop offset="1" stop-color="hsl(${hB},60%,13%)"/>
    </linearGradient>
  </defs>
  <rect width="${w}" height="${h}" fill="url(#sky)"/>
  <circle cx="${sunX}" cy="${h * 0.34}" r="26" fill="hsl(${hA},85%,72%)" opacity="0.85"/>
  <path d="M0 ${h * 0.72} L${w * 0.22} ${h * 0.44} L${w * 0.42} ${h * 0.68} L${w * 0.62} ${h * 0.4} L${w * 0.84} ${h * 0.7} L${w} ${h * 0.55} V${h} H0 Z" fill="hsl(${hB},45%,22%)"/>
  <path d="M0 ${h * 0.85} L${w * 0.3} ${h * 0.6} L${w * 0.55} ${h * 0.82} L${w * 0.78} ${h * 0.58} L${w} ${h * 0.78} V${h} H0 Z" fill="hsl(${hB},50%,15%)"/>
  <text x="14" y="${h - 12}" font-family="system-ui,sans-serif" font-size="13" fill="#ffffff" opacity="0.75">${label}</text>
</svg>`
}

function svgResponse(seed: string, w?: number, h?: number): Response {
  return new Response(svgFrame(seed, w, h), {
    status: 200,
    headers: { 'Content-Type': 'image/svg+xml' },
  })
}

// ---------------- 造数：性能采样 ----------------

let cpuWalk = 46
let gpuWalk = 82

function perfSample(t: number): unknown {
  cpuWalk = Math.min(92, Math.max(14, cpuWalk + (Math.random() * 14 - 7)))
  gpuWalk = Math.min(99, Math.max(38, gpuWalk + (Math.random() * 10 - 5)))
  return {
    t,
    cpu: Math.round(cpuWalk * 10) / 10,
    mem_pct: 57.3,
    mem_used_gb: 18.3,
    gpus: [{ util: Math.round(gpuWalk), mem_used_mb: 8150 + Math.round(Math.random() * 300), mem_total_mb: 12282 }],
    task: { task_id: RUN_ID, cpu_pct: 31.5, mem_gb: 4.2, n_proc: 2 },
  }
}

function perfHistory(): unknown {
  const samples: unknown[] = []
  const t0 = now() - 3600
  for (let i = 0; i < 240; i++) samples.push(perfSample(t0 + i * 15))
  return { interval_s: 2, samples }
}

// ---------------- HTTP 路由 ----------------

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function route(rawUrl: string, init?: RequestInit): Promise<Response> {
  const url = new URL(rawUrl)
  const path = url.pathname
  const method = (init?.method ?? 'GET').toUpperCase()
  const seg = path.split('/').filter(Boolean) // ['api', ...]

  // 资源类（<img>/<video> 直引）：返回 SVG 占位图
  if (/^\/api\/tasks\/[^/]+\/preview$/.test(path)) return Promise.resolve(svgResponse(path.split('/')[3]))
  if (/^\/api\/tasks\/[^/]+\/stills\/\d+$/.test(path)) return Promise.resolve(svgResponse(`${path}still`))
  if (/^\/api\/compare\/[^/]+\/asset\//.test(path)) return Promise.resolve(svgResponse(path))

  const r: Promise<Response> = (() => {
    if (method === 'GET' && path === '/api/models') return Promise.resolve(json(MODELS))
    if (method === 'GET' && path === '/api/presets') return Promise.resolve(json(PRESETS))
    if (method === 'POST' && path === '/api/presets') return Promise.resolve(json({ id: `p-${Date.now()}` }, 201))
    if (method === 'GET' && path === '/api/hardware') {
      return Promise.resolve(json({
        gpus: [{ name: 'NVIDIA GeForce RTX 4070 Ti', vram_gb: 12 }],
        cpu: '13th Gen Intel Core i7-13700K', cpu_cores: 24, ram_gb: 32,
        nvenc: true, av1_nvenc: true, nvdec: true, d3d11va: true, svt_av1: true,
      }))
    }
    if (method === 'GET' && path === '/api/engine') {
      return Promise.resolve(json({
        backend: 'trt', python: '3.11.9', detail: 'TensorRT 10.12 · CUDA 12.8',
        running: { backend: 'trt', detail: 'TensorRT 10.12 · CUDA 12.8' },
      }))
    }
    if (method === 'GET' && path === '/api/settings')
      return Promise.resolve(json({ update_source: 'auto', mcp_enabled: true }))
    if (method === 'PUT' && path === '/api/settings') return Promise.resolve(json({ ok: true }))
    if (method === 'GET' && path === '/api/tasks') {
      const q = (url.searchParams.get('q') ?? '').toLowerCase()
      const list = buildTasks()
      return Promise.resolve(json(
        q
          ? list.filter((t) => JSON.stringify(t).toLowerCase().includes(q))
          : list,
      ))
    }
    if (method === 'POST' && path === '/api/tasks/output-path') {
      const body = JSON.parse(String(init?.body ?? '{}'))
      const stem = String(body.input ?? '').replace(/\.[^.\\/]+$/, '')
      const params = body.params ?? {}
      return Promise.resolve(json({ output: params.out_kind && params.out_kind !== 'video'
        ? `${stem}_${params.target_scale ?? 2}x_frames`
        : `${stem}_${params.target_scale ?? 2}x.${params.container ?? 'mp4'}` }))
    }
    if (method === 'POST' && path === '/api/watermark/preview') {
      const body = JSON.parse(String(init?.body ?? '{}'))
      const m = body.mask
      const sx = m.unit === 'percent' ? 11 : 1
      const sy = m.unit === 'percent' ? 16.45 : 1
      const right = 1100 - Math.round(m.right * sx), bottom = 1645 - Math.round(m.bottom * sy)
      const x = right - Math.ceil(m.width * sx - 1e-9), y = bottom - Math.ceil(m.height * sy - 1e-9)
      if (x < 0 || y < 0) return Promise.resolve(json({ detail: '填白区域超出图片范围（1100 × 1645）' }, 400))
      const svg = (fill: boolean) => 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(
        `<svg xmlns="http://www.w3.org/2000/svg" width="1100" height="1645" viewBox="0 0 1100 1645"><rect width="1100" height="1645" fill="white"/><g fill="#eee" stroke="#222" stroke-width="5"><rect x="60" y="100" width="470" height="550"/><rect x="550" y="100" width="470" height="550"/><rect x="60" y="670" width="960" height="420"/><rect x="60" y="1110" width="960" height="440"/></g><g font-family="sans-serif" text-anchor="middle" fill="#555" font-size="36"><text x="295" y="380">漫画页预览示例</text><text x="785" y="380">保持画框与线条</text><text x="550" y="890">在左图拖动框选填白区域</text></g><text x="945" y="1618" fill="#aaa" font-size="30">示例水印</text>${fill ? `<rect x="${x}" y="${y}" width="${right - x}" height="${bottom - y}" fill="white"/>` : ''}</svg>`,
      )
      return Promise.resolve(json({ width: 1100, height: 1645, box: [x, y, right, bottom], original: svg(false), processed: svg(true),
        detected: body.mode === 'smart' && body.sample ? true : null,
        score: body.mode === 'smart' && body.sample ? .99 : null, reason: '', method: 'white' }))
    }
    if (method === 'POST' && path === '/api/watermark/batch') {
      const body = JSON.parse(String(init?.body ?? '{}'))
      return Promise.resolve(json({ id: 'watermark-demo', status: 'done', total: body.paths.length,
        completed: body.paths.length, succeeded: body.paths.length, failed: 0, elapsed_s: 1.2,
        skipped: 0, mode: body.mode ?? 'fixed', removal: body.removal ?? 'white', current: '', output_dir: 'D:\\output\\图片_去水印', errors: [] }))
    }
    if (path.startsWith('/api/watermark/batch/')) {
      return Promise.resolve(json({ id: 'watermark-demo', status: 'done', total: 3,
        completed: 3, succeeded: 3, failed: 0, elapsed_s: 1.2,
        current: '', output_dir: 'D:\\output\\图片_去水印', errors: [] }))
    }
    if (method === 'POST' && path === '/api/tasks') return Promise.resolve(json({ id: `t-${Date.now()}` }, 201))
    if (method === 'POST' && path === '/api/images/scan') {
      // 图片超分·文件夹模式：假漫画目录树（封面 + 两话子目录）
      const body = JSON.parse(String(init?.body ?? '{}')) as { folder?: string }
      const f = body.folder ?? 'D:\\manga'
      const rels = ['ch01/p001.jpg', 'ch01/p002.jpg', 'ch02/p001.jpg', 'ch02/p002.jpg', 'cover.jpg']
      return Promise.resolve(json({
        folder: f, total: rels.length, dirs: 2,
        files: rels.map((rel) => ({ path: `${f}\\${rel.replace(/\//g, '\\')}`, rel })),
      }))
    }
    if (method === 'POST' && path === '/api/tasks/batch') {
      const body = JSON.parse(String(init?.body ?? '{}')) as { ids?: string[] }
      return Promise.resolve(json({ ok: true, done: body.ids ?? [], failed: {} }))
    }
    if (method === 'POST' && path === '/api/tasks/reorder') return Promise.resolve(json({ ok: true }))
    if (method === 'GET' && path === '/api/stats') {
      return Promise.resolve(json({
        total: 47, done: 39, frames: 1823405, bytes: 83751800000,
        queue_gate: { active: true, reason: '' },
      }))
    }
    if (method === 'GET' && path === '/api/perf/history') return Promise.resolve(json(perfHistory()))
    if (method === 'GET' && path === '/api/trt-component') {
      return Promise.resolve(json({
        installed: true, version: 1, ort: '1.20.1', trt: '10.12.0', python: '3.11.9',
        size_bytes: 1503238553, gpu_arch: 'sm_89',
        assets: { version: 1, python: '3.11.9', ort: '1.20.1', trt: '10.12.0', assets: {} },
        installing: false, phase: null, file: '', done: 0, total: 0, error: null, source: null,
      }))
    }
    if (method === 'GET' && path.startsWith('/api/log-tail')) {
      const lines = [
        '[2026-09-04 10:12:01] sidecar 启动，监听 127.0.0.1:18923',
        '[2026-09-04 10:12:02] 引擎探测：TensorRT 10.12 · CUDA 12.8 可用',
        '[2026-09-04 10:12:02] 已装模型 4 个，内置 4 个',
        '[2026-09-04 10:15:44] 任务 t-run 启动：anime_ep01_1080p.mkv → x4',
        '[2026-09-04 10:15:47] 引擎装配完成（TRT 反序列化 6.2s），开始推理',
        '[2026-09-04 10:16:00] 进度 9860/21600 · 12.4 fps · ETA 15m49s',
      ]
      return Promise.resolve(json({ lines }))
    }
    if (method === 'POST' && path === '/api/probe') {
      return Promise.resolve(json({
        ok: true, error: null, width: 1920, height: 1080, fps: 23.976,
        duration_s: 1420.5, total_frames: 34069, codec: 'h264', pix_fmt: 'yuv420p',
        has_audio: true, audio_tracks: ['aac 2.0'], subtitles: [],
        bit_depth: 8, vfr: false, field_order: 'progressive',
        decoder: { nvdec: true, d3d11va: true },
        recommend: {
          model_id: DEFAULT_ANIME_MODEL, model_name: MODELS[0].name, target_scale: 2,
          deinterlace: false, deband: false, interp: 'off', animated: true,
          reasons: ['采样 4 帧中平坦区域占 83%，素材更接近动画内容（大色块平涂）',
            '源高 1080px，推荐 x2（→ 2160p）', '源帧率 23.98，如需更流畅可手动开启 RIFE 补帧（默认不自动开）'],
        },
      }))
    }
    if (method === 'POST' && path === '/api/models/import') return Promise.resolve(json({ ok: true }))
    if (method === 'GET' && path === '/api/compare/cache') return Promise.resolve(json({ jobs: 3, bytes: 2362232012 }))
    if (method === 'DELETE' && path === '/api/compare/cache') return Promise.resolve(json({ removed_jobs: 3, freed_bytes: 2362232012 }))
    if (method === 'GET' && path === '/api/queue-done/cancel') return Promise.resolve(json({ ok: true }))

    // /api/tasks/:id/* 子路由
    if (seg[1] === 'tasks' && seg.length >= 3) {
      const id = seg[2]
      if (method === 'DELETE' && seg.length === 3) return Promise.resolve(json({ ok: true }))
      if (method === 'POST' && (seg[3] === 'cancel' || seg[3] === 'resume')) return Promise.resolve(json({ ok: true }))
      if (method === 'GET' && seg[3] === 'stills' && seg.length === 4) {
        return Promise.resolve(json({ status: 'ready', count: 4, built_at: now() - 3600, error: null }))
      }
      if (method === 'GET' && seg[3] === 'sr-log') {
        return Promise.resolve(new Response(
          ['task=t-' + id, 'engine=trt tile=256 batch=4', 'load=6.21s warmup=1.02s', 'infer avg=80.6ms/frame p95=112ms', 'encode=hevc_nvenc 18crf', 'total=879.3s fps_avg=9.83'].join('\n'),
          { status: 200, headers: { 'Content-Type': 'text/plain; charset=utf-8' } },
        ))
      }
      if (method === 'POST' && seg[3] === 'share-card') {
        return Promise.resolve(json({ path: 'D:\\output\\share.png', kind: 'image', url: `${BASE}/api/tasks/${id}/share-card/file?kind=image&t=${now()}` }))
      }
    }
    if (/^\/api\/models\/[^/]+\/download$/.test(path) && method === 'POST') return Promise.resolve(json({ ok: true }))
    if (/^\/api\/models\/[^/]+$/.test(path) && method === 'DELETE') return Promise.resolve(json({ ok: true }))
    if (seg[1] === 'trt-component' && (method === 'POST' || method === 'DELETE')) return Promise.resolve(json({ ok: true }))
    if (seg[1] === 'compare' && method === 'POST' && seg.length === 2) {
      return Promise.resolve(json({
        id: 'cmp-demo', kind: 'video', input: 'D:\\videos\\anime_ep01_1080p.mkv',
        start_s: 12, end_s: 16, scale: 4, still_count: 4, status: 'done', error: null,
        entries: MODELS.slice(0, 3).map((m) => ({
          model_id: m.id, status: 'done', pct: 100, error: null, has_output: true,
          fps: 12.1, elapsed_s: 5.2, out_bytes: 12345678, out_w: 7680, out_h: 4320,
        })),
      }))
    }
    if (seg[1] === 'compare' && seg.length === 3 && method === 'GET') {
      return Promise.resolve(json({
        id: seg[2], kind: 'video', input: 'D:\\videos\\anime_ep01_1080p.mkv',
        start_s: 12, end_s: 16, scale: 4, still_count: 4, status: 'done', error: null,
        entries: MODELS.slice(0, 3).map((m) => ({
          model_id: m.id, status: 'done', pct: 100, error: null, has_output: true,
          fps: 12.1, elapsed_s: 5.2, out_bytes: 12345678, out_w: 7680, out_h: 4320,
        })),
      }))
    }
    if (seg[1] === 'trim' && method === 'POST' && seg.length === 2) {
      return Promise.resolve(json({ job_id: 'trim-demo', output: 'D:\\output\\cut.mp4' }, 201))
    }
    if (seg[1] === 'trim' && seg.length === 3 && method === 'GET') {
      return Promise.resolve(json({
        state: 'done', progress: 1, input: 'D:\\videos\\anime_ep01_1080p.mkv',
        start_s: 12, end_s: 32, mode: 'fast', output: 'D:\\output\\cut.mp4', error: null,
      }))
    }
    return Promise.resolve(json({ detail: `mock 未实现：${method} ${path}` }, 404))
  })()
  return r
}

// ---------------- WebSocket 替身 ----------------

class FakeWebSocket {
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  readyState = 0
  private timers: ReturnType<typeof setInterval>[] = []

  constructor(_url: string) {
    setTimeout(() => {
      this.readyState = 1
      this.onopen?.(new Event('open'))
      this.startPumps()
    }, 40)
  }

  private emit(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) } as MessageEvent)
  }

  private startPumps() {
    this.timers.push(
      setInterval(() => this.emit(perfSample(now())), 2000),
      setInterval(() => {
        runFrames += 12 + Math.floor(Math.random() * 8)
        if (runFrames >= RUN_TOTAL) runFrames = Math.round(RUN_TOTAL * 0.08) // 循环演示
        this.emit({
          type: 'progress', task_id: RUN_ID, frames: runFrames,
          fps: 11.8 + Math.random() * 1.6,
          eta_sec: Math.max(0, Math.round((RUN_TOTAL - runFrames) / 12.4)),
        })
      }, 1000),
    )
  }

  send(_data: unknown): void { /* noop */ }
  close(): void {
    this.timers.forEach(clearInterval)
    this.readyState = 3
    // 不触发 onclose：store 的断线重连在预览里没有意义
  }
}

// ---------------- window.sv 替身 ----------------

function installSvBridge(): void {
  window.sv = {
    backendInfo: () => Promise.resolve({ baseUrl: BASE, token: 'mock', mcpCommand: { command: 'C:\\Programs\\RainFrame\\resources\\sidecar\\sidecar.exe', args: ['mcp'] } }),
    appVersion: () => Promise.resolve('0.5.6-preview.1'),
    checkUpdate: () =>
      Promise.resolve({ status: 'available', current: '0.5.6-preview.1', version: '0.5.6', notes: '预览环境造数' }),
    downloadUpdate: () => Promise.resolve({ ok: true }),
    installUpdate: () => Promise.resolve(),
    updateState: () => Promise.resolve({ ready: '', downloading: false, source: 'github' }),
    setUpdateSource: () => {},
    onUpdateProgress: () => () => {},
    onUpdateReady: () => () => {},
    pickVideo: () => Promise.resolve(['D:\\videos\\anime_ep01_1080p.mkv']),
    pickImages: () => Promise.resolve(['D:\\pics\\comic_p01.png', 'D:\\pics\\comic_p02.png', 'D:\\pics\\comic_p03.png']),
    pickOutput: (suggest: string) => Promise.resolve(`D:\\output\\${suggest}`),
    pickModel: () => Promise.resolve('D:\\models\\my_custom.onnx'),
    saveLog: () => Promise.resolve(null),
    showInFolder: () => {},
    fsExists: () => Promise.resolve(true),
    pickDir: () => Promise.resolve('D:\\output'),
    openPath: () => Promise.resolve(),
    pathForFile: () => '',
    taskEvent: () => {},
    taskProgress: () => {},
    win: {
      minimize: () => {},
      toggleMaximize: () => {},
      close: () => {},
      setCloseToTray: () => {},
      onMaximized: () => () => {},
    },
    onNavigate: () => () => {},
  }
}

// ---------------- 安装 ----------------

const rawFetch = window.fetch.bind(window)
window.fetch = ((input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.startsWith(BASE)) return route(url, init)
  return rawFetch(input, init)
}) as typeof fetch

window.WebSocket = FakeWebSocket as unknown as typeof WebSocket
installSvBridge()
