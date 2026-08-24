<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
  NButton,
  NCard,
  NInputNumber,
  NProgress,
  NSlider,
  NTag,
  useMessage,
} from 'naive-ui'
import { api, mediaSrc, type ProbeInfo, type TrimJob } from '../api'
import { openWizardWith } from '../store'

const message = useMessage()

const input = ref('')
const probeInfo = ref<ProbeInfo | null>(null)
const probing = ref(false)
const startSec = ref(0)
const endSec = ref(0)
const output = ref('')
const job = ref<TrimJob | null>(null)
const polling = ref<ReturnType<typeof setInterval> | null>(null)
const videoEl = ref<HTMLVideoElement | null>(null)
const previewBroken = ref(false) // 浏览器解不了的格式（AVI/WMV 等）：无预览但剪切不受影响

const videoUrl = computed(() => (input.value ? mediaSrc(input.value) : ''))
const duration = computed(() => probeInfo.value?.duration_s ?? 0)
const selDur = computed(() => Math.max(0, endSec.value - startSec.value))
const busy = computed(() => job.value?.state === 'queued' || job.value?.state === 'running')

const range = computed<[number, number]>({
  get: () => [startSec.value, endSec.value],
  set: (v) => {
    startSec.value = v[0]
    endSec.value = v[1]
  },
})

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
  input.value = path
  probeInfo.value = null
  job.value = null
  previewBroken.value = false
  probing.value = true
  const r = await api.probe(path)
  probing.value = false
  if (r.ok) {
    probeInfo.value = (await r.json()) as ProbeInfo
    startSec.value = 0
    endSec.value = Math.min(probeInfo.value.duration_s, 30)
    autoFillOutput()
  } else {
    const e = await r.json()
    message.error(`无法读取视频: ${e.detail ?? r.status}`)
  }
}

function autoFillOutput() {
  if (!input.value) return
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
  if (p) output.value = p
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

async function startCut(andSr: boolean) {
  if (!input.value || selDur.value <= 0.05) {
    message.error('请先选择视频并设置有效的入点/出点')
    return
  }
  job.value = null
  try {
    const r = await api.createTrim({
      input: input.value,
      start_s: startSec.value,
      end_s: endSec.value,
      mode: 'exact',
      output: output.value || undefined,
    })
    job.value = { state: 'queued', progress: 0, input: input.value, start_s: startSec.value,
      end_s: endSec.value, mode: 'exact', output: r.output, error: null }
    polling.value = setInterval(async () => {
      try {
        const j = await api.trimStatus(r.job_id)
        job.value = j
        if (j.state === 'done' || j.state === 'failed') {
          stopPolling()
          if (j.state === 'done') {
            message.success(`剪切完成: ${j.duration_s?.toFixed(1) ?? '?'}s`)
            if (andSr) openWizardWith(j.output)
          } else {
            message.error(`剪切失败: ${j.error}`)
          }
        }
      } catch {
        stopPolling()
      }
    }, 400)
  } catch (e) {
    message.error(`提交失败: ${(e as Error).message}`)
  }
}

function stopPolling() {
  if (polling.value) {
    clearInterval(polling.value)
    polling.value = null
  }
}

function openFolder() {
  if (job.value?.output) window.sv.showInFolder(job.value.output)
}
function toSr() {
  if (job.value?.output) openWizardWith(job.value.output)
}
</script>

<template>
  <div class="trim-page">
    <h2 class="title">视频剪切</h2>
    <p class="subtitle">精确转码 · 帧精确 · 剪出片段后可直接加入超分队列</p>

    <NButton v-if="!input" dashed block size="large" @click="pick">
      点击选择视频文件
    </NButton>

    <template v-else>
      <div class="head">
        <NButton size="small" quaternary @click="pick">重选视频</NButton>
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
            常见于 AVI/WMV/FLV/TS，或本机无 HEVC 硬解码。剪切不受任何影响——
            可用下方滑条和入点/出点数字设置区间（时长、帧率信息正常），产物完整无损。
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
            v-model:value="range"
            range
            :min="0"
            :max="Math.max(duration, 0.1)"
            :step="0.1"
            :format-tooltip="(v: number) => fmt(v)"
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
          <input v-model="output" class="out-input" spellcheck="false" />
          <NButton size="tiny" @click="pickOutputFile">浏览…</NButton>
        </div>
      </NCard>

      <div class="actions">
        <NButton :disabled="busy || selDur <= 0.05" :loading="busy" @click="startCut(false)">
          剪切保存
        </NButton>
        <NButton type="primary" :disabled="busy || selDur <= 0.05" @click="startCut(true)">
          剪切并去超分 →
        </NButton>
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
          </div>
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
.res-detail { font-size: 12.5px; color: #9aa0a6; margin-bottom: 6px; word-break: break-all; }
.res-detail.warn { color: #fbbf24; }
.res-btns { display: flex; gap: 8px; margin-top: 8px; }
</style>
