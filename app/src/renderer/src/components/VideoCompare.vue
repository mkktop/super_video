<script setup lang="ts">
import { computed, onActivated, onBeforeUnmount, onDeactivated, onMounted, ref, watch } from 'vue'
import { NButton, NSlider } from 'naive-ui'
import { mediaSrc } from '../api'

const props = defineProps<{
  srcPath?: string
  outPath?: string
  /** 直接给资源地址（模型对比产物走 HTTP+token，非本地文件）——优先于路径 */
  srcUrl?: string
  outUrl?: string
  /** 上游还在产物流（对比作业运行中）：加载失败按"未就绪"定时重试而非判死 */
  streaming?: boolean
}>()

const srcUrl = computed(() => props.srcUrl ?? mediaSrc(props.srcPath ?? ''))
const outUrl = computed(() => props.outUrl ?? mediaSrc(props.outPath ?? ''))

const srcV = ref<HTMLVideoElement | null>(null)
const outV = ref<HTMLVideoElement | null>(null)
const root = ref<HTMLElement | null>(null)

const pos = ref(50) // 分割线位置 %
const playing = ref(false)
const cur = ref(0)
const dur = ref(0)

// ---- 加载失败重试：对比产物是边跑边落的（seg 先切好、各模型 mp4 跑完一个出一个），
// 刚点开成片的一瞬某路可能还是 404——那不是格式问题。失败计数同时充当加载代号
// 追加进 URL 触发强制重载；换地址时清零重新计。streaming 时限放宽到 10 次×1.5s ----
const MAX_FAST = 1 // 非流式也允许多试一次：挡偶发网络抖动误标"格式不支持"
const MAX_STREAMING = 10
const RETRY_MS = 1500
const srcFails = ref(0)
const outFails = ref(0)
const retryTimers: Record<'src' | 'out', ReturnType<typeof setTimeout> | null> = { src: null, out: null }
const maxFails = computed(() => (props.streaming ? MAX_STREAMING : MAX_FAST))

function busted(url: string, n: number) {
  return n === 0 ? url : `${url}${url.includes('?') ? '&' : '?'}_r=${n}`
}
function onErr(side: 'src' | 'out') {
  if (retryTimers[side] !== null) return
  if ((side === 'src' ? srcFails.value : outFails.value) >= maxFails.value) return
  retryTimers[side] = setTimeout(() => {
    retryTimers[side] = null
    if (side === 'src') srcFails.value++
    else outFails.value++
  }, RETRY_MS)
}
const srcBroken = computed(() => srcFails.value > maxFails.value)
const outBroken = computed(() => outFails.value > maxFails.value)
/** 流式中出现过失败且尚未放弃：提示"生成中/正在重试"而非"不支持" */
const waitingAssets = computed(
  () => !!props.streaming && !srcBroken.value && !outBroken.value &&
    (srcFails.value > 0 || outFails.value > 0))

watch([srcUrl, outUrl], () => {
  srcFails.value = 0
  outFails.value = 0
  if (retryTimers.src) { clearTimeout(retryTimers.src); retryTimers.src = null }
  if (retryTimers.out) { clearTimeout(retryTimers.out); retryTimers.out = null }
})
onBeforeUnmount(() => {
  if (retryTimers.src) clearTimeout(retryTimers.src)
  if (retryTimers.out) clearTimeout(retryTimers.out)
})

const fmt = (t: number) => {
  const m = Math.floor(t / 60)
  const s = Math.floor(t % 60)
  return `${m}:${String(s).padStart(2, '0')}`
}

// 以输出视频为主时钟，rAF 持续把源视频对齐（两路时长可能差 ±1 帧）。
// 三档校正：半帧内不动；大偏差（拖动恢复/loop 绕回/卡顿后）硬 seek；
// 中间偏差用 playbackRate 在 ±10% 内平滑追赶——对比场景跳帧闪烁比轻微变速扎眼得多，
// 而单一 0.05s 死区会放任 1~3 帧的持续错位（运动画面肉眼可辨，30fps 一帧仅 33ms）
const FRAME_TOL = 0.02
const HARD_DRIFT = 0.3
let raf = 0
let lastCur = -1
function tick() {
  const s = srcV.value
  const o = outV.value
  if (s && o) {
    // 任一路在 seek 中不校：o 的 currentTime 还是中间态，跟着对只会来回抖
    if (!s.seeking && !o.seeking && !srcBroken.value) {
      const drift = s.currentTime - o.currentTime
      if (Math.abs(drift) > HARD_DRIFT) {
        s.currentTime = o.currentTime
        s.playbackRate = 1
      } else if (o.paused) {
        // 暂停态画面静止，直接对齐没有闪烁问题
        if (Math.abs(drift) > FRAME_TOL) s.currentTime = o.currentTime
        if (s.playbackRate !== 1) s.playbackRate = 1
      } else if (Math.abs(drift) > FRAME_TOL) {
        // drift<0 源落后→加速追，>0 超前→减速让
        s.playbackRate = Math.min(1.1, Math.max(0.9, 1 - drift * 0.5))
      } else if (s.playbackRate !== 1) {
        s.playbackRate = 1
      }
    }
    if (!o.paused && s.paused && !srcBroken.value) void s.play().catch(() => {})
    if (o.paused && !s.paused) s.pause()
    playing.value = !o.paused
    if (Math.abs(o.currentTime - lastCur) > 0.05) {
      lastCur = o.currentTime
      cur.value = o.currentTime
    }
  }
  raf = requestAnimationFrame(tick)
}

function toggle() {
  const s = srcV.value
  const o = outV.value
  if (!o) return
  if (o.paused) {
    void o.play().catch(() => {})
    if (s) void s.play().catch(() => {})
  } else {
    o.pause()
    s?.pause()
  }
}

function seek(v: number) {
  cur.value = v
  lastCur = v
  if (outV.value) outV.value.currentTime = v
  if (srcV.value) srcV.value.currentTime = v
}

function setFromX(clientX: number) {
  const rect = root.value?.getBoundingClientRect()
  if (!rect || rect.width === 0) return
  pos.value = Math.min(100, Math.max(0, ((clientX - rect.left) / rect.width) * 100))
}

function onDown(e: MouseEvent) {
  e.preventDefault()
  setFromX(e.clientX)
  const move = (ev: MouseEvent) => setFromX(ev.clientX)
  const up = () => {
    window.removeEventListener('mousemove', move)
    window.removeEventListener('mouseup', up)
  }
  window.addEventListener('mousemove', move)
  window.addEventListener('mouseup', up)
}

function onKey(e: KeyboardEvent) {
  const step = e.shiftKey ? 5 : 1
  if (e.key === 'ArrowLeft') pos.value = Math.max(0, pos.value - step)
  else if (e.key === 'ArrowRight') pos.value = Math.min(100, pos.value + step)
}

function startRaf() {
  if (!raf) raf = requestAnimationFrame(tick)
}
function stopRaf() {
  if (raf) {
    cancelAnimationFrame(raf)
    raf = 0
  }
}

onMounted(() => {
  window.addEventListener('keydown', onKey)
  startRaf()
})
// 父页面（模型对比）KeepAlive 常驻：切页时停掉全局键盘监听与 rAF，
// 否则在别的页按 ←/→ 会挪看不见的分割线；对比页(Compare)走正常卸载路径
onActivated(() => {
  window.addEventListener('keydown', onKey)
  startRaf()
})
onDeactivated(() => {
  window.removeEventListener('keydown', onKey)
  stopRaf()
})
onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKey)
  stopRaf()
})
</script>

<template>
  <div class="vcompare">
    <div ref="root" class="stage" @mousedown="onDown">
      <video
        ref="outV"
        class="video"
        :src="busted(outUrl, outFails)"
        muted
        loop
        playsinline
        preload="auto"
        @loadedmetadata="dur = outV?.duration ?? 0"
        @error="onErr('out')"
      />
      <video
        ref="srcV"
        class="video top"
        :src="busted(srcUrl, srcFails)"
        muted
        loop
        playsinline
        preload="auto"
        :style="{ clipPath: `inset(0 ${100 - pos}% 0 0)` }"
        @error="onErr('src')"
      />
      <div class="handle" :style="{ left: pos + '%' }">
        <div class="line" />
        <div class="knob">⇄</div>
      </div>
      <span class="label label-l">源</span>
      <span class="label label-r">超分</span>
      <div v-if="waitingAssets" class="broken pending">
        成片还在生成中，正在自动加载…
      </div>
      <div v-else-if="srcBroken || outBroken" class="broken">
        {{ srcBroken ? '源' : '超分' }}视频无法直接播放：可能是该模型的输出编码浏览器解不了，
        或资源地址已失效——可切回「静帧」模式对比
      </div>
    </div>
    <div class="bar">
      <NButton size="small" @click="toggle">{{ playing ? '暂停' : '播放' }}</NButton>
      <span class="time">{{ fmt(cur) }} / {{ fmt(dur) }}</span>
      <NSlider
        class="seek"
        :value="cur"
        :min="0"
        :max="Math.max(dur, 0.1)"
        :step="0.01"
        :format-tooltip="(v: number) => fmt(v)"
        :disabled="!dur"
        @update:value="seek"
      />
    </div>
  </div>
</template>

<style scoped>
.vcompare {
  height: 100%;
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-height: 0;
}
.stage {
  position: relative;
  flex: 1;
  min-height: 0;
  overflow: hidden;
  user-select: none;
  cursor: ew-resize;
  background: #0d0e10;
  border-radius: 6px;
}
/* 与静帧对比同构：两层同尺寸 contain，clip-path 裁剪，画面像素级对齐 */
.video {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: contain;
  pointer-events: none;
}
.handle {
  position: absolute;
  top: 0;
  bottom: 0;
  width: 0;
  pointer-events: none;
}
.line {
  position: absolute;
  top: 0;
  bottom: 0;
  left: -1px;
  width: 2px;
  background: #4f8cff;
  box-shadow: 0 0 8px rgba(79, 140, 255, 0.8);
}
.knob {
  position: absolute;
  top: 50%;
  left: -16px;
  width: 32px;
  height: 32px;
  margin-top: -16px;
  border-radius: 50%;
  background: #4f8cff;
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.5);
}
.label {
  position: absolute;
  top: 10px;
  font-size: 12px;
  padding: 3px 10px;
  border-radius: 12px;
  background: rgba(0, 0, 0, 0.55);
  color: #e8eaed;
  pointer-events: none;
}
.label-l { left: 10px; }
.label-r { right: 10px; }
.broken {
  position: absolute;
  left: 50%;
  bottom: 14px;
  transform: translateX(-50%);
  max-width: 86%;
  font-size: 12px;
  padding: 5px 12px;
  border-radius: 10px;
  background: rgba(120, 70, 0, 0.78);
  color: #fbbf24;
  pointer-events: none;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.broken.pending {
  background: rgba(17, 52, 96, 0.75);
  color: #7db4ff;
}
.bar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
  padding: 0 2px;
}
.time {
  font-size: 12px;
  color: #9aa0a6;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.seek { flex: 1; }
</style>
