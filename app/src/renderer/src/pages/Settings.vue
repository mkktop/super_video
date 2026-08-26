<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  NButton,
  NInput,
  NPopover,
  NProgress,
  NRadioButton,
  NRadioGroup,
  NSelect,
  NSpace,
  NSwitch,
  NTag,
  useMessage,
} from 'naive-ui'
import { api } from '../api'
import { checkAppUpdate, refreshTrt, store } from '../store'

const message = useMessage()
const engine = ref<'auto' | 'cuda' | 'trt' | 'directml'>('auto')
const precision = ref<'fp16' | 'fp32'>('fp16')
const saving = ref(false)
const appVersion = ref('')
const checking = ref(false)
const proxyMode = ref<'auto' | 'direct' | 'custom'>('auto')
const proxyAddr = ref('')
const savingProxy = ref(false)
const perfSampling = ref(true)
const autoCheck = ref(true)
const trcBusy = ref(false)
const parallelStreams = ref(false)
const outputDir = ref('') // 全局输出目录（空 = 源视频同目录）
const savingOutDir = ref(false)
// 已保存的引擎/精度（脏状态对比用；保存成功后同步）
const savedEngine = ref<'auto' | 'cuda' | 'trt' | 'directml'>('auto')
const savedPrecision = ref<'fp16' | 'fp32'>('fp16')
const proxyOptions = [
  { label: '跟随系统代理', value: 'auto' },
  { label: '直连（不走代理）', value: 'direct' },
  { label: '自定义代理', value: 'custom' },
]
// 更新状态全部从全局 store 派生：事件监听在 store 层注册，
// 切页/重进设置页不丢"已下载待安装"与下载进度
// 仅 available 状态下 version 才有效——latest 时后端也回传版本号(=当前版),不能据此显示下载按钮
const updateVersion = computed(() => (store.update.status === 'available' ? store.update.version : ''))
const updateNotes = computed(() => store.update.notes)
const readyVersion = computed(() => store.update.ready)
const downloading = computed(() => store.update.downloading)
const downloadPercent = computed(() => store.update.percent)
const updateMsg = computed(() => {
  const u = store.update
  if (u.ready) return `新版本 v${u.ready} 已下载完成，点击"立即重启"生效`
  if (u.downloading) return '正在下载更新…（下载完成后可点击"立即重启"）'
  if (u.downloadError) return `下载失败：${u.downloadError}`
  if (!u.checked) return ''
  if (u.status === 'dev') return '开发模式不检查更新（打包版自动检查 GitHub Releases）'
  if (u.status === 'available')
    return `发现新版本 v${u.version}，点击"下载更新"获取（全量安装包）${
      u.notes ? '（悬浮在"检查更新"上可查看更新内容）' : ''
    }`
  if (u.status === 'latest') return `已是最新版本（${u.current}）`
  if (u.status === 'error') return `检查失败：${u.error ?? '未知错误'}（发布前属正常，见 README 发布流程）`
  return ''
})
const engineDirty = computed(() => engine.value !== savedEngine.value || precision.value !== savedPrecision.value)
const backendLabel = computed(() => {
  const b = store.engine?.backend
  if (b === 'trt') return 'CUDA + TensorRT'
  if (b === 'cuda') return 'CUDA'
  return 'DirectML'
})
const backendType = computed(() =>
  store.engine?.backend === 'trt' || store.engine?.backend === 'cuda' ? 'success' : 'default',
)
/** 更新状态标签：挂在版本行右侧,一眼可辨 */
const updateTag = computed(() => {
  if (readyVersion.value) return { text: `v${readyVersion.value} 已就绪`, type: 'success' as const }
  if (downloading.value) return { text: '下载中', type: 'info' as const }
  const u = store.update
  if (u.status === 'available') return { text: `可更新 v${u.version}`, type: 'warning' as const }
  if (u.status === 'latest' && u.checked) return { text: '已是最新', type: 'success' as const }
  return null
})
/** 硬件编码能力汇总（无任何硬编时提示软编兜底） */
const encSummary = computed(() => {
  const h = store.hardware
  if (!h) return ''
  const parts: string[] = []
  if (h.nvenc) parts.push('NVENC H.264')
  if (h.av1_nvenc) parts.push('NVENC AV1')
  if (h.amf) parts.push('AMF')
  if (h.svt_av1) parts.push('SVT-AV1（软件）')
  return parts.length ? parts.join(' · ') : '无（使用软件编码）'
})
/** 长路径中段省略：保留盘符开头与末级文件夹名 */
const outDirShown = computed(() => {
  const p = outputDir.value
  if (!p || p.length <= 48) return p
  return `${p.slice(0, 22)} … ${p.slice(-22)}`
})

onMounted(async () => {
  const s = (await api.settings()) as {
    engine?: 'auto' | 'cuda' | 'trt' | 'directml'
    precision?: 'fp16' | 'fp32'
    download_proxy?: string
    perf_sampling?: boolean
    auto_update_check?: boolean
    output_dir?: string
    parallel_streams?: boolean
  }
  engine.value = s.engine ?? 'auto'
  precision.value = s.precision ?? 'fp16'
  parallelStreams.value = s.parallel_streams === true
  perfSampling.value = s.perf_sampling !== false
  autoCheck.value = s.auto_update_check !== false
  outputDir.value = String(s.output_dir ?? '').trim()
  const p = s.download_proxy ?? ''
  if (p === 'direct') proxyMode.value = 'direct'
  else if (p.startsWith('http')) {
    proxyMode.value = 'custom'
    proxyAddr.value = p
  } else proxyMode.value = 'auto'
  appVersion.value = await window.sv.appVersion()
  savedEngine.value = engine.value
  savedPrecision.value = precision.value
  refreshTrt()
})

async function saveProxy() {
  const v = proxyMode.value === 'direct' ? 'direct'
    : proxyMode.value === 'custom' ? proxyAddr.value.trim() : ''
  if (proxyMode.value === 'custom' && !/^https?:\/\//.test(v)) {
    message.error('代理地址需以 http:// 或 https:// 开头')
    return
  }
  savingProxy.value = true
  const r = await api.saveSettings({ download_proxy: v })
  savingProxy.value = false
  if (r.ok) {
    message.success('已保存，新的模型下载立即生效')
  } else {
    message.error(`保存失败: ${(await r.json()).detail ?? r.status}`)
  }
}

async function savePerfSampling(v: boolean) {
  const r = await api.saveSettings({ perf_sampling: v })
  if (!r.ok) {
    message.error(`保存失败: ${(await r.json()).detail ?? r.status}`)
    perfSampling.value = !v
  }
}

async function saveAutoCheck(v: boolean) {
  const r = await api.saveSettings({ auto_update_check: v })
  if (!r.ok) {
    message.error(`保存失败: ${(await r.json()).detail ?? r.status}`)
    autoCheck.value = !v
  }
}

async function checkUpdate() {
  checking.value = true
  try {
    await checkAppUpdate() // 结果进 store.update,本页文案由 computed 派生
  } finally {
    checking.value = false
  }
}

async function doDownload() {
  store.update.downloading = true
  store.update.percent = 0
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

async function saveEngine() {
  saving.value = true
  const r = await api.saveSettings({ engine: engine.value, precision: precision.value })
  saving.value = false
  if (r.ok) {
    savedEngine.value = engine.value
    savedPrecision.value = precision.value
    message.success('已保存，从下一个任务起生效')
    store.engine = await api.engine()
  } else {
    message.error(`保存失败: ${(await r.json()).detail ?? r.status}`)
  }
}

async function saveParallel(v: boolean) {
  const r = await api.saveSettings({ parallel_streams: v })
  if (!r.ok) {
    message.error(`保存失败: ${(await r.json()).detail ?? r.status}`)
    parallelStreams.value = !v
  } else {
    message.success(v ? '已开启，从下一个任务起生效' : '已关闭')
  }
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

// ---- TRT 可选组件 ----

function fmtGB(b: number): string {
  return (b / 1e9).toFixed(2) + ' GB'
}

/** 安装需下载的体积（core + 匹配架构的 builder 包） */
const trcDownloadBytes = computed(() => {
  const t = store.trt
  if (!t || t.installed || t.installing) return 0
  const a = t.assets?.assets ?? {}
  const core = a.core?.size ?? 0
  const key = t.gpu_arch && a[`builder-${t.gpu_arch}`] ? `builder-${t.gpu_arch}` : 'builder-ptx'
  return core + (a[key]?.size ?? 0)
})

async function installTrc() {
  trcBusy.value = true
  const r = await api.installTrtComponent()
  if (!r.ok) message.error(`无法开始安装: ${(await r.json()).detail ?? r.status}`)
  // 成功则进度走 WS trt_component 事件;失败恢复按钮
  trcBusy.value = r.ok
}

async function uninstallTrc() {
  trcBusy.value = true
  const r = await api.uninstallTrtComponent()
  trcBusy.value = false
  if (r.ok) {
    message.success('已卸载，推理回退 DirectML')
    await refreshTrt()
  } else {
    message.error(`${(await r.json()).detail ?? r.status}`)
  }
}
</script>

<template>
  <div class="settings-page">
    <div class="page-head">
      <h1>设置</h1>
    </div>

    <!-- 固定两列各排各的（高低卡搭配），宽窗口卡片变宽而不是多挤出几列窄条 -->
    <div class="settings-cols">
      <!-- 左列：内容最长的三张 -->
      <div class="col">
        <!-- 处理引擎 -->
        <section class="card">
          <header class="card-head">
            <div class="card-title">处理引擎</div>
            <div class="card-sub">推理后端与计算精度，影响画质细节的还原方式</div>
          </header>
          <div class="card-body">
            <div class="row">
              <span class="row-label">当前后端</span>
              <span class="backend-status">
                <NTag size="small" :bordered="false" :type="backendType">{{ backendLabel }}</NTag>
                <span v-if="store.engine?.detail" class="hint-inline">{{ store.engine.detail }}</span>
              </span>
            </div>
            <div class="row stack">
              <span class="row-label">推理后端</span>
              <NRadioGroup v-model:value="engine" size="small">
                <NRadioButton value="auto">自动</NRadioButton>
                <NRadioButton value="directml">DirectML</NRadioButton>
                <NRadioButton value="cuda">CUDA</NRadioButton>
                <NRadioButton value="trt">TensorRT</NRadioButton>
              </NRadioGroup>
            </div>
            <p class="hint">DirectML 兼容所有显卡；CUDA / TensorRT 仅限 NVIDIA 显卡。TensorRT 需安装下方加速组件，未安装时自动回退 DirectML。</p>
            <div class="row stack">
              <span class="row-label">计算精度</span>
              <NRadioGroup v-model:value="precision" size="small">
                <NRadioButton value="fp16">FP16（推荐）</NRadioButton>
                <NRadioButton value="fp32">FP32</NRadioButton>
              </NRadioGroup>
            </div>
            <p class="hint">FP16 处理速度约提升 1.4~1.7 倍，画质无可感知差异；FP32 供个别模型出现数值异常时使用。</p>
            <div class="row switch-row bordered-top">
              <span class="row-text">
                双路并行
                <small>两个进程分段同时处理，提升 GPU 利用率；配合硬件编码器效果最佳。显存占用约增加一倍，低显存设备建议关闭</small>
              </span>
              <NSwitch v-model:value="parallelStreams" size="small" @update:value="saveParallel" />
            </div>
            <div class="save-row">
              <span v-if="engineDirty" class="hint-inline">有未保存的修改，保存后从下一个任务起生效</span>
              <NButton type="primary" size="small" :loading="saving" @click="saveEngine">保存</NButton>
            </div>
          </div>
        </section>

        <!-- TensorRT 加速组件 -->
        <section v-if="store.trt" class="card">
          <header class="card-head">
            <div class="card-title">TensorRT 加速组件</div>
            <div class="card-sub">可选 · NVIDIA 显卡推理加速（1080p→4K 最高约 2.5 倍）</div>
          </header>
          <div class="card-body">
            <!-- 安装中：进度 -->
            <template v-if="store.trt.installing">
              <p class="hint" style="margin-bottom: 8px">
                {{ store.trt.phase === 'download'
                  ? `正在下载 ${store.trt.file}（${fmtGB(store.trt.done)} / ${fmtGB(store.trt.total)}）`
                  : `正在解压 ${store.trt.file} …` }}
              </p>
              <NProgress
                :percentage="store.trt.total ? Math.min(100, Math.round(store.trt.done / store.trt.total * 100)) : 0"
                :height="6"
                indicator-placement="inside"
              />
            </template>

            <!-- 已安装：状态 + 卸载 -->
            <template v-else-if="store.trt.installed">
              <div class="row switch-row">
                <span class="row-text">
                  已安装
                  <small>v{{ store.trt.version }} · onnxruntime-gpu {{ store.trt.ort }} · TensorRT {{ store.trt.trt }} · 占用 {{ fmtGB(store.trt.size_bytes) }}</small>
                </span>
                <NButton size="small" :loading="trcBusy" @click="uninstallTrc">卸载</NButton>
              </div>
              <p class="hint">
                配合上方推理后端选「TensorRT」使用；卸载后自动回退 DirectML。
                若提示文件被占用，退出应用后重试。
              </p>
            </template>

            <!-- 未安装：安装入口 -->
            <template v-else>
              <p class="hint">
                适用于 NVIDIA 显卡（2018 年及之后架构）。安装需从 GitHub 下载约 1.5 GB
                运行库，耗时取决于网络环境；未安装时推理使用 DirectML，功能不受影响。
              </p>
              <div class="row switch-row">
                <span class="row-text" v-if="store.trt.error" style="color: #e88080">上次安装失败：{{ store.trt.error }}</span>
                <span class="row-text" v-else>检测到显卡架构：{{ store.trt.gpu_arch ?? '未知（将下载通用包）' }}</span>
                <NButton size="small" type="primary" :loading="trcBusy" @click="installTrc">
                  {{ store.trt.error ? '重试安装' : `下载并安装（约 ${fmtGB(trcDownloadBytes)}）` }}
                </NButton>
              </div>
            </template>
          </div>
        </section>

        <!-- 模型下载 -->
        <section class="card">
          <header class="card-head">
            <div class="card-title">模型下载</div>
            <div class="card-sub">模型从 GitHub Releases 获取时的网络通道</div>
          </header>
          <div class="card-body">
            <p class="hint">
              若「跟随系统代理」模式下下载缓慢，可能是代理规则未覆盖 GitHub CDN 域名，
              可切换为「自定义代理」并填写本地代理地址（如 http://127.0.0.1:7890）。
            </p>
            <div class="row inline-wrap">
              <NSelect v-model:value="proxyMode" :options="proxyOptions" size="small" style="width: 170px" />
              <NInput
                v-if="proxyMode === 'custom'"
                v-model:value="proxyAddr"
                size="small"
                placeholder="http://127.0.0.1:7890"
                style="width: 230px"
              />
              <NButton size="small" type="primary" :loading="savingProxy" @click="saveProxy">保存</NButton>
            </div>
          </div>
        </section>
      </div>

      <!-- 右列：短小配置项 -->
      <div class="col">
        <!-- 输出位置 -->
        <section class="card">
          <header class="card-head">
            <div class="card-title">输出位置</div>
            <div class="card-sub">新建超分任务的默认保存目录，剪切导出同样遵循</div>
          </header>
          <div class="card-body">
            <div class="out-path-box">
              <template v-if="outputDir">
                <span class="out-path" :title="outputDir">{{ outDirShown }}</span>
                <NButton quaternary size="tiny" @click="openOutDir">打开</NButton>
              </template>
              <span v-else class="out-path empty">未设置 · 保存到源视频同目录</span>
            </div>
            <div class="out-actions">
              <NButton size="small" type="primary" :loading="savingOutDir" @click="pickOutDir">浏览…</NButton>
              <NButton v-if="outputDir" size="small" quaternary :disabled="savingOutDir" @click="clearOutDir">恢复默认</NButton>
            </div>
          <p class="hint">
            目录不存在时会自动创建。输出文件在目录内没有同名时直接沿用原文件名；
            已有同名（含源文件本身）时自动改用「原名_倍率」后缀，不覆盖任何现有文件。
          </p>
          </div>
        </section>

        <!-- 应用与更新 -->
        <section class="card">
          <header class="card-head">
            <div class="card-title">应用与更新</div>
            <div class="card-sub">版本检查与升级安装</div>
          </header>
          <div class="card-body">
            <div class="row switch-row">
              <span class="row-text version-line">
                当前版本 <b>v{{ appVersion }}</b>
                <NTag v-if="updateTag" size="small" :bordered="false" :type="updateTag.type">{{ updateTag.text }}</NTag>
              </span>
              <NSpace :size="8">
                <NPopover trigger="hover" placement="top-end" :disabled="!updateNotes" :width="380" trigger-style="display: inline-flex">
                  <template #trigger>
                    <NButton size="small" :loading="checking" @click="checkUpdate">检查更新</NButton>
                  </template>
                  <div class="update-notes">
                    <div class="update-notes-head">本次更新内容</div>
                    <div class="update-notes-body">{{ updateNotes }}</div>
                  </div>
                </NPopover>
                <NButton
                  v-if="updateVersion && !readyVersion"
                  type="primary"
                  size="small"
                  :loading="downloading"
                  :disabled="downloading"
                  @click="doDownload"
                >
                  下载更新 v{{ updateVersion }}
                </NButton>
                <NButton v-if="readyVersion" type="primary" size="small" @click="doInstall">立即重启 v{{ readyVersion }}</NButton>
              </NSpace>
            </div>
            <NProgress v-if="downloading" :percentage="downloadPercent" :height="6" style="margin-top: 10px" />
            <p v-if="updateMsg" class="hint" style="margin-top: 8px">{{ updateMsg }}</p>
            <div class="row switch-row bordered-top">
              <span class="row-text">启动时自动检查更新<small>有新版本时在顶栏版本号旁提示</small></span>
              <NSwitch v-model:value="autoCheck" size="small" @update:value="saveAutoCheck" />
            </div>
          </div>
        </section>

        <!-- 性能监控 -->
        <section class="card">
          <header class="card-head">
            <div class="card-title">性能监控</div>
            <div class="card-sub">「性能」页仪表盘与趋势图的数据来源</div>
          </header>
          <div class="card-body">
            <div class="row switch-row">
              <span class="row-text">
                后台性能采样
                <small>每 2 秒采集 CPU / GPU / 内存占用与任务进程开销；关闭后停止采样、立即生效</small>
              </span>
              <NSwitch v-model:value="perfSampling" size="small" @update:value="savePerfSampling" />
            </div>
            <p class="hint">历史数据保留最近 1 小时，应用重启后清零。</p>
          </div>
        </section>
      </div>
    </div>

    <!-- 设备信息：通栏规格条沉底 -->
    <section class="card">
      <header class="card-head">
        <div class="card-title">设备信息</div>
        <div class="card-sub">决定可选的处理规格与硬件编码能力</div>
      </header>
      <div class="card-body">
        <div class="spec-grid">
          <span class="k">显卡</span>
          <span>{{ store.gpuName }}<template v-if="store.hardware?.gpus?.[0]?.vram_gb">（{{ store.hardware.gpus[0].vram_gb }}GB 显存）</template></span>
          <span class="k">处理器</span>
          <span>{{ store.hardware?.cpu }} · {{ store.hardware?.cpu_cores }} 核心</span>
          <span class="k">内存</span>
          <span>{{ store.hardware?.ram_gb }} GB</span>
          <span class="k">硬件编码</span>
          <span>{{ encSummary }}</span>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.settings-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  width: 100%;
  min-width: 560px; /* 窄于此宽度改为横向滚动,不挤压内部控件 */
}
h1 { font-size: 20px; font-weight: 700; }

/* 两列手动配平（左=高卡 右=短卡）：宽屏只把卡片撑宽,永远不挤第三列；
   窄窗口 auto-fit 回落单列,两列各自整列下移,结构仍稳定 */
.settings-cols {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(430px, 1fr));
  gap: 16px;
  align-items: start;
}
.col { display: flex; flex-direction: column; gap: 16px; min-width: 0; }

/* 统一卡片骨架：头部(标题+副题) + 分隔线行式主体 */
.card {
  background: #1a1c1f;
  border: 1px solid #26292e;
  border-radius: 12px;
  overflow: hidden;
}
.card-head {
  padding: 14px 18px 12px;
  border-bottom: 1px solid #23262b;
  background: linear-gradient(180deg, rgba(255, 255, 255, 0.02), transparent);
}
.card-title { font-size: 14px; font-weight: 650; color: #e8eaed; }
.card-sub { font-size: 12px; color: #9aa0a6; margin-top: 3px; }
.card-body { padding: 4px 18px 14px; }

/* 行式布局：相邻行以发丝线分隔 */
.row { padding: 12px 0; }
.row.bordered-top { border-top: 1px solid #212429; margin-top: 4px; }
.row.stack {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 8px;
}
.row.inline-wrap {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
/* 开关/按钮行：文字描述居左，控件贴右 */
.switch-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.row-label { font-weight: 600; font-size: 13px; color: #e8eaed; }
.row-text { font-weight: 600; font-size: 13px; color: #e8eaed; }
.row-text small {
  display: block;
  font-weight: 400;
  font-size: 12px;
  color: #9aa0a6;
  margin-top: 3px;
  max-width: 420px;
}
.backend-status {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.hint { color: #9aa0a6; font-size: 12px; margin: 2px 0 6px; line-height: 1.55; }
.hint-inline { color: #9aa0a6; font-size: 12px; }
.save-row {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 10px;
  border-top: 1px solid #212429;
  margin-top: 2px;
  padding-top: 10px;
}

/* 输出位置卡 */
.out-path-box {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  background: #141517;
  border: 1px solid #26292e;
  border-radius: 8px;
  padding: 8px 12px;
  margin-top: 10px;
  min-height: 34px;
}
.out-path {
  font-family: Consolas, 'Courier New', monospace;
  font-size: 12.5px;
  color: #c9cdd4;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.out-path.empty { color: #7c838c; font-family: inherit; }
.out-actions { display: flex; gap: 8px; margin-top: 10px; }

.version-line { display: inline-flex; align-items: center; gap: 8px; }
.version-line b { font-weight: 700; }
.update-notes-head { font-weight: 600; margin-bottom: 6px; }
.update-notes-body {
  white-space: pre-wrap;
  font-size: 12px;
  line-height: 1.6;
  color: #c9cdd4;
  max-height: 240px;
  overflow-y: auto;
}

/* 设备信息：标签-值两列网格；通栏时四项两行铺满，窄处自动合并单列 */
.spec-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 10px 32px;
  padding-top: 10px;
  align-items: baseline;
}
.spec-grid > span { display: inline-flex; gap: 10px; align-items: baseline; min-width: 0; }
.spec-grid .k { color: #9aa0a6; font-size: 12.5px; flex-shrink: 0; width: 60px; }
.spec-grid > span:nth-child(even) { color: #e8eaed; font-size: 13px; }
</style>
