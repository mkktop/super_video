<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { NButton, NEmpty, NModal, NRadioButton, NRadioGroup, NTag, useMessage } from 'naive-ui'
import { api } from '../api'
import type { TaskStills } from '../api'
import { store, ui } from '../store'
import { useFullscreen } from '../utils'
import CompareSlider from '../components/CompareSlider.vue'
import VideoCompare from '../components/VideoCompare.vue'

const message = useMessage()
const task = computed(() => store.tasks.find((t) => t.id === ui.compareTaskId))

/** 整页进真全屏：对比画面铺满显示器（该页本就隐藏侧栏，再上一层到屏幕级） */
const pageEl = ref<HTMLElement | null>(null)
const { isFullscreen: pageFull, toggle: togglePageFull } = useFullscreen(pageEl)
const canCompare = computed(() => !!task.value?.preview_src && !!task.value?.preview_path)
// 视频对比直接播输入/输出文件，只有已完成的任务有输出；
// 图片序列任务的输出是目录（不是可播放的视频文件），按扩展名排除
const canVideo = computed(
  () => task.value?.status === 'done' && !!task.value.input_path && !!task.value.output_path
    && /\.(mp4|m4v|mov|webm|mkv)$/i.test(task.value.output_path),
)
const mode = ref<'frames' | 'video'>('frames')
const busy = computed(() => task.value?.status === 'running' || task.value?.status === 'queued')

// ---- 多帧静帧：完成态任务懒构建（源/输出同时间戳成对抽帧），轮询到 ready ----
const stills = ref<TaskStills | null>(null)
const stillIdx = ref(0)
let stillsTimer: ReturnType<typeof setTimeout> | null = null
const stillsReady = computed(
  () => !!task.value && stills.value?.status === 'ready' && stills.value.count > 1)
const stillCount = computed(() => (stillsReady.value ? stills.value!.count : 1))
const stillsBuilding = computed(() => !!task.value && stills.value?.status === 'building')

async function pollStills(tries = 0) {
  const t = task.value
  if (!t || t.status !== 'done') return
  try {
    stills.value = await api.taskStills(t.id)
    stillIdx.value = Math.min(stillIdx.value, Math.max(0, (stills.value.count || 1) - 1))
  } catch {
    stills.value = null // 状态拿不到：回落单帧预览，不打断页面
    return
  }
  if (stills.value.status === 'building' && tries < 150) {
    stillsTimer = setTimeout(() => pollStills(tries + 1), 700)
  }
}

// 对比画面源：静帧就绪用样本对（built_at 做缓存版本号），否则单帧预览对
const sliderSrc = computed(() => {
  const t = task.value
  if (!t) return ''
  return stillsReady.value
    ? api.taskStillUrl(t.id, stillIdx.value, true, stills.value!.built_at ?? 0)
    : api.previewUrl(t.id, t.updated_at, true)
})
const sliderOut = computed(() => {
  const t = task.value
  if (!t) return ''
  return stillsReady.value
    ? api.taskStillUrl(t.id, stillIdx.value, false, stills.value!.built_at ?? 0)
    : api.previewUrl(t.id, t.updated_at)
})

const fileName = computed(() => task.value?.input_path.split(/[\\/]/).pop() ?? '')
const outName = computed(() => task.value?.output_path.split(/[\\/]/).pop() ?? '')
const modelName = computed(
  () => store.models.find((m) => m.id === task.value?.model_id)?.name ?? task.value?.model_id ?? '',
)
const scaleLabel = computed(() => {
  const t = task.value
  if (!t) return ''
  const tw = t.params?.target_w as number | undefined
  const th = t.params?.target_h as number | undefined
  if (tw && th) return `${t.src_w}x${t.src_h} → ${tw}x${th}`
  const k = Number(t.params?.target_scale ?? t.params?.scale ?? 4)
  return `${t.src_w}x${t.src_h} → ${t.src_w * k}x${t.src_h * k}`
})

function back() {
  ui.compareTaskId = null
  ui.page = 'tasks'
}

// ---- 分享卡片：源 vs 输出同时间点抽帧 → 长图 / 滑块动图 ----
const canShare = computed(() => task.value?.status === 'done')
const shareBusy = ref<'' | 'image' | 'gif'>('')
const sharePreview = ref<{ url: string; path: string; kind: string } | null>(null)
const shareShow = ref(false)
const showInFolder = (p: string) => window.sv.showInFolder(p)
async function makeShare(kind: 'image' | 'gif') {
  const t = task.value
  if (!t) return
  shareBusy.value = kind
  try {
    sharePreview.value = await api.createShareCard(t.id, kind)
    shareShow.value = true
  } catch (e) {
    message.error((e as Error).message || '生成失败')
  } finally {
    shareBusy.value = ''
  }
}
function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape') {
    // 全屏态下 ESC 交给浏览器原生退出全屏，别同时退回任务页
    if (pageFull.value) return
    back()
  }
  // [ ] 切换静帧样本（与模型对比页同键位；只在静帧视图）
  if ((e.key === '[' || e.key === ']') && mode.value === 'frames' && stillCount.value > 1) {
    stillIdx.value = e.key === '['
      ? Math.max(0, stillIdx.value - 1)
      : Math.min(stillCount.value - 1, stillIdx.value + 1)
  }
}
// 任务在页面上跑完（完成前几秒出首对预览，随后静帧可抽）→ 补一次轮询
watch(() => task.value?.status, (st, old) => {
  if (st === 'done' && st !== old) pollStills()
})
onMounted(() => {
  pollStills()
  window.addEventListener('keydown', onKey)
})
onUnmounted(() => {
  if (stillsTimer) clearTimeout(stillsTimer)
  window.removeEventListener('keydown', onKey)
})
</script>

<template>
  <div ref="pageEl" class="compare-page">
    <div class="head">
      <NButton size="small" quaternary @click="back">← 返回任务</NButton>
      <div class="names">
        <span class="file">{{ fileName }}</span>
        <span class="arrow">→</span>
        <span class="out">{{ outName }}</span>
      </div>
      <div class="tags">
        <NTag size="small" :bordered="false">{{ modelName }}</NTag>
        <NTag size="small" :bordered="false" type="info">{{ scaleLabel }}</NTag>
      </div>
      <NRadioGroup v-model:value="mode" size="small">
        <NRadioButton value="frames">静帧</NRadioButton>
        <NRadioButton value="video" :disabled="!canVideo">视频</NRadioButton>
      </NRadioGroup>
      <NButton v-if="canShare" size="small" :loading="shareBusy === 'image'" @click="makeShare('image')">
        📷 分享长图
      </NButton>
      <NButton v-if="canShare" size="small" :loading="shareBusy === 'gif'" @click="makeShare('gif')">
        🎞 分享动图
      </NButton>
      <NButton size="small" @click="togglePageFull">
        {{ pageFull ? '退出全屏（ESC）' : '⛶ 全屏' }}
      </NButton>
    </div>

    <div class="stage">
      <VideoCompare
        v-if="mode === 'video' && canVideo"
        :src-path="task!.input_path"
        :out-path="task!.output_path"
      />
      <CompareSlider
        v-else-if="canCompare || stillsReady"
        :src-url="sliderSrc"
        :out-url="sliderOut"
      />
      <NEmpty
        v-else
        :description="busy ? '任务还在处理中，完成前几秒会出现首对预览' : '该任务没有可对比的预览图'"
        class="empty"
      >
        <template #extra>
          <NButton size="small" @click="back">返回</NButton>
        </template>
      </NEmpty>
    </div>

    <!-- 静帧样本条：缩略图取源帧，点选/[ ] 切换（与模型对比页同款交互） -->
    <div v-if="mode === 'frames' && (stillsReady || stillsBuilding)" class="frame-strip">
      <template v-if="stillsReady">
        <button
          v-for="i in stillCount"
          :key="i"
          class="f-thumb"
          :class="{ on: stillIdx === i - 1 }"
          :title="`样本帧 ${i}/${stillCount}（[ ] 键切换）`"
          @click="stillIdx = i - 1"
        >
          <img :src="api.taskStillUrl(task!.id, i - 1, true, stills!.built_at ?? 0)" alt="" loading="lazy" />
          <span class="f-idx">{{ i }}</span>
        </button>
      </template>
      <span v-else class="strip-note">正在抽取样本帧…</span>
    </div>

    <!-- 分享卡片预览：长图/动图 + 打开产物位置 -->
    <NModal
      v-model:show="shareShow"
      :auto-focus="false"
      transform-origin="center"
    >
      <div v-if="sharePreview" class="share-modal">
        <div class="share-head">
          <span>分享卡片已生成{{ sharePreview.kind === 'gif' ? '（滑块动图 GIF）' : '（长图 PNG）' }}</span>
          <NButton size="tiny" quaternary @click="shareShow = false">关闭</NButton>
        </div>
        <img class="share-img" :src="api.shareCardUrl(task!.id, sharePreview.kind as 'image' | 'gif', Date.now())" />
        <div class="share-foot">
          <span class="share-path" :title="sharePreview.path">{{ sharePreview.path }}</span>
          <NButton size="small" type="primary" @click="showInFolder(sharePreview.path)">
            打开所在文件夹
          </NButton>
        </div>
      </div>
    </NModal>
  </div>
</template>

<style scoped>
.compare-page {
  display: flex;
  flex-direction: column;
  width: 100%; /* page-full 是横向 flex 容器，舞台内容全为绝对定位不撑宽，不显式给宽会塌缩成头部行宽 */
  height: 100%;
  gap: 10px;
}
/* 全屏态：整页铺满显示器，舞台吃掉头部以外全部空间 */
.compare-page:fullscreen {
  background: var(--sv-panel-deep);
  padding: 12px 16px;
}
:fullscreen .stage {
  border-radius: 6px;
}
.head {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-shrink: 0;
}
.names {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  overflow: hidden;
}
.file { font-weight: 600; white-space: nowrap; }
.arrow, .out { color: #9aa1ad; font-size: 13px; white-space: nowrap; }
.file, .out { overflow: hidden; text-overflow: ellipsis; }
.tags { display: flex; gap: 6px; flex-shrink: 0; margin-left: auto; }
.stage {
  flex: 1;
  min-height: 0;
  border: 1px solid rgba(255, 255, 255, 0.07);
  border-radius: 12px;
  background: var(--sv-panel-deep);
  box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.015);
}
.empty { margin: auto; }
/* 静帧样本条（样式与 CompareModels 同款，两页独立演化各自维护） */
.frame-strip {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
  align-items: center;
  min-height: 58px;
}
.f-thumb {
  position: relative;
  width: 104px;
  height: 58px;
  padding: 0;
  border: 2px solid rgba(255, 255, 255, 0.09);
  border-radius: 8px;
  overflow: hidden;
  cursor: pointer;
  background: var(--sv-panel-deep);
  flex-shrink: 0;
  transition: border-color 0.15s, transform 0.15s;
}
.f-thumb:hover { border-color: rgba(255, 255, 255, 0.22); transform: translateY(-1px); }
.f-thumb.on { border-color: #4f8cff; box-shadow: 0 0 10px rgba(79, 140, 255, 0.35); }
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
.strip-note { font-size: 12px; color: #9aa1ad; }

.share-modal {
  display: flex;
  flex-direction: column;
  gap: 10px;
  width: min(760px, 86vw);
  max-height: 86vh;
  background: linear-gradient(180deg, #1c2027, #181b21);
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 14px;
  padding: 14px 16px;
}
.share-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 14px;
  font-weight: 600;
}
.share-img {
  width: 100%;
  border-radius: 6px;
  overflow-y: auto;
}
.share-foot {
  display: flex;
  align-items: center;
  gap: 10px;
}
.share-path {
  flex: 1;
  min-width: 0;
  font-size: 12px;
  color: #9aa1ad;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
