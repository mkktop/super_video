<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, computed, ref } from 'vue'
import { NButton, NInput, NSwitch, useMessage } from 'naive-ui'
import { api } from '../api'

const message = useMessage()

const lines = ref<string[]>([])
const query = ref('')
const autoScroll = ref(true)
const box = ref<HTMLElement | null>(null)
let pinned = true // 用户是否停在底部;向上翻阅时不抢滚动位置
let timer: ReturnType<typeof setInterval> | null = null

// 子串过滤（不区分大小写）：排障时从 300 行里快速捞出相关行
const shown = computed(() => {
  const q = query.value.trim().toLowerCase()
  if (!q) return lines.value
  return lines.value.filter((l) => l.toLowerCase().includes(q))
})

async function load() {
  try {
    lines.value = (await api.logTail(300)).lines
    if (autoScroll.value && pinned) {
      await nextTick()
      if (box.value) box.value.scrollTop = box.value.scrollHeight
    }
  } catch {
    /* 日志拉取失败下个周期重试 */
  }
}

function onScroll() {
  if (!box.value) return
  pinned = box.value.scrollHeight - box.value.scrollTop - box.value.clientHeight < 40
}

async function exportLog() {
  const p = await window.sv.saveLog(lines.value.join('\n') || '(空)')
  if (p) message.success(`日志已导出: ${p}`)
}

function isErr(l: string): boolean {
  return /error|failed|traceback|exception|异常|失败/i.test(l)
}

onMounted(() => {
  load()
  timer = setInterval(() => {
    if (!document.hidden) load()
  }, 3000)
})
onUnmounted(() => {
  if (timer) clearInterval(timer)
})
</script>

<template>
  <div class="logs-page">
    <div class="page-head">
      <div>
        <h1>服务日志</h1>
        <p class="sub">sidecar 运行日志 · 最近 300 行 · 3 秒自动刷新</p>
      </div>
      <div class="head-actions">
        <NInput
          v-model:value="query"
          size="small"
          clearable
          placeholder="过滤关键字…"
          style="width: 200px"
        />
        <span class="as-label">自动滚动</span>
        <NSwitch v-model:value="autoScroll" size="small" />
        <NButton size="small" @click="load">刷新</NButton>
        <NButton size="small" @click="exportLog">导出</NButton>
      </div>
    </div>

    <div ref="box" class="log-box" @scroll="onScroll">
      <div v-if="!lines.length" class="log-empty">暂无日志</div>
      <div v-else-if="!shown.length" class="log-empty">没有匹配「{{ query.trim() }}」的行</div>
      <div
        v-for="(l, i) in shown"
        :key="i"
        class="log-line"
        :class="{ err: isErr(l) }"
      >{{ l }}</div>
    </div>
  </div>
</template>

<style scoped>
.logs-page { display: flex; flex-direction: column; gap: 14px; }
.page-head { display: flex; justify-content: space-between; align-items: center; gap: 16px; flex-wrap: wrap; }
h1 { font-size: 20px; font-weight: 700; }
.sub { font-size: 12.5px; color: #9aa0a6; margin-top: 4px; }
.head-actions { display: flex; align-items: center; gap: 10px; }
.as-label { font-size: 12.5px; color: #9aa0a6; }

.log-box {
  background: #101113;
  border: 1px solid #26292e;
  border-radius: 10px;
  padding: 12px 14px;
  height: calc(100vh - 175px);
  min-height: 240px;
  overflow-y: auto;
  font-family: Consolas, 'Courier New', monospace;
  font-size: 12px;
  line-height: 1.65;
  color: #c9cdd4;
}
.log-line { white-space: pre-wrap; word-break: break-all; }
.log-line.err { color: #f87171; }
.log-empty { color: #767d88; }
</style>
