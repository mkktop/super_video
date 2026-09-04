<script setup lang="ts">
import { computed, nextTick, onActivated, onBeforeUnmount, onDeactivated, onMounted, ref, watch } from 'vue'
import {
  NButton,
  NProgress,
  NRadioButton,
  NRadioGroup,
  NSlider,
  NSpace,
  NTag,
  useMessage,
} from 'naive-ui'
import { api, compareAssetUrl, mediaSrc, type CompareJob, type ProbeInfo } from '../api'
import { openWizardWith, store, ui } from '../store'
import { useFullscreen } from '../utils'
import CompareSlider from '../components/CompareSlider.vue'
import VideoCompare from '../components/VideoCompare.vue'

const message = useMessage()

/** 结果舞台整段进全屏（含切换条），对比画面铺满显示器 */
const stageSec = ref<HTMLElement | null>(null)
const { isFullscreen: stageFull, toggle: toggleStageFull } = useFullscreen(stageSec)

const MAX_MODELS = 6
const MAX_SEG_S = 20

// ---- 素材 ----
const mode = ref<'video' | 'image'>('video')
const videoInput = ref('')
const probeInfo = ref<ProbeInfo | null>(null)
const probing = ref(false)
let probeSeq = 0
const imageInput = ref('')
const segStart = ref(0)
const segDur = ref(6)

const maxStart = computed(() => Math.max(0, (probeInfo.value?.duration_s ?? 1) - 1))
const maxDur = computed(() =>
  Math.min(MAX_SEG_S, Math.max(1, (probeInfo.value?.duration_s ?? 1) - segStart.value)))

watch(segStart, (s) => {
  if (segDur.value > maxDur.value) segDur.value = maxDur.value
})

async function loadVideoFile(path: string, start?: number, dur?: number) {
  const seq = ++probeSeq
  videoInput.value = path
  probeInfo.value = null
  probing.value = true
  const r = await api.probe(path)
  if (seq !== probeSeq) return
  probing.value = false
  if (r.ok) {
    probeInfo.value = (await r.json()) as ProbeInfo
    // 预填区间（剪切页跳转）：起点/时长钳到合法范围，超 20s 由上限截断
    segStart.value = Math.max(0, Math.min(start ?? 0, maxStart.value))
    segDur.value = Math.max(1, Math.min(dur ?? 6, maxDur.value))
  } else {
    const e = await r.json()
    message.error(e.detail ?? `无法读取视频`)
    videoInput.value = ''
  }
}

async function pickVideo() {
  const files = await window.sv.pickVideo()
  if (!files.length) return
  await loadVideoFile(files[0])
}

async function pickImage() {
  const files = await window.sv.pickImages()
  if (files.length) imageInput.value = files[0]
}

// 剪切页「剪切并去对比模型」/ 新建任务页「先试跑」跳转：区间已落成独立小文件，
// 默认从 0 起取整段（超 20s 由上限截断，仍可在下方滑条自由调整取段）。
// 新建任务页会附带预选模型与倍率（pendingModel/pendingScale），此处一并消费。
// 本页 KeepAlive 常驻：只有首次进入才触发 onMounted；之后从缓存激活只触发
// onActivated——两处都得消费，否则新片段进不来（还会停在上次的结果视图里），
// 「开始对比」自然点不了。首次进入两钩子连发，靠置空信号保证幂等。
function consumePendingCompare() {
  const pc = ui.pendingCompare
  if (!pc) return
  ui.pendingCompare = null
  reset()
  view.value = 'frames'
  mode.value = 'video'
  const pm = ui.pendingModel
  ui.pendingModel = null
  const wantScale = ui.pendingScale
  ui.pendingScale = null
  void loadVideoFile(pc.input, pc.start_s, pc.end_s - pc.start_s).then(() => {
    if (!pm) return
    const spec = store.models.find((m) => m.id === pm)
    if (!spec || !spec.vram_ok || selected.value.size >= MAX_MODELS) return
    selected.value.add(pm)
    selected.value = new Set(selected.value)
    void nextTick(() => {
      // commonScales 的 watch 会把 scale 拉回最大值；预填倍率在它之后收敛
      if (wantScale && commonScales.value.includes(wantScale)) scale.value = wantScale
    })
  })
}
onMounted(consumePendingCompare)
onActivated(consumePendingCompare)

const srcReady = computed(() =>
  mode.value === 'video' ? !!probeInfo.value?.ok : !!imageInput.value)

const fmtT = (s: number) => {
  const m = Math.floor(s / 60)
  const sec = s - m * 60
  return `${m}:${sec < 10 ? '0' : ''}${sec.toFixed(1)}`
}

// ---- 模型多选 ----
const selected = ref<Set<string>>(new Set())
const cmpModels = computed(() =>
  store.models
    .filter((m) => m.kind !== 'interp' && m.engine !== 'torch')
    .sort((a, b) => Number(b.installed || b.bundled) - Number(a.installed || a.bundled)))

function toggle(id: string, ok?: boolean) {
  if (!ok) return
  if (selected.value.has(id)) selected.value.delete(id)
  else if (selected.value.size < MAX_MODELS) selected.value.add(id)
  selected.value = new Set(selected.value) // 触发响应式
}

const commonScales = computed(() => {
  const picked = cmpModels.value.filter((m) => selected.value.has(m.id))
  if (!picked.length) return [] as number[]
  return picked.reduce<number[]>(
    (acc, m) => acc.filter((s) => m.scale.includes(s)),
    [...picked[0].scale],
  )
})

const scale = ref(2)
watch(commonScales, (list) => {
  if (list.length && !list.includes(scale.value)) scale.value = Math.max(...list)
})

// ---- 运行与轮询 ----
const job = ref<CompareJob | null>(null)
const running = ref(false)
let pollTimer: ReturnType<typeof setTimeout> | null = null
let pollFailures = 0

function stopPolling() {
  if (pollTimer) clearTimeout(pollTimer)
  pollTimer = null
}
onBeforeUnmount(stopPolling)

function startPolling(id: string) {
  stopPolling()
  const tick = async () => {
    try {
      job.value = await api.compareStatus(id)
      pollFailures = 0
    } catch {
      if (++pollFailures >= 10) {
        stopPolling()
        running.value = false
        message.error('对比状态查询失败（后端可能已退出）')
        return
      }
    }
    if (job.value && ['done', 'failed', 'canceled'].includes(job.value.status)) {
      running.value = false
      stopPolling()
      return
    }
    pollTimer = setTimeout(tick, 800)
  }
  tick()
}

const canRun = computed(
  () =>
    srcReady.value &&
    selected.value.size >= 1 &&
    commonScales.value.includes(scale.value) &&
    !running.value,
)

async function run() {
  if (!canRun.value) return
  running.value = true
  try {
    const j = await api.createCompare({
      kind: mode.value,
      input: mode.value === 'video' ? videoInput.value : imageInput.value,
      ...(mode.value === 'video'
        ? { start_s: segStart.value, end_s: segStart.value + segDur.value }
        : {}),
      models: [...selected.value],
      scale: scale.value,
    })
    job.value = j
    stillIdx.value = 0
    startPolling(j.id)
  } catch (e) {
    running.value = false
    message.error(`无法开始对比: ${e instanceof Error ? e.message : e}`)
  }
}

async function cancelJob() {
  if (job.value) await api.cancelCompare(job.value.id)
}

function reset() {
  stopPolling()
  job.value = null
  running.value = false
  curModel.value = ''
  stillIdx.value = 0
}

const modelName = (id: string) =>
  store.models.find((m) => m.id === id)?.name ?? id

// ---- 结果视图：与原版分割线对比 + 模型切换（与任务全页对比同款交互） ----
const doneEntries = computed(() =>
  (job.value?.entries ?? []).filter((e) => e.status === 'done' && e.has_output))
const curModel = ref('')
watch(doneEntries, (list) => {
  if (!list.length) {
    curModel.value = ''
    return
  }
  if (!list.some((e) => e.model_id === curModel.value)) curModel.value = list[0].model_id
})
const curEntry = computed(() =>
  doneEntries.value.find((e) => e.model_id === curModel.value) ?? null)

// 视频模式：总览网格/静帧/成片三种对比载体；图片只有网格与静帧
const view = ref<'grid' | 'frames' | 'video'>('frames')
// 静帧多帧样本：切片段均匀取 4 帧（后端避黑选定），源/模型同索引=同时间戳
const stillIdx = ref(0)
const stillCount = computed(() => job.value?.still_count ?? 1)

/** 资产 key 按作业形态分：视频的源静帧/成品多帧在 stills/ 子目录带索引；
 * 图片作业源图是平铺 src_still、成品就是 <mid>.png（无索引）——图片此前
 * 被照搬视频的带索引 key，静帧与总览全部 404（v0.2.10 起的存量缺陷） */
function srcKey(i: number): string {
  return job.value?.kind === 'image' ? 'src_still' : `src_still/${i}`
}
function outKey(mid: string, i: number): string {
  return job.value?.kind === 'image' ? `still/${mid}` : `still/${mid}/${i}`
}

const srcStillUrl = computed(() =>
  job.value ? compareAssetUrl(job.value.id, srcKey(stillIdx.value)) : '')
const outStillUrl = computed(() =>
  job.value && curModel.value
    ? compareAssetUrl(job.value.id, outKey(curModel.value, stillIdx.value)) : '')
const srcVideoUrl = computed(() =>
  job.value ? compareAssetUrl(job.value.id, 'seg') : '')
const outVideoUrl = computed(() =>
  job.value && curModel.value
    ? compareAssetUrl(job.value.id, `out/${curModel.value}`) : '')

/** 数字键 1~6 快速切换模型（←/→ 已被分割线占用）；[ ] 切换静帧样本 */
function onKey(e: KeyboardEvent) {
  if (!job.value || e.target instanceof HTMLInputElement) return
  const n = parseInt(e.key, 10)
  if (n >= 1 && n <= doneEntries.value.length) {
    curModel.value = doneEntries.value[n - 1].model_id
  } else if (e.key === '[' || e.key === ']') {
    if (view.value !== 'frames' || stillCount.value <= 1) return
    stillIdx.value = e.key === '['
      ? Math.max(0, stillIdx.value - 1)
      : Math.min(stillCount.value - 1, stillIdx.value + 1)
  }
}
// 本页 KeepAlive 常驻：卸载不再发生，切页时须手动摘掉全局键盘监听
// （否则在别的页敲数字会切到看不见的对比模型）
onMounted(() => window.addEventListener('keydown', onKey))
onActivated(() => window.addEventListener('keydown', onKey)) // addEventListener 同函数幂等
onDeactivated(() => window.removeEventListener('keydown', onKey))
onBeforeUnmount(() => window.removeEventListener('keydown', onKey))

/** 用此模型发起正式任务：视频→新建任务（预填源视频），图片→图片超分页 */
function useModel(mid: string) {
  ui.pendingModel = mid
  ui.pendingScale = scale.value
  if (mode.value === 'video' && videoInput.value) openWizardWith(videoInput.value)
  else ui.page = 'imagesr'
}
</script>

<template>
  <div class="cmp-page">
    <div class="page-head">
      <div>
        <h1>模型对比</h1>
        <p class="sub">同一段素材并排跑多个模型——看画质差异、比处理速度，选出适合的那一个</p>
      </div>
    </div>

    <template v-if="!job">
      <!-- ① 素材 -->
      <section class="sec">
        <h2 class="sec-title"><span class="sec-num">1</span>选择素材</h2>
        <NRadioGroup v-model:value="mode" size="small">
          <NRadioButton value="video">视频片段</NRadioButton>
          <NRadioButton value="image">单张图片</NRadioButton>
        </NRadioGroup>

        <div v-if="mode === 'video'" class="src-box">
          <NButton dashed block size="large" @click="pickVideo">
            {{ videoInput ? '重新选择视频' : '点击选择视频（截取一小段做对比）' }}
          </NButton>
          <template v-if="videoInput">
            <div class="video-row">
              <video :src="mediaSrc(videoInput)" controls muted class="src-video" />
              <div class="src-info">
                <div v-if="probing" class="hint">正在读取视频信息…</div>
                <template v-else-if="probeInfo?.ok">
                  <div class="kv">分辨率 <b>{{ probeInfo.width }}x{{ probeInfo.height }}</b></div>
                  <div class="kv">时长 <b>{{ fmtT(probeInfo.duration_s) }}</b> · 帧率 <b>{{ probeInfo.fps }}</b></div>
                  <div class="slider-row">
                    <span class="lbl">起点 {{ fmtT(segStart) }}</span>
                    <NSlider v-model:value="segStart" :min="0" :max="maxStart" :step="0.5" :format-tooltip="(v: number) => fmtT(v)" />
                  </div>
                  <div class="slider-row">
                    <span class="lbl">时长 {{ fmtT(segDur) }}</span>
                    <NSlider v-model:value="segDur" :min="1" :max="maxDur" :step="0.5" :format-tooltip="(v: number) => fmtT(v)" />
                  </div>
                  <p class="hint">对比片段最长 {{ MAX_SEG_S }} 秒——足够看出画质与速度差异，又不必等太久</p>
                </template>
              </div>
            </div>
          </template>
        </div>

        <div v-else class="src-box">
          <NButton dashed block size="large" @click="pickImage">
            {{ imageInput ? '重新选择图片' : '点击选择图片' }}
          </NButton>
          <img v-if="imageInput" :src="mediaSrc(imageInput)" class="src-img" alt="" />
        </div>
      </section>

      <!-- ② 模型 -->
      <section class="sec">
        <h2 class="sec-title">
          <span class="sec-num">2</span>选择模型（{{ selected.size }}/{{ MAX_MODELS }}）
          <span class="sel-chip" :class="{ on: selected.size >= 1 }">
            {{ selected.size >= 1 ? `将对比 ${selected.size} 个模型` : '至少选 1 个' }}
          </span>
        </h2>
        <div class="model-grid">
          <div
            v-for="m in cmpModels"
            :key="m.id"
            class="model-card"
            :class="{ selected: selected.has(m.id), disabled: !m.vram_ok }"
            @click="toggle(m.id, m.vram_ok)"
          >
            <span v-if="selected.has(m.id)" class="m-check">✓</span>
            <div class="m-head">
              <span class="m-name">{{ m.name }}</span>
              <NTag v-if="!m.installed && !m.bundled" size="tiny" :bordered="false" type="warning">需下载 {{ m.size_mb }}MB</NTag>
              <NTag v-if="!m.vram_ok" size="tiny" :bordered="false" type="error">显存不足</NTag>
            </div>
            <div class="m-desc">{{ m.description }}</div>
            <div class="m-tags">
              <span>x{{ m.scale.join('/x') }}</span>
              <span>{{ m.vram_gb }}GB 显存</span>
            </div>
          </div>
        </div>
      </section>

      <!-- ③ 倍率 -->
      <section class="sec">
        <h2 class="sec-title"><span class="sec-num">3</span>放大倍数</h2>
        <NRadioGroup v-if="commonScales.length" v-model:value="scale" size="small">
          <NRadioButton v-for="s in commonScales" :key="s" :value="s">x{{ s }}</NRadioButton>
        </NRadioGroup>
        <p v-else class="hint">所选模型没有共同支持的倍率，请调整模型组合（或选倍率集合有交集的模型）</p>
      </section>

      <div class="footer-bar">
        <span class="hint-inline">
          {{ mode === 'video'
            ? `将用 ${selected.size || 0} 个模型依次处理 ${fmtT(segDur)} 的片段（每个模型加载一次引擎）`
            : `将用 ${selected.size || 0} 个模型依次处理这张图片` }}
        </span>
        <NButton type="primary" :loading="running" :disabled="!canRun" @click="run">
          开始对比
        </NButton>
      </div>
    </template>

    <!-- 运行中 / 结果 -->
    <template v-else>
      <section class="sec">
        <div class="res-head">
          <h2 class="sec-title no-num">
            {{ job.status === 'running' ? '对比进行中…' :
               job.status === 'done' ? '对比完成' :
               job.status === 'canceled' ? '已取消（已完成的模型结果保留）' :
               job.status === 'failed' ? `对比失败：${job.error ?? ''}` : '排队中…' }}
          </h2>
          <NSpace :size="8">
            <NButton v-if="running" size="small" @click="cancelJob">取消</NButton>
            <NButton size="small" @click="reset">换一批再比</NButton>
          </NSpace>
        </div>

        <div class="entry-list">
          <div
            v-for="e in job.entries"
            :key="e.model_id"
            class="entry"
            :class="[e.status, { active: e.model_id === curModel && e.status === 'done' }]"
            role="button"
            @click="e.status === 'done' && (curModel = e.model_id)"
          >
            <span class="e-name">{{ modelName(e.model_id) }}</span>
            <span class="e-status">
              <template v-if="e.status === 'queued'">等待</template>
              <template v-else-if="e.status === 'running'">{{ Math.round(e.pct * 100) }}%</template>
              <template v-else-if="e.status === 'done'">
                完成 · {{ job.kind === 'video' ? `${e.fps.toFixed(1)} fps` : '' }} {{ e.elapsed_s }}s · {{ e.out_w }}x{{ e.out_h }}
              </template>
              <template v-else-if="e.status === 'canceled'">已跳过</template>
              <template v-else>失败: {{ e.error }}</template>
            </span>
            <NProgress
              v-if="e.status === 'running'"
              :percentage="Math.round(e.pct * 100)"
              :height="6"
              :show-indicator="false"
              class="e-bar"
            />
          </div>
        </div>
      </section>

      <!-- 对比舞台：源 vs 当前模型，分割线交互；按钮/数字键切换模型 -->
      <section v-if="curEntry" ref="stageSec" class="sec stage-sec">
        <div class="stage-bar">
          <div class="switcher">
            <NButton
              v-for="(e, i) in doneEntries"
              :key="e.model_id"
              size="small"
              :type="e.model_id === curModel ? 'primary' : 'default'"
              :secondary="e.model_id !== curModel"
              @click="curModel = e.model_id"
            >
              {{ i + 1 }}. {{ modelName(e.model_id) }}
            </NButton>
          </div>
          <NRadioGroup
            v-model:value="view"
            size="small"
            class="view-toggle"
          >
            <NRadioButton value="grid">总览</NRadioButton>
            <NRadioButton value="frames">静帧</NRadioButton>
            <NRadioButton v-if="job.kind === 'video'" value="video">成片</NRadioButton>
          </NRadioGroup>
          <NButton size="small" class="fs-toggle" @click="toggleStageFull">
            {{ stageFull ? '退出全屏（ESC）' : '⛶ 全屏' }}
          </NButton>
        </div>

        <div class="stage">
          <VideoCompare
            v-if="job.kind === 'video' && view === 'video'"
            :src-url="srcVideoUrl"
            :out-url="outVideoUrl"
            :streaming="running"
          />
          <CompareSlider v-else-if="view === 'frames'" :src-url="srcStillUrl" :out-url="outStillUrl" />
          <!-- 总览网格：源 vs 各模型同帧并排，一眼比完所有候选；点卡片进分割线细看 -->
          <div v-else class="grid-view">
            <div
              class="g-card"
              role="button"
              :title="'原片（点此回静帧细看）'"
              @click="view = 'frames'"
            >
              <img :src="compareAssetUrl(job.id, srcKey(0))" alt="原片" loading="lazy" />
              <div class="g-name">原片</div>
              <div class="g-meta">{{ job.input.split(/[\\/]/).pop() }}</div>
            </div>
            <div
              v-for="e in doneEntries"
              :key="e.model_id"
              class="g-card"
              :class="{ cur: e.model_id === curModel }"
              role="button"
              :title="`点此进入分割线对比：${modelName(e.model_id)}`"
              @click="curModel = e.model_id; stillIdx = 0; view = 'frames'"
            >
              <img :src="compareAssetUrl(job.id, outKey(e.model_id, 0))" alt="" loading="lazy" />
              <div class="g-name">{{ modelName(e.model_id) }}</div>
              <div class="g-meta">
                {{ job.kind === 'video' ? `${e.fps.toFixed(1)} fps · ` : '' }}{{ e.out_w }}x{{ e.out_h }}
              </div>
            </div>
          </div>
        </div>

        <!-- 静帧样本条：缩略图取源帧，点选/[ ] 切换；切模型保持当前帧 -->
        <div
          v-if="job.kind === 'video' && view === 'frames' && stillCount > 1"
          class="frame-strip"
        >
          <button
            v-for="i in stillCount"
            :key="i"
            class="f-thumb"
            :class="{ on: stillIdx === i - 1 }"
            :title="`样本帧 ${i}/${stillCount}`"
            @click="stillIdx = i - 1"
          >
            <img :src="compareAssetUrl(job.id, `src_still/${i - 1}`)" alt="" loading="lazy" />
            <span class="f-idx">{{ i }}</span>
          </button>
        </div>

        <div class="stage-foot">
          <span class="hint-inline">
            数字键 1~{{ doneEntries.length }} 切换模型 · 拖动分割线对比原版（←/→ 微调）{{ stillCount > 1 ? ' · [ ] 换静帧样本' : '' }} ·
            当前：{{ curEntry.out_w }}x{{ curEntry.out_h }} ·
            {{ job.kind === 'video' ? `${curEntry.fps.toFixed(1)} fps · ` : '' }}{{ curEntry.elapsed_s }}s
          </span>
          <NButton size="small" type="primary" @click="useModel(curEntry.model_id)">
            {{ job.kind === 'video' ? '用此模型处理完整视频 →' : '用此模型处理图片 →' }}
          </NButton>
        </div>
      </section>
    </template>
  </div>
</template>

<script lang="ts">
export default { name: 'CompareModels' }
</script>

<style scoped>
.cmp-page {
  display: flex;
  flex-direction: column;
  gap: 18px;
  min-height: 100%;
}
h1 { font-size: 21px; font-weight: 750; letter-spacing: 0.3px; }
.sub { font-size: 12.5px; color: #9aa1ad; margin-top: 4px; }

.sec {
  display: flex;
  flex-direction: column;
  gap: 12px;
  background: linear-gradient(180deg, #1c2027, #181b21);
  border: 1px solid rgba(255, 255, 255, 0.06);
  border-radius: 14px;
  padding: 16px 18px;
}
/* 全屏态的舞台段独占整屏，去掉面板画布感 */
.stage-sec {
  background: none;
  border: none;
  border-radius: 0;
  padding: 0;
}
.sec-title {
  font-size: 15px;
  font-weight: 650;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.sec-title.no-num { gap: 10px; }
.sel-chip { margin-left: auto; font-size: 12px; font-weight: 400; color: #9aa1ad; }
.sel-chip.on { color: #6fa0ff; }
.sec-num {
  width: 20px; height: 20px; border-radius: 6px;
  background: var(--sv-grad);
  color: #fff; font-size: 12px; font-weight: 600;
  display: inline-flex; align-items: center; justify-content: center;
  flex-shrink: 0;
  box-shadow: 0 0 10px rgba(79, 140, 255, 0.3);
}
.hint { color: #9aa1ad; font-size: 12px; line-height: 1.55; }
.hint-inline { color: #9aa1ad; font-size: 12px; }

.src-box { display: flex; flex-direction: column; gap: 12px; }
.video-row { display: flex; gap: 16px; align-items: flex-start; flex-wrap: wrap; }
.src-video { width: min(420px, 100%); border-radius: 8px; background: #000; }
.src-info { flex: 1; min-width: 260px; display: flex; flex-direction: column; gap: 8px; }
.kv { font-size: 13px; color: #9aa1ad; }
.kv b { color: #e9ecf2; }
.slider-row { display: flex; align-items: center; gap: 12px; }
.slider-row .lbl { font-size: 12.5px; color: #e9ecf2; width: 86px; flex-shrink: 0; }
.slider-row :deep(.n-slider) { flex: 1; }
.src-img { max-width: min(560px, 100%); border-radius: 10px; border: 1px solid rgba(255, 255, 255, 0.08); }

.model-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px; }
.model-card {
  position: relative;
  border: 1.5px solid rgba(255, 255, 255, 0.07);
  border-radius: 12px;
  padding: 14px;
  cursor: pointer;
  background: rgba(255, 255, 255, 0.02);
  transition: border-color 0.16s, background 0.16s, transform 0.16s, box-shadow 0.16s;
}
.model-card:hover { border-color: rgba(255, 255, 255, 0.16); transform: translateY(-2px); }
.model-card.selected {
  border-color: #4f8cff;
  background: linear-gradient(180deg, rgba(79, 140, 255, 0.1), rgba(139, 92, 246, 0.05));
  box-shadow: 0 0 0 1px rgba(79, 140, 255, 0.45), 0 6px 18px rgba(79, 140, 255, 0.16);
}
.model-card.disabled { opacity: 0.45; cursor: not-allowed; }
.m-check {
  position: absolute; top: 10px; right: 10px;
  width: 18px; height: 18px; border-radius: 50%;
  background: var(--sv-grad); color: #fff; font-size: 11px;
  display: inline-flex; align-items: center; justify-content: center;
  box-shadow: 0 0 10px rgba(79, 140, 255, 0.5);
}
.m-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.m-name { font-weight: 650; font-size: 14px; }
.m-desc { color: #9aa1ad; font-size: 12px; margin: 6px 0; }
.m-tags { display: flex; gap: 10px; font-size: 12px; color: #7c838f; }

.footer-bar {
  position: sticky; bottom: 0; margin-top: auto;
  display: flex; justify-content: space-between; align-items: center;
  padding: 12px 16px; background: rgba(20, 23, 29, 0.88);
  border: 1px solid rgba(255, 255, 255, 0.07);
  border-radius: 14px;
  box-shadow: 0 -6px 24px rgba(0, 0, 0, 0.3), 0 8px 22px rgba(0, 0, 0, 0.25);
  z-index: 5;
  gap: 12px;
}

.res-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
.entry-list { display: flex; flex-direction: column; gap: 8px; }
.entry {
  display: grid; grid-template-columns: 160px 1fr; gap: 12px; align-items: center;
  background: linear-gradient(180deg, #1c2027, #181b21); border: 1px solid rgba(255, 255, 255, 0.06); border-radius: 10px;
  padding: 10px 14px;
  transition: border-color 0.15s, background 0.15s;
}
.entry .e-name { font-size: 13px; font-weight: 600; }
.entry .e-status { font-size: 12.5px; color: #9aa1ad; }
.entry.done { cursor: pointer; }
.entry.done:hover { border-color: rgba(255, 255, 255, 0.16); }
.entry.done.active { border-color: #4f8cff; background: rgba(79, 140, 255, 0.08); box-shadow: 0 0 0 1px rgba(79, 140, 255, 0.35); }
.entry.done .e-status { color: #34d399; }
.entry.failed .e-status { color: #f87171; }
.entry.running { grid-template-columns: 160px 1fr 220px; }
.entry.canceled { opacity: 0.6; }

.stage-sec { gap: 10px; }
.stage-bar {
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
}
.switcher { display: flex; gap: 8px; flex-wrap: wrap; }
.view-toggle { margin-left: auto; }
.fs-toggle { margin-left: auto; }
.stage-sec:fullscreen .view-toggle { margin-left: 0; }
/* 全屏态：舞台段铺满整屏，画布吃掉剩余空间（数字键/分割线交互照常） */
.stage-sec:fullscreen {
  background: #0d0e10;
  padding: 14px 20px;
}
.stage-sec:fullscreen .stage {
  height: auto;
  min-height: 0;
  flex: 1;
}
/* 图片模式没有 view-toggle，全屏按钮顶到右侧 */
.stage-sec:fullscreen .fs-toggle { margin-left: auto; }
.stage {
  height: min(62vh, 640px);
  min-height: 320px;
  border: 1px solid rgba(255, 255, 255, 0.07);
  border-radius: 12px;
  background: var(--sv-panel-deep);
  overflow: hidden;
}
/* 总览网格：源 + 各模型并排缩略，卡片悬停/当前模型描边 */
.grid-view {
  height: 100%;
  overflow-y: auto;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 10px;
  padding: 12px;
  align-content: start;
}
.g-card {
  cursor: pointer;
  border: 2px solid transparent;
  border-radius: 10px;
  overflow: hidden;
  background: rgba(255, 255, 255, 0.02);
  transition: border-color 0.15s, transform 0.15s;
}
.g-card:hover { border-color: rgba(255, 255, 255, 0.2); transform: translateY(-1px); }
.g-card.cur { border-color: #4f8cff; box-shadow: 0 0 12px rgba(79, 140, 255, 0.3); }
.g-card img {
  width: 100%;
  aspect-ratio: 16/9;
  object-fit: contain;
  background: #000;
  display: block;
}
.g-name {
  padding: 6px 8px 0;
  font-size: 12.5px;
  font-weight: 600;
  color: #e9ecf2;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.g-meta {
  padding: 2px 8px 8px;
  font-size: 11px;
  color: #9aa1ad;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.frame-strip { display: flex; gap: 8px; }
.f-thumb {
  position: relative;
  width: 104px;
  height: 58px;
  padding: 0;
  border: 2px solid #2a2d31;
  border-radius: 6px;
  overflow: hidden;
  cursor: pointer;
  background: #0d0e10;
  flex-shrink: 0;
}
.f-thumb:hover { border-color: #4a4f55; }
.f-thumb.on { border-color: #4f8cff; }
.f-thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}
.f-idx {
  position: absolute;
  left: 4px;
  bottom: 3px;
  min-width: 14px;
  text-align: center;
  font-size: 10px;
  line-height: 14px;
  border-radius: 4px;
  background: rgba(0, 0, 0, 0.65);
  color: #c8cdd4;
}
.f-thumb.on .f-idx { background: rgba(79, 140, 255, 0.85); color: #fff; }
.stage-foot {
  display: flex; justify-content: space-between; align-items: center; gap: 12px;
  flex-wrap: wrap;
}
</style>
