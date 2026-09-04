/** 应用更新域：通道/自动检查设置、检查/下载/安装动作与全部展示态派生。
 *  更新状态本体在全局 store.update（事件监听在 store 层注册，切页不丢）。 */
import { computed, ref } from 'vue'
import { useMessage } from 'naive-ui'
import { api } from '../api'
import { checkAppUpdate, store } from '../store'

export function useAppUpdate() {
  const message = useMessage()
  const checking = ref(false)
  const autoCheck = ref(true)
  const updateChannel = ref<'stable' | 'preview'>('stable') // 预览版可收到 -preview.N 预发布推送

  function apply(s: Record<string, unknown>) {
    autoCheck.value = s.auto_update_check !== false
    updateChannel.value = s.update_channel === 'preview' ? 'preview' : 'stable'
  }

  // 仅 available 状态下 version 才有效——latest 时后端也回传版本号(=当前版),不能据此显示下载按钮
  const updateVersion = computed(() => (store.update.status === 'available' ? store.update.version : ''))
  const updateNotes = computed(() => store.update.notes)
  const readyVersion = computed(() => store.update.ready)
  const downloading = computed(() => store.update.downloading)
  const downloadPercent = computed(() => store.update.percent)
  const updateMsg = computed(() => {
    const u = store.update
    if (u.ready) return `新版本 v${u.ready} 已下载完成，点击"立即重启"生效`
    if (u.downloading) {
      // 进度条下方明示当前下载源；切源（GitHub → R2 备用源）时进度回零重下，来源跟着变
      const src = u.downloadSource === 'r2' ? 'R2 备用源' : u.downloadSource === 'github' ? 'GitHub' : ''
      return src
        ? `正在从 ${src} 下载更新…（下载完成后可点击"立即重启"）`
        : '正在下载更新…（连接下载源中）'
    }
    if (u.downloadError) return `下载失败：${u.downloadError}`
    if (!u.checked) return ''
    if (u.status === 'dev') return '开发模式不检查更新（打包版自动检查 GitHub Releases）'
    if (u.status === 'available')
      return `发现新版本 v${u.version}，点击"下载更新"获取（全量安装包）${
        u.notes ? '（悬浮在"检查更新"上可查看更新内容）' : ''
      }`
    if (u.status === 'latest') return `已是最新版本（${u.current}）`
    // error 只出现在打包版（dev 模式走上面的 'dev' 分支）：真实网络/源问题，引导重试。
    // 旧文案「发布前属正常」是尚无 Release 时代的 dev 语境，正式版用户看到会误判
    if (u.status === 'error') return `检查失败：${u.error ?? '未知错误'}（请检查网络后重试）`
    return ''
  })

  /** 更新状态标签：挂在版本行右侧,一眼可辨 */
  const updateTag = computed(() => {
    if (readyVersion.value) return { text: `v${readyVersion.value} 已就绪`, type: 'success' as const }
    if (downloading.value) return { text: '下载中', type: 'info' as const }
    const u = store.update
    if (u.status === 'available') return { text: `可更新 v${u.version}`, type: 'warning' as const }
    if (u.status === 'latest' && u.checked) return { text: '已是最新', type: 'success' as const }
    return null
  })

  async function saveAutoCheck(v: boolean) {
    const r = await api.saveSettings({ auto_update_check: v })
    if (!r.ok) {
      message.error(`保存失败: ${(await r.json()).detail ?? r.status}`)
      autoCheck.value = !v
    }
  }

  async function saveUpdateChannel(v: 'stable' | 'preview') {
    const r = await api.saveSettings({ update_channel: v })
    if (!r.ok) {
      message.error(`保存失败: ${(await r.json()).detail ?? r.status}`)
      updateChannel.value = v === 'preview' ? 'stable' : 'preview'
      return
    }
    store.settings = { ...store.settings, update_channel: v }
    // 通道由主进程在检查时消费：先同步过去，再立即重查一次让用户看到新通道结果
    window.sv.setUpdateChannel(v)
    void checkUpdate()
  }

  async function checkUpdate() {
    checking.value = true
    try {
      await checkAppUpdate(true) // 手动检查允许切 R2 备用源；结果进 store.update,本页文案由 computed 派生
    } finally {
      checking.value = false
    }
  }

  async function doDownload() {
    store.update.downloading = true
    store.update.percent = 0
    store.update.downloadSource = '' // 首个进度事件到达前来源未知（连接下载源阶段）
    store.update.downloadError = ''
    const r = await window.sv.downloadUpdate()
    if (!r.ok) {
      store.update.downloading = false
      store.update.downloadError = r.error ?? '未知错误'
    }
    // 成功时 update-ready 事件会把 downloading 置 false 并写入 ready
  }

  function doInstall() {
    void window.sv.installUpdate() // 静默安装后自动拉起新版本
  }

  return {
    checking, autoCheck, updateChannel, updateVersion, updateNotes, readyVersion,
    downloading, downloadPercent, updateMsg, updateTag,
    saveAutoCheck, saveUpdateChannel, checkUpdate, doDownload, doInstall, apply,
  }
}
