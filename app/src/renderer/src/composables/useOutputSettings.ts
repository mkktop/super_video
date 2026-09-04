/** 输出位置与命名：全局输出目录 + 输出命名模板（新建任务页实时读取）。 */
import { computed, ref } from 'vue'
import { useMessage } from 'naive-ui'
import { api } from '../api'
import { store } from '../store'

export function useOutputSettings() {
  const message = useMessage()
  const outputDir = ref('') // 全局输出目录（空 = 源视频同目录）
  const savingOutDir = ref(false)
  const nameTemplate = ref('')
  const savingNameTpl = ref(false)

  function apply(s: Record<string, unknown>) {
    outputDir.value = String(s.output_dir ?? '').trim()
    nameTemplate.value = String(s.output_name_template ?? '')
  }

  /** 立即持久化输出目录并同步全局 store（新建任务页实时读取该值推导默认路径） */
  async function persistOutDir(v: string) {
    savingOutDir.value = true
    const r = await api.saveSettings({ output_dir: v })
    savingOutDir.value = false
    if (r.ok) {
      outputDir.value = v
      store.settings = { ...store.settings, output_dir: v }
      message.success(v ? '已保存，从下一个任务起生效' : '已恢复默认（源视频同目录）')
    } else {
      message.error(`保存失败: ${(await r.json()).detail ?? r.status}`)
    }
  }

  async function pickOutDir() {
    const p = await window.sv.pickDir()
    if (p) await persistOutDir(p)
  }

  function clearOutDir() {
    if (outputDir.value) void persistOutDir('')
  }

  function openOutDir() {
    if (outputDir.value) void window.sv.openPath(outputDir.value)
  }

  async function saveNameTemplate() {
    const v = nameTemplate.value.trim()
    if (/[<>:"/\\|?*]/.test(v)) {
      message.error('模板含文件名非法字符（<>:"/\\|?*）')
      return
    }
    savingNameTpl.value = true
    const r = await api.saveSettings({ output_name_template: v })
    savingNameTpl.value = false
    if (r.ok) {
      message.success(v ? '已保存，新任务按模板命名' : '已恢复默认命名（沿用原文件名）')
    } else {
      message.error(`保存失败: ${(await r.json()).detail ?? r.status}`)
    }
  }

  /** 长路径中段省略：保留盘符开头与末级文件夹名 */
  const outDirShown = computed(() => {
    const p = outputDir.value
    if (!p || p.length <= 48) return p
    return `${p.slice(0, 22)} … ${p.slice(-22)}`
  })

  return {
    outputDir, savingOutDir, nameTemplate, savingNameTpl, outDirShown,
    persistOutDir, pickOutDir, clearOutDir, openOutDir, saveNameTemplate, apply,
  }
}
