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

    <div v-if="recents.length && input" class="recents">
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

    <!-- 空态：整块拖放引导区（点击=选择文件，拖入=直接载入） -->
    <div v-if="!input" class="dropzone" @click="pick">
      <div class="dz-icon">
        <svg width="34" height="34" viewBox="0 0 34 34">
          <rect x="3" y="6.5" width="28" height="21" rx="4" fill="none" stroke="currentColor" stroke-width="2" />
          <path d="M14.2 12.6l8 4.4-8 4.4z" fill="currentColor" />
          <path d="M7 11.2h.01M7 17h.01M7 22.8h.01M27 11.2h.01M27 17h.01M27 22.8h.01" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" />
        </svg>
      </div>
      <div class="dz-title">把视频拖进这里，或点击选择</div>
      <div class="dz-sub">帧精确剪切 · 剪完可直接送去超分或模型对比</div>
      <div class="dz-formats">MP4 · MKV · MOV · WebM · TS · AVI</div>
      <div v-if="recents.length" class="dz-recents">
        <button
          v-for="p in recents"
          :key="p"
          class="recent-chip"
          :title="p"
          @click.stop="pickRecent(p)"
        >
          {{ baseName(p) }}
        </button>
      </div>
    </div>

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
/* ---- 空态拖放引导区 ---- */
.dropzone {
  position: relative;
  margin-top: 14px;
  min-height: 420px;
  border: 1.5px dashed rgba(96, 130, 200, 0.42);
  border-radius: 18px;
  background:
    radial-gradient(460px 240px at 50% -4%, rgba(79, 140, 255, 0.08), transparent 65%),
    radial-gradient(320px 200px at 88% 108%, rgba(139, 92, 246, 0.06), transparent 65%),
    linear-gradient(180deg, #171a21, #14161c);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 7px;
  cursor: pointer;
  overflow: hidden;
  padding: 28px;
  transition: border-color 0.2s ease, box-shadow 0.2s ease;
}
.dropzone:hover {
  border-color: rgba(79, 140, 255, 0.75);
  box-shadow:
    0 0 0 1px rgba(79, 140, 255, 0.22),
    0 12px 34px rgba(0, 0, 0, 0.32),
    inset 0 0 70px rgba(79, 140, 255, 0.05);
}
.dz-icon {
  width: 74px;
  height: 74px;
  border-radius: 22px;
  background: var(--sv-grad);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  margin-bottom: 12px;
  box-shadow: 0 10px 28px rgba(79, 140, 255, 0.35), inset 0 1px 0 rgba(255, 255, 255, 0.28);
  transition: transform 0.2s ease;
}
.dropzone:hover .dz-icon { transform: translateY(-5px); }
.dz-title { font-size: 17.5px; font-weight: 700; color: #e9ecf2; letter-spacing: 0.3px; }
.dz-sub { font-size: 12.5px; color: #9aa1ad; }
.dz-formats {
  margin-top: 10px;
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 2px;
  color: #5f6a7d;
}
.dz-recents {
  margin-top: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 0 20px;
}
.dz-recents .recent-chip.current { cursor: default; }

.title { font-size: 21px; font-weight: 750; letter-spacing: 0.3px; margin-bottom: 2px; }
.subtitle { font-size: 13px; color: #9aa1ad; margin-bottom: 16px; }
.head { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
.recents { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 4px; }
.recents-label { font-size: 12px; color: #9aa1ad; }
.recent-chip {
  display: inline-flex;
  align-items: center;
  max-width: 240px;
  padding: 4px 12px;
  border-radius: 14px;
  border: 1px solid rgba(255, 255, 255, 0.08);
  background: rgba(255, 255, 255, 0.03);
  color: #c6cbd4;
  font-size: 12.5px;
  cursor: pointer;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  transition: border-color 0.15s, color 0.15s;
}
.recent-chip:hover { border-color: rgba(79, 140, 255, 0.55); color: #fff; }
.recent-chip.current { border-color: rgba(79, 140, 255, 0.7); color: #fff; cursor: default; background: rgba(79, 140, 255, 0.08); }
/* 拖拽遮罩：拖文件进窗口时整页高亮 */
.drop-mask {
  position: fixed;
  inset: 0;
  z-index: 50;
  background: rgba(10, 12, 16, 0.78);
  border: 2px dashed rgba(79, 140, 255, 0.75);
  display: flex;
  align-items: center;
  justify-content: center;
  pointer-events: none;
  box-shadow: inset 0 0 120px rgba(79, 140, 255, 0.12);
}
.drop-tip {
  font-size: 18px;
  font-weight: 650;
  color: #e9ecf2;
  letter-spacing: 1px;
  padding: 14px 28px;
  border-radius: 14px;
  border: 1px solid rgba(79, 140, 255, 0.45);
  background: rgba(79, 140, 255, 0.08);
  box-shadow: 0 0 40px rgba(79, 140, 255, 0.2);
}
.preview-wrap { display: flex; flex-direction: column; gap: 6px; margin-bottom: 14px; position: relative; }
.preview {
  width: 100%;
  max-height: min(62vh, 760px);
  min-height: 300px;
  background: #000;
  border-radius: 14px;
  outline: none;
  border: 1px solid rgba(255, 255, 255, 0.07);
  box-shadow: 0 12px 34px rgba(0, 0, 0, 0.35);
}
.preview-broken {
  position: absolute;
  inset: 0 0 46px 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  background: var(--sv-panel-deep);
  border-radius: 12px;
  padding: 20px;
  text-align: center;
}
.pb-title { font-size: 14px; font-weight: 600; color: #fbbf24; }
.pb-desc { font-size: 12.5px; color: #9aa1ad; max-width: 520px; line-height: 1.7; }
.mark-btns {
  display: flex;
  gap: 8px;
  justify-content: center;
  margin-top: 12px;
  padding: 8px 12px;
  border-radius: 12px;
  background: linear-gradient(180deg, #1c2027, #181b21);
  border: 1px solid rgba(255, 255, 255, 0.06);
}
.panel { background: linear-gradient(180deg, #1c2027, #181b21); }
.row { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.row:last-child { margin-bottom: 0; }
.lbl { width: 64px; flex-shrink: 0; font-size: 13px; color: #9aa1ad; }
.slider { flex: 1; }
.dash { color: #9aa1ad; }
.out-input {
  flex: 1;
  background: rgba(0, 0, 0, 0.28);
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 8px;
  color: #e9ecf2;
  font-size: 13px;
  padding: 5px 10px;
  min-width: 0;
}
.out-input:focus { outline: none; border-color: #4f8cff; }
.actions { display: flex; gap: 10px; margin: 16px 0; }
.result { margin-top: 4px; }
/* 结果卡状态脊线（inset 不挤布局）：与任务卡同语言 */
.result.running, .result.queued { box-shadow: inset 3px 0 0 rgba(79, 140, 255, 0.85); }
.result.done { box-shadow: inset 3px 0 0 rgba(52, 211, 153, 0.8); }
.result.failed { box-shadow: inset 3px 0 0 rgba(248, 113, 113, 0.85); }
.result.canceled { box-shadow: inset 3px 0 0 rgba(251, 191, 36, 0.75); }
.res-line { font-size: 13.5px; margin-bottom: 8px; }
.res-line.ok { color: #34d399; }
.res-line.err { color: #f87171; }
.res-line.warn { color: #fbbf24; }
.res-detail { font-size: 12.5px; color: #9aa1ad; margin-bottom: 6px; word-break: break-all; }
.res-detail.warn { color: #fbbf24; }
.res-btns { display: flex; gap: 8px; margin-top: 8px; }
</style>
