<script setup lang="ts">
import { computed, onBeforeUnmount, onDeactivated, ref, watch } from 'vue'
import {
  NButton,
  NCard,
  NInputNumber,
  NProgress,
  NSlider,
  NTag,
  useDialog,
  useMessage,
} from 'naive-ui'
import { api, ApiError, mediaSrc, type ProbeInfo, type TrimJob } from '../api'
import { openWizardWith, ui } from '../store'
import { useFileDrop, useRecentVideos } from '../composables/videoPicks'

const message = useMessage()
const dialog = useDialog()

const input = ref('')
const probeInfo = ref<ProbeInfo | null>(null)
const probing = ref(false)
const startSec = ref(0)
const endSec = ref(0)
const output = ref('')
const outputTouched = ref(false) // 手改/手选过输出路径后，区间联动不再覆盖（与新建任务页同款规则）
const job = ref<TrimJob | null>(null)
const polling = ref<ReturnType<typeof setInterval> | null>(null)
const videoEl = ref<HTMLVideoElement | null>(null)
const previewBroken = ref(false) // 浏览器解不了的格式（AVI/WMV 等）：无预览但剪切不受影响
let probeSeq = 0 // 快速重选文件时丢弃迟到的旧 probe 响应（旧数据覆盖新文件的竞态）

// ---- 最近输入与拖拽换片（与新建任务页共用，见 composables/videoPicks） ----
const baseName = (p: string) => p.split(/[\/]/).pop() ?? p
const { recents, pushRecent } = useRecentVideos()

function pickRecent(p: string) {
  if (busy.value || p === input.value) return
  void load(p)
}

// 作业进行中忽略换片（busy 为运行时求值，回调里判断）
const { dragDepth, onDragEnter, onDragLeave, onDropFiles } = useFileDrop((vids) => {
  if (!busy.value) void load(vids[0])
})

const videoUrl = computed(() => (input.value ? mediaSrc(input.value) : ''))
const duration = computed(() => probeInfo.value?.duration_s ?? 0)
const selDur = computed(() => Math.max(0, endSec.value - startSec.value))
const busy = computed(() => job.value?.state === 'queued' || job.value?.state === 'running')

// NSlider range 的入出都是 number[]：读侧给快照，写侧显式落回两个 ref
const sliderRange = computed(() => [startSec.value, endSec.value])
function onRangeChange(v: (string | number)[]) {
  startSec.value = Number(v[0] ?? 0)
  endSec.value = Number(v[1] ?? 0)
}

const fmt = (s: number) => {
  const m = Math.floor(s / 60)
  const sec = s - m * 60
  return `${m}:${sec.toFixed(1).padStart(4, '0')}`
}

async function pick() {
  const files = await window.sv.pickVideo()
  if (!files.length) return
  await load(files[0])
}

async function load(path: string) {
  const seq = ++probeSeq
  input.value = path
  probeInfo.value = null
  job.value = null
  previewBroken.value = false
  outputTouched.value = false // 换文件：恢复自动填充
  probing.value = true
  pushRecent([path])
  const r = await api.probe(path)
  if (seq !== probeSeq) return // 已重选其他文件：丢弃过期响应
  probing.value = false
  if (r.ok) {
    probeInfo.value = (await r.json()) as ProbeInfo
    startSec.value = 0
    endSec.value = Math.min(probeInfo.value.duration_s, 30)
    autoFillOutput()
  } else {
    // 换到读不了的视频：清掉上一部的区间/输出残留，动作按钮因 selDur=0 自然禁用
    startSec.value = 0
    endSec.value = 0
    output.value = ''
    const e = await r.json()
    message.error(`无法读取视频: ${e.detail ?? r.status}`)
  }
}

/** 移除当前视频回到初始空状态（剪切进行中不允许，避免丢任务卡） */
function removeVideo() {
  stopPolling()
  jobId.value = ''
  input.value = ''
  probeInfo.value = null
  job.value = null
  previewBroken.value = false
  outputTouched.value = false
  output.value = ''
  startSec.value = 0
  endSec.value = 0
}

function autoFillOutput() {
  if (!input.value || outputTouched.value) return
  const m = input.value.match(/^(.*?)(\.[^.]+)?$/)
  output.value = `${m?.[1]}_cut_${ts(startSec.value)}-${ts(endSec.value)}.mp4`
}

const ts = (t: number) => {
  const m = Math.floor(t / 60)
  const s = Math.floor(t % 60)
  return `${String(m).padStart(2, '0')}-${String(s).padStart(2, '0')}`
}

watch([startSec, endSec], autoFillOutput)

async function pickOutputFile() {
  const p = await window.sv.pickOutput(output.value || 'cut.mp4')
  if (p) {
    output.value = p
    outputTouched.value = true
  }
}

function markStart() {
  if (!videoEl.value) return
  startSec.value = Math.min(videoEl.value.currentTime, endSec.value - 0.1)
}
function markEnd() {
  if (!videoEl.value) return
  endSec.value = Math.max(videoEl.value.currentTime, startSec.value + 0.1)
}
function seekTo(t: number) {
  if (videoEl.value) videoEl.value.currentTime = t
}

/** 剪切目的地：仅保存 / 剪完带产物去超分 / 剪完带产物去模型对比 */
async function startCut(dest: 'save' | 'sr' | 'cmp') {
  if (!input.value || selDur.value <= 0.05) {
    message.error('请先选择视频并设置有效的入点/出点')
    return
  }
  stopPolling() // 上一次任务若仍在轮询（异常残留），先掐掉再起新的
  job.value = null
  const run = async (overwrite: boolean) => {
    const r = await api.createTrim({
      input: input.value!,
      start_s: startSec.value,
      end_s: endSec.value,
      mode: 'exact',
      output: output.value || undefined,
      overwrite,
    })
    jobId.value = r.job_id
    job.value = { state: 'queued', progress: 0, input: input.value!, start_s: startSec.value,
      end_s: endSec.value, mode: 'exact', output: r.output, error: null }
    return r.job_id
  }
  const startPollingFor = (jid: string) => {
    let failures = 0
    polling.value = setInterval(async () => {
      try {
        const j = await api.trimStatus(jid)
        failures = 0
        job.value = j
        if (j.state === 'done' || j.state === 'failed' || j.state === 'canceled') {
          stopPolling()
          if (j.state === 'done') {
            message.success(`剪切完成: ${j.duration_s?.toFixed(1) ?? '?'}s`)
            // 自动跳转只在用户还停在剪切页时执行；已切去别的页就不拽人，
            // 结果卡上的「去超分 / 去对比模型」按钮随时可手动续接
            if (ui.page === 'trim') {
              if (dest === 'sr') openWizardWith(j.output)
              else if (dest === 'cmp') toCompareWith(j.output)
            }
          } else if (j.state === 'failed') {
            message.error(`剪切失败: ${j.error}`)
          }
        }
      } catch {
        // 单次瞬时失败（网络抖动）不能永久停轮询——按钮会永远禁用、进度永卡；
        // 连续多次失败才认为真断了
        if (++failures >= 10) stopPolling()
      }
    }, 400)
  }
  try {
    startPollingFor(await run(false))
  } catch (e) {
    if (e instanceof ApiError && e.status === 409) {
      // 输出文件已存在：确认覆盖后重交（仅显式路径会撞；自动命名带时间戳）
      const confirmed = await new Promise<boolean>((resolve) => {
        dialog.warning({
          title: '输出路径冲突',
          content: `${e.message}。继续将覆盖该文件，确定吗？`,
          positiveText: '覆盖并继续',
          negativeText: '返回修改',
          onPositiveClick: () => resolve(true),
          onNegativeClick: () => resolve(false),
          onClose: () => resolve(false),
        })
      })
      if (!confirmed) return
      try {
        startPollingFor(await run(true))
      } catch (e2) {
        message.error(`提交失败: ${(e2 as Error).message}`)
      }
    } else {
      message.error(`提交失败: ${(e as Error).message}`)
    }
  }
}

function stopPolling() {
  if (polling.value) {
    clearInterval(polling.value)
    polling.value = null
  }
}

// 组件随页面切换即卸载：不停轮询会泄漏持有闭包的 interval（反复进出累积）
onBeforeUnmount(stopPolling)
// KeepAlive 常驻后切页只是"隐藏"：不主动暂停的话预览的声音会一直响
onDeactivated(() => {
  if (videoEl.value && !videoEl.value.paused) videoEl.value.pause()
})

const jobId = ref('')

async function cancelCut() {
  if (!jobId.value) return
  try {
    await api.cancelTrim(jobId.value)
  } catch { /* 状态轮询会兜底呈现 */ }
}

function openFolder() {
  if (job.value?.output) window.sv.showInFolder(job.value.output)
}
function toSr() {
  if (job.value?.output) openWizardWith(job.value.output)
}

/** 剪切产物直达模型对比页：区间已落成文件，对比页内还能自由调整取段（≤20s） */
function toCompareWith(file: string) {
  ui.pendingCompare = {
    input: file,
    start_s: 0,
    end_s: Math.max(1, Math.min(selDur.value, 20)),
  }
  ui.page = 'mcompare'
}
</script>

<script lang="ts">
// KeepAlive include 按名匹配：剪切进行中切页回来，区间与结果卡不丢
export default { name: 'Trim' }
</script>

<template>
  <div
    class="trim-page"
    @dragenter="onDragEnter"
    @dragover.prevent
    @dragleave="onDragLeave"
    @drop.prevent="onDropFiles"
  >
    <div v-if="dragDepth" class="drop-mask">
      <div class="drop-tip">松开即可载入视频</div>
    </div>
    <h2 class="title">视频剪切</h2>
    <p class="subtitle">精确转码 · 帧精确 · 剪切完成后可直接加入超分队列</p>

    <div v-if="recents.length" class="recents">
      <span class="recents-label">最近：</span>
      <button
        v-for="p in recents"
        :key="p"
        class="recent-chip"
        :class="{ current: p === input }"
        :title="p"
        @click="pickRecent(p)"
      >
        {{ baseName(p) }}
      </button>
    </div>

    <NButton v-if="!input" dashed block size="large" style="margin-top: 12px" @click="pick">
      点击选择视频文件（也可直接拖进窗口）
    </NButton>

    <template v-else>
      <div class="head">
        <NButton size="small" quaternary :disabled="busy" @click="pick">重选视频</NButton>
        <NButton size="small" quaternary type="error" :disabled="busy" @click="removeVideo">
          移除
        </NButton>
        <NTag v-if="probeInfo" size="small" :bordered="false">
          {{ probeInfo.width }}x{{ probeInfo.height }} · {{ probeInfo.fps }}fps · {{ fmt(duration) }}
        </NTag>
      </div>

      <div class="preview-wrap">
        <video
          ref="videoEl"
          :src="videoUrl"
          controls
          preload="metadata"
          class="preview"
          :style="{ visibility: previewBroken ? 'hidden' : 'visible' }"
          @error="previewBroken = true"
        />
        <div v-if="previewBroken" class="preview-broken">
          <div class="pb-title">无法预览：该视频的容器/编码浏览器不支持</div>
          <div class="pb-desc">
            常见于 AVI/WMV/FLV/TS 格式，或当前设备不支持 HEVC 硬解码。剪切功能不受影响：
            可通过下方滑条与入点/出点数值设置区间（时长、帧率信息正常），输出完整无损。
          </div>
        </div>
        <div class="mark-btns">
          <NButton size="tiny" :disabled="previewBroken" @click="seekTo(startSec)">← 入点</NButton>
          <NButton size="tiny" type="primary" :disabled="previewBroken" @click="markStart">设为入点</NButton>
          <NButton size="tiny" type="primary" :disabled="previewBroken" @click="markEnd">设为出点</NButton>
          <NButton size="tiny" :disabled="previewBroken" @click="seekTo(endSec)">出点 →</NButton>
        </div>
      </div>

      <NCard size="small" :bordered="true" class="panel">
        <div class="row">
          <span class="lbl">选择区间</span>
          <NSlider
            :value="sliderRange"
            range
            :min="0"
            :max="Math.max(duration, 0.1)"
            :step="0.1"
            :format-tooltip="(v: number) => fmt(v)"
            @update:value="onRangeChange"
            class="slider"
          />
        </div>
        <div class="row">
          <span class="lbl">入点/出点</span>
          <NInputNumber v-model:value="startSec" :min="0" :max="duration" :step="0.1" size="small" style="width: 130px" />
          <span class="dash">~</span>
          <NInputNumber v-model:value="endSec" :min="0" :max="duration" :step="0.1" size="small" style="width: 130px" />
          <NTag size="small" :bordered="false" type="info" style="margin-left: 8px">
            片段时长 {{ fmt(selDur) }}
          </NTag>
        </div>
        <div class="row">
          <span class="lbl">输出到</span>
          <input v-model="output" class="out-input" spellcheck="false" @input="outputTouched = true" />
          <NButton size="tiny" @click="pickOutputFile">浏览…</NButton>
        </div>
      </NCard>

      <div class="actions">
        <NButton :disabled="busy || selDur <= 0.05" :loading="busy" @click="startCut('save')">
          剪切保存
        </NButton>
        <NButton type="primary" :disabled="busy || selDur <= 0.05" @click="startCut('sr')">
          剪切并去超分 →
        </NButton>
        <!-- 先落盘剪切，完成后自动带着剪切文件进模型对比页（选模型并排跑） -->
        <NButton type="primary" ghost :disabled="busy || selDur <= 0.05" @click="startCut('cmp')">
          剪切并去对比模型 →
        </NButton>
        <NButton v-if="busy" quaternary type="warning" @click="cancelCut">取消</NButton>
      </div>

      <NCard v-if="job" size="small" :bordered="true" class="result" :class="job.state">
        <template v-if="job.state === 'queued' || job.state === 'running'">
          <div class="res-line">正在剪切… {{ Math.round(job.progress * 100) }}%</div>
          <NProgress type="line" :percentage="Math.round(job.progress * 100)" :show-indicator="false" />
        </template>
        <template v-else-if="job.state === 'done'">
          <div class="res-line ok">✓ 剪切完成（{{ job.duration_s?.toFixed(1) }}s）</div>
          <div class="res-detail">
            {{ job.output }}
          </div>
          <div v-for="n in job.notices ?? []" :key="n" class="res-detail warn">{{ n }}</div>
          <div class="res-btns">
            <NButton size="small" @click="openFolder">打开所在文件夹</NButton>
            <NButton size="small" type="primary" @click="toSr">去超分</NButton>
            <NButton size="small" type="primary" ghost @click="toCompareWith(job.output)">
              去对比模型 →
            </NButton>
          </div>
        </template>
        <template v-else-if="job.state === 'canceled'">
          <div class="res-line warn">已取消剪切</div>
        </template>
        <template v-else>
          <div class="res-line err">✗ 剪切失败: {{ job.error }}</div>
        </template>
      </NCard>
    </template>
  </div>
</template>

<style scoped>
.trim-page { width: 100%; } /* 全屏铺满,预览跟随窗口放大 */
.title { font-size: 20px; font-weight: 600; margin-bottom: 2px; }
.subtitle { font-size: 13px; color: #9aa0a6; margin-bottom: 16px; }
.head { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
.recents { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 4px; }
.recents-label { font-size: 12px; color: #9aa0a6; }
.recent-chip {
  display: inline-flex;
  align-items: center;
  max-width: 240px;
  padding: 4px 12px;
  border-radius: 14px;
  border: 1px solid #33373d;
  background: #1c1e21;
  color: #c6cad0;
  font-size: 12.5px;
  cursor: pointer;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  transition: border-color 0.15s, color 0.15s;
}
.recent-chip:hover { border-color: #4f8cff; color: #fff; }
.recent-chip.current { border-color: #4f8cff; color: #fff; cursor: default; }
/* 拖拽遮罩：拖文件进窗口时整页高亮 */
.drop-mask {
  position: fixed;
  inset: 0;
  z-index: 50;
  background: rgba(13, 14, 16, 0.72);
  border: 2px dashed #4f8cff;
  display: flex;
  align-items: center;
  justify-content: center;
  pointer-events: none;
}
.drop-tip { font-size: 18px; font-weight: 600; color: #e8eaed; letter-spacing: 1px; }
.preview-wrap { display: flex; flex-direction: column; gap: 6px; margin-bottom: 14px; position: relative; }
.preview {
  width: 100%;
  max-height: min(62vh, 760px);
  min-height: 300px;
  background: #000;
  border-radius: 8px;
  outline: none;
}
.preview-broken {
  position: absolute;
  inset: 0 0 46px 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  background: #0d0e10;
  border-radius: 8px;
  padding: 20px;
  text-align: center;
}
.pb-title { font-size: 14px; font-weight: 600; color: #fbbf24; }
.pb-desc { font-size: 12.5px; color: #9aa0a6; max-width: 520px; line-height: 1.7; }
.mark-btns { display: flex; gap: 8px; justify-content: center; }
.panel { background: #1a1c1f; }
.row { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.row:last-child { margin-bottom: 0; }
.lbl { width: 64px; flex-shrink: 0; font-size: 13px; color: #9aa0a6; }
.slider { flex: 1; }
.dash { color: #9aa0a6; }
.out-input {
  flex: 1;
  background: #141517;
  border: 1px solid #2a2d31;
  border-radius: 6px;
  color: #e8eaed;
  font-size: 13px;
  padding: 5px 10px;
  min-width: 0;
}
.out-input:focus { outline: none; border-color: #4f8cff; }
.actions { display: flex; gap: 10px; margin: 16px 0; }
.result { margin-top: 4px; }
.res-line { font-size: 13.5px; margin-bottom: 8px; }
.res-line.ok { color: #34d399; }
.res-line.err { color: #f87171; }
.res-line.warn { color: #fbbf24; }
.res-detail { font-size: 12.5px; color: #9aa0a6; margin-bottom: 6px; word-break: break-all; }
.res-detail.warn { color: #fbbf24; }
.res-btns { display: flex; gap: 8px; margin-top: 8px; }
</style>
