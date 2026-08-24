/** sidecar HTTP/WS 客户端。baseUrl 由主进程注入（sidecar 端口动态探测）。 */
export let baseUrl = ''

export function wsUrl(): string {
  return baseUrl.replace('http', 'ws') + '/ws'
}

export async function initBase(): Promise<void> {
  const info = await window.sv.backendInfo()
  baseUrl = info.baseUrl
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
  tile_hint: number
  installed: boolean
  bundled?: boolean
  size_mb?: number
  vram_ok?: boolean
  vram_note?: string | null
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
  eta_sec: number
  error: string | null
  preview_path: string | null
  preview_src?: string | null
  out_bytes: number
  elapsed_s: number
  queue_position: number | null
  updated_at: number
}

export const api = {
  async models(): Promise<ModelInfo[]> {
    return (await fetch(`${baseUrl}/api/models`)).json()
  },
  async hardware(): Promise<Record<string, unknown>> {
    return (await fetch(`${baseUrl}/api/hardware`)).json()
  },
  async engine(): Promise<{ backend: string; python: string; detail: string }> {
    return (await fetch(`${baseUrl}/api/engine`)).json()
  },
  async presets(): Promise<Preset[]> {
    return (await fetch(`${baseUrl}/api/presets`)).json()
  },
  async probe(path: string): Promise<Response> {
    return fetch(`${baseUrl}/api/probe`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path }),
    })
  },
  async settings(): Promise<Record<string, unknown>> {
    return (await fetch(`${baseUrl}/api/settings`)).json()
  },
  async saveSettings(body: Record<string, unknown>): Promise<Response> {
    return fetch(`${baseUrl}/api/settings`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
  },
  async logTail(n = 120): Promise<{ lines: string[] }> {
    return (await fetch(`${baseUrl}/api/log-tail?n=${n}`)).json()
  },
  async downloadModel(id: string): Promise<Response> {
    return fetch(`${baseUrl}/api/models/${id}/download`, { method: 'POST' })
  },
  async deleteModel(id: string): Promise<Response> {
    return fetch(`${baseUrl}/api/models/${id}`, { method: 'DELETE' })
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
    return fetch(`${baseUrl}/api/models/import`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
  },
  async tasks(): Promise<Task[]> {
    return (await fetch(`${baseUrl}/api/tasks`)).json()
  },
  async createTask(body: {
    input: string
    output?: string
    model_id: string
    params: Record<string, unknown>
  }): Promise<Response> {
    return fetch(`${baseUrl}/api/tasks`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
  },
  async cancel(id: string): Promise<Response> {
    return fetch(`${baseUrl}/api/tasks/${id}/cancel`, { method: 'POST' })
  },
  async remove(id: string): Promise<Response> {
    return fetch(`${baseUrl}/api/tasks/${id}`, { method: 'DELETE' })
  },
  async reorderTasks(ids: string[]): Promise<Response> {
    return fetch(`${baseUrl}/api/tasks/reorder`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids }),
    })
  },
  async resume(id: string): Promise<Response> {
    return fetch(`${baseUrl}/api/tasks/${id}/resume`, { method: 'POST' })
  },
  previewUrl(id: string, updatedAt: number, src = false): string {
    return `${baseUrl}/api/tasks/${id}/preview?t=${updatedAt}${src ? '&src=1' : ''}`
  },
}
