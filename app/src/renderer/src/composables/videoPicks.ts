/** 视频文件挑选用共用逻辑：最近输入芯片 + 整页拖拽放置区。
 *  新建任务页与剪切页同款交互，此前两处逐行重复维护。 */
import { onMounted, ref } from 'vue'

export const VIDEO_EXT = /\.(mp4|mkv|mov|avi|webm|flv|ts|m4v|wmv)$/i

const RECENT_KEY = 'sv_recent_videos'

/** 最近输入（localStorage 双页共享；展示前过滤掉已不存在的文件） */
export function useRecentVideos(limit = 6) {
  const recents = ref<string[]>([])

  onMounted(async () => {
    try {
      const list = JSON.parse(localStorage.getItem(RECENT_KEY) ?? '[]')
      recents.value = Array.isArray(list) ? list.filter((x) => typeof x === 'string') : []
    } catch {
      recents.value = []
    }
    const ok: string[] = []
    for (const p of recents.value) {
      if (await window.sv.fsExists(p)) ok.push(p)
    }
    recents.value = ok.slice(0, limit)
  })

  function pushRecent(paths: string[]) {
    const list = [...paths, ...recents.value.filter((x) => !paths.includes(x))].slice(0, limit)
    recents.value = list
    try {
      localStorage.setItem(RECENT_KEY, JSON.stringify(list))
    } catch {
      /* 存储不可用只是少了快捷入口 */
    }
  }

  return { recents, pushRecent }
}

/** 整页拖拽放置区（dragDepth 计数防子元素进出闪烁；File.path 已移除，
 *  路径经 webUtils 取）。非视频文件忽略，命中时回调拿到视频路径列表。 */
export function useFileDrop(onFiles: (vids: string[]) => void) {
  const dragDepth = ref(0)
  function onDragEnter(e: DragEvent) {
    if (!e.dataTransfer?.types.includes('Files')) return
    dragDepth.value++
  }
  function onDragLeave() {
    dragDepth.value = Math.max(0, dragDepth.value - 1)
  }
  function onDropFiles(e: DragEvent) {
    dragDepth.value = 0
    const vids = [...(e.dataTransfer?.files ?? [])]
      .filter((f) => VIDEO_EXT.test(f.name))
      .map((f) => window.sv.pathForFile(f))
    if (vids.length) onFiles(vids)
  }
  return { dragDepth, onDragEnter, onDragLeave, onDropFiles }
}
