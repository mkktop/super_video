/** sidecar HTTP/WS 客户端。baseUrl 由主进程注入（sidecar 端口动态探测）。 */
export let baseUrl = ''

export async function initBase(): Promise<void> {
  const info = await window.sv.backendInfo()
  baseUrl = info.baseUrl
}

export interface ModelInfo {
  id: string
  name: string
  scale: number[]
  content: string[]
  speed: string
  vram_gb: number
  description: string
  tile_hint: number
  installed: boolean
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
  out_bytes: number
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
  previewUrl(id: string, updatedAt: number): string {
    return `${baseUrl}/api/tasks/${id}/preview?t=${updatedAt}`
  },
}
