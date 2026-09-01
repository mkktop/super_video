/** sidecar HTTP/WS 客户端。baseUrl/token 由主进程注入（sidecar 端口动态探测，
 *  token 是本地 API 鉴权令牌——防浏览器里恶意网页直接打 127.0.0.1 的 drive-by）。 */
export let baseUrl = ''
let token = ''

/** 统一 fetch：自动带 token 头（401 时 sidecar 会拒绝无令牌请求） */
function _fetch(url: string, init?: RequestInit): Promise<Response> {
  const headers = new Headers(init?.headers)
  if (token) headers.set('X-SV-Token', token)
  return fetch(url, { ...init, headers })
}

/** 无需鉴权的资源地址（<img>/<video> 等无法带请求头）追加 token 查询参数 */
function withToken(url: string): string {
  return token ? `${url}${url.includes('?') ? '&' : '?'}token=${token}` : url
}

export function wsUrl(): string {
  return withToken(baseUrl.replace('http', 'ws') + '/ws')
}

/** 本地视频的 file:// 源地址（webSecurity:false 下 <video> 原生加载，
 * Chromium 文件加载器自带 Range 与 moov 尾部索引处理）。
 * 盘符段原样，其余逐段编码（#、?、中文路径安全）。 */
export function mediaSrc(p: string): string {
  return (
    'file:///' +
    p
      .replace(/\\/g, '/')
      .split('/')
      .map((s, i) => (i === 0 ? s : encodeURIComponent(s)))
      .join('/')
  )
}

export async function initBase(): Promise<void> {
  const info = await window.sv.backendInfo()
  baseUrl = info.baseUrl
  token = info.token ?? ''
}

export interface ModelInfo {
  id: string
  name: string
  scale: number[]
  kind?: 'sr' | 'interp'
  content: string[]
  speed: string
  vram_gb: number
  description: string
  engine?: 'onnx' | 'torch'
  tile_hint: number
  installed: boolean
  bundled?: boolean
  size_mb?: number
  vram_ok?: boolean
  vram_note?: string | null
  denoise_levels?: number[]
}

export interface Preset {
  id: string
  name: string
  icon: string
  desc: string
  model_id: string
  target_scale: number
  codec: string
  crf: number
  interp?: 'off' | 'rife2x'
  container?: 'mp4' | 'mkv' | 'mov'
  audio_mode?: string
  subtitle_mode?: string
  denoise?: number | null
  deinterlace?: boolean
  deband?: boolean
  /** 用户自定义预设（可删除）；内置预设无此标记 */
  user?: boolean
}

/** 智能推荐（probe recommend=true 附带）：规则引擎产出，见后端 recommend.py */
export interface RecommendInfo {
  model_id: string | null
  model_name: string
  target_scale: number | null
  deinterlace: boolean
  deband: boolean
  interp: 'off' | 'rife2x'
  animated: boolean | null
  reasons: string[]
}

/** 任务总量统计（首页四宫格）：不受任务列表历史上限影响的全量聚合 */
export interface Stats {
  total: number
  done: number
  frames: number
  bytes: number
  /** 处理时机闸门（定时/闲时）：false=队列挂起等待中，reason 为提示文案 */
  queue_gate?: { active: boolean; reason: string }
}

/** TRT 可选组件的资产描述（release 上的 7z 分包） */
export interface TrcPart {
  url: string
  size: number
  sha256: string
  raw?: number
}

export interface TrcStatus {
  installed: boolean
  version: number | null
  ort: string | null
  trt: string | null
  python: string | null
  size_bytes: number
  gpu_arch: string | null
  assets: { version: number; python: string; ort: string; trt: string; assets: Record<string, TrcPart> }
  installing: boolean
  phase: string | null // download | extract | done | error
  file: string
  done: number
  total: number
  error: string | null
}

export interface ProbeInfo {
  ok: boolean
  error: string | null
  width: number
  height: number
  fps: number
  duration_s: number
  total_frames: number
  codec: string
  pix_fmt: string
  has_audio: boolean
  audio_tracks?: string[]
  subtitles?: string[]
  /** 按本文件实测的硬解可用性（hwdecode=true 时附带） */
  decoder?: { nvdec: boolean; d3d11va: boolean }
  /** 智能推荐（recommend=true 时附带；源分析失败则缺省） */
  recommend?: RecommendInfo
}

export interface Task {
  id: string
  input_path: string
  output_path: string
  model_id: string
  params: Record<string, unknown>
  status: 'queued' | 'running' | 'done' | 'failed' | 'canceled'
  src_w: number
  src_h: number
  fps: number
  total_frames: number
  progress_frames: number
  fps_run: number
  /** 完成时落定的平均速度（总帧数÷本轮用时，端到端口径）；未完成为 0 */
  fps_avg: number
  eta_sec: number
  error: string | null
  preview_path: string | null
  preview_src?: string | null
  out_bytes: number
  elapsed_s: number
  queue_position: number | null
  updated_at: number
  /** 超分性能日志是否已落盘（sr_profiling 开启时完成的任务为 true） */
  has_sr_log?: boolean
}

/** 任务对比页多帧静帧状态（详见后端 sv/server/task_stills.py） */
export interface TaskStills {
  status: 'ready' | 'building' | 'failed' | 'unsupported'
  count: number
  built_at: number | null
  error: string | null
}

/** 模型对比作业（详见后端 sv/server/compare.py） */
export interface CompareEntry {
  model_id: string
  status: 'queued' | 'running' | 'done' | 'failed' | 'canceled'
  pct: number
  error: string | null
  has_output: boolean
  fps: number
  elapsed_s: number
  out_bytes: number
  out_w: number
  out_h: number
}

export interface CompareJob {
  id: string
  kind: 'image' | 'video'
  input: string
  start_s: number
  end_s: number
  scale: number
  /** 视频模式静帧样本数（4），图片模式恒为 1 */
  still_count: number
  status: 'queued' | 'running' | 'done' | 'failed' | 'canceled'
  error: string | null
  entries: CompareEntry[]
}

/** 对比产物资源地址（key: seg | src_still/<i> | out/<mid> | still/<mid>[/<i>]） */
export function compareAssetUrl(id: string, key: string): string {
  return withToken(`${baseUrl}/api/compare/${id}/asset/${key}`)
}

export interface TrimJob {
  state: 'queued' | 'running' | 'done' | 'failed' | 'canceled'
  progress: number
  input: string
  start_s: number
  end_s: number
  mode: string
  output: string
  error: string | null
  actual_start_s?: number
  duration_s?: number
  notices?: string[]
}

/** 性能采样:sidecar 2s 一拍,环形缓冲最近 1 小时(重启清零)。
 *  GPU 仅 NVIDIA 可采集,非 N 卡 gpus 为空数组。 */
export interface PerfGpu {
  util: number | null
  mem_used_mb: number
  mem_total_mb: number
}

export interface PerfTaskUsage {
  task_id: string
  cpu_pct: number
  mem_gb: number
  n_proc: number
}

export interface PerfSample {
  t: number
  cpu: number
  mem_pct: number
  mem_used_gb: number
  gpus: PerfGpu[]
  task: PerfTaskUsage | null
}

export const api = {
  async models(): Promise<ModelInfo[]> {
    return (await _fetch(`${baseUrl}/api/models`)).json()
  },
  async hardware(): Promise<Record<string, unknown>> {
    return (await _fetch(`${baseUrl}/api/hardware`)).json()
  },
  async engine(): Promise<{
    backend: string
    python: string
    detail: string
    /** 任务进行中才有：当前任务实际所用后端（设置热切换下一任务生效，可能与 backend 不同） */
    running?: { backend: string; detail: string }
  }> {
    return (await _fetch(`${baseUrl}/api/engine`)).json()
  },
  async presets(): Promise<Preset[]> {
    return (await _fetch(`${baseUrl}/api/presets`)).json()
  },
  /** 保存用户自定义预设（当前任务页参数的快照） */
  async createPreset(body: {
    name: string
    icon?: string
    desc?: string
    model_id: string
    target_scale: number
    codec: string
    crf: number
    container?: string
    audio_mode?: string
    subtitle_mode?: string
    interp?: string
    denoise?: number | null
    deinterlace?: boolean
    deband?: boolean
  }): Promise<Response> {
    return _fetch(`${baseUrl}/api/presets`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
  },
  async deletePreset(id: string): Promise<Response> {
    return _fetch(`${baseUrl}/api/presets/${id}`, { method: 'DELETE' })
  },
  async probe(path: string, hwdecode = false, recommend = false): Promise<Response> {
    return _fetch(`${baseUrl}/api/probe`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path, hwdecode, recommend }),
    })
  },
  async settings(): Promise<Record<string, unknown>> {
    return (await _fetch(`${baseUrl}/api/settings`)).json()
  },
  async saveSettings(body: Record<string, unknown>): Promise<Response> {
    return _fetch(`${baseUrl}/api/settings`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
  },
  async logTail(n = 120): Promise<{ lines: string[] }> {
    return (await _fetch(`${baseUrl}/api/log-tail?n=${n}`)).json()
  },
  async downloadModel(id: string): Promise<Response> {
    return _fetch(`${baseUrl}/api/models/${id}/download`, { method: 'POST' })
  },
  async deleteModel(id: string): Promise<Response> {
    return _fetch(`${baseUrl}/api/models/${id}`, { method: 'DELETE' })
  },
  async importModel(body: {
    path: string
    id: string
    name: string
    scale: number
    color: string
    value_range: string
    tile: number
  }): Promise<Response> {
    return _fetch(`${baseUrl}/api/models/import`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
  },
  async tasks(): Promise<Task[]> {
    return (await _fetch(`${baseUrl}/api/tasks`)).json()
  },
  async stats(): Promise<Stats> {
    return (await _fetch(`${baseUrl}/api/stats`)).json()
  },
  async perfHistory(): Promise<{ interval_s: number; samples: PerfSample[] }> {
    return (await _fetch(`${baseUrl}/api/perf/history`)).json()
  },
  async trtComponent(): Promise<TrcStatus> {
    return (await _fetch(`${baseUrl}/api/trt-component`)).json()
  },
  async installTrtComponent(): Promise<Response> {
    return _fetch(`${baseUrl}/api/trt-component/install`, { method: 'POST' })
  },
  async uninstallTrtComponent(): Promise<Response> {
    return _fetch(`${baseUrl}/api/trt-component`, { method: 'DELETE' })
  },
  async createTask(body: {
    input?: string
    inputs?: string[]
    output?: string
    model_id: string
    params: Record<string, unknown>
  }): Promise<Response> {
    return _fetch(`${baseUrl}/api/tasks`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
  },
  async cancel(id: string): Promise<Response> {
    return _fetch(`${baseUrl}/api/tasks/${id}/cancel`, { method: 'POST' })
  },
  async remove(id: string): Promise<Response> {
    return _fetch(`${baseUrl}/api/tasks/${id}`, { method: 'DELETE' })
  },
  async reorderTasks(ids: string[]): Promise<Response> {
    return _fetch(`${baseUrl}/api/tasks/reorder`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids }),
    })
  },
  async resume(id: string): Promise<Response> {
    return _fetch(`${baseUrl}/api/tasks/${id}/resume`, { method: 'POST' })
  },
  /** 超分性能日志文本（sr_profiling 开启时完成的任务才有；无则 404） */
  async srLog(id: string): Promise<string> {
    const r = await _fetch(`${baseUrl}/api/tasks/${id}/sr-log`)
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail ?? `HTTP ${r.status}`)
    return r.text()
  },
  previewUrl(id: string, updatedAt: number, src = false): string {
    return withToken(`${baseUrl}/api/tasks/${id}/preview?t=${updatedAt}${src ? '&src=1' : ''}`)
  },
  /** 任务对比页多帧静帧：状态（未构建时后端自动起线程，轮询到 ready） */
  async taskStills(id: string): Promise<TaskStills> {
    const r = await _fetch(`${baseUrl}/api/tasks/${id}/stills`)
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail ?? `HTTP ${r.status}`)
    return r.json()
  },
  /** 任务静帧样本资源地址（v=built_at：重建后强制刷新缩略图） */
  taskStillUrl(id: string, i: number, src: boolean, v: number | string): string {
    return withToken(`${baseUrl}/api/tasks/${id}/stills/${i}?v=${v}${src ? '&src=1' : ''}`)
  },
  /** 生成对比分享卡片（长图/滑块动图）；返回产物路径与可展示的 URL */
  async createShareCard(id: string, kind: 'image' | 'gif'): Promise<{ path: string; kind: string; url: string }> {
    const r = await _fetch(`${baseUrl}/api/tasks/${id}/share-card`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind }),
    })
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail ?? `HTTP ${r.status}`)
    return r.json()
  },
  /** 分享卡片产物的资源地址（<img> 无法带头，走 token 查询参数） */
  shareCardUrl(id: string, kind: 'image' | 'gif', bust: number): string {
    return withToken(`${baseUrl}/api/tasks/${id}/share-card/file?kind=${kind}&t=${bust}`)
  },
  async cancelTrim(id: string): Promise<Response> {
    return _fetch(`${baseUrl}/api/trim/${id}/cancel`, { method: 'POST' })
  },
  /** 取消"队列完成后关机/休眠"倒计时（无倒计时=幂等成功） */
  async cancelQueueAction(): Promise<{ ok: boolean }> {
    return (await _fetch(`${baseUrl}/api/queue-done/cancel`, { method: 'POST' })).json()
  },

  // ---- 模型对比 ----

  async createCompare(body: {
    kind: 'image' | 'video'
    input: string
    start_s?: number
    end_s?: number
    models: string[]
    scale: number
  }): Promise<CompareJob> {
    const r = await _fetch(`${baseUrl}/api/compare`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (!r.ok) throw new Error(`${(await r.json()).detail ?? r.status}`)
    return r.json()
  },
  async compareStatus(id: string): Promise<CompareJob> {
    return (await _fetch(`${baseUrl}/api/compare/${id}`)).json()
  },
  async cancelCompare(id: string): Promise<Response> {
    return _fetch(`${baseUrl}/api/compare/${id}/cancel`, { method: 'POST' })
  },
  /** 对比产物占用统计（设置页展示） */
  async compareCacheStats(): Promise<{ jobs: number; bytes: number }> {
    const r = await _fetch(`${baseUrl}/api/compare/cache`)
    if (!r.ok) throw new Error(`HTTP ${r.status}`)
    return r.json()
  },
  /** 清理全部对比产物（有作业进行中时后端 409 拒绝） */
  async clearCompareCache(): Promise<{ removed_jobs: number; freed_bytes: number }> {
    const r = await _fetch(`${baseUrl}/api/compare/cache`, { method: 'DELETE' })
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail ?? `HTTP ${r.status}`)
    return r.json()
  },
  async createTrim(body: {
    input: string
    start_s: number
    end_s: number
    mode: string
    output?: string
  }): Promise<{ job_id: string; output: string }> {
    const r = await _fetch(`${baseUrl}/api/trim`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (!r.ok) throw new Error((await r.json()).detail ?? `HTTP ${r.status}`)
    return r.json()
  },
  async trimStatus(id: string): Promise<TrimJob> {
    const r = await _fetch(`${baseUrl}/api/trim/${id}`)
    if (!r.ok) throw new Error(`HTTP ${r.status}`)
    return r.json()
  },
}
