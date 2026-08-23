<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  NBadge,
  NButton,
  NConfigProvider,
  NEmpty,
  NGlobalStyle,
  NMessageProvider,
  NSpace,
  NTag,
  darkTheme,
  dateZhCN,
  zhCN,
  type GlobalThemeOverrides,
} from 'naive-ui'
import { initStore, store } from './store'
import TaskCard from './components/TaskCard.vue'
import NewTaskModal from './components/NewTaskModal.vue'

const showModal = ref(false)

const themeOverrides: GlobalThemeOverrides = {
  common: {
    bodyColor: '#141517',
    cardColor: '#1E2023',
    modalColor: '#1E2023',
    popoverColor: '#26282C',
    primaryColor: '#4F8CFF',
    primaryColorHover: '#6FA0FF',
    primaryColorPressed: '#3B78E8',
    successColor: '#34D399',
    warningColor: '#FBBF24',
    errorColor: '#F87171',
    borderColor: '#2A2D31',
    dividerColor: '#2A2D31',
    borderRadius: '8px',
    fontSizeMedium: '14px',
  },
}

onMounted(() => {
  initStore()
})

const runningCount = computed(
  () => store.tasks.filter((t) => t.status === 'running' || t.status === 'queued').length,
)
</script>

<template>
  <n-config-provider
    :theme="darkTheme"
    :theme-overrides="themeOverrides"
    :locale="zhCN"
    :date-locale="dateZhCN"
  >
    <n-global-style />
    <n-message-provider>
      <div class="shell">
        <header class="topbar">
          <div class="brand">
            <span class="logo">⬆</span>
            <span class="title">super_video</span>
            <n-tag size="small" :bordered="false" type="info">M1</n-tag>
          </div>
          <div class="right">
            <n-tag v-if="store.gpuName" size="small" :bordered="false">{{ store.gpuName }}</n-tag>
            <n-badge dot :type="store.connected ? 'success' : 'error'" />
            <n-button type="primary" @click="showModal = true">＋ 新建任务</n-button>
          </div>
        </header>

        <main class="content">
          <n-empty
            v-if="store.ready && store.tasks.length === 0"
            description="队列为空，点击右上角「新建任务」添加视频"
            class="empty"
          />
          <n-space v-else vertical :size="12">
            <TaskCard v-for="t in [...store.tasks].reverse()" :key="t.id" :task="t" />
          </n-space>
          <div v-if="!store.ready" class="loading">正在连接后端服务…</div>
        </main>

        <NewTaskModal v-model:show="showModal" />
        <footer v-if="runningCount" class="footbar">
          队列中 {{ runningCount }} 个任务 · 严格串行执行（一次只跑一个，其余排队）
        </footer>
      </div>
    </n-message-provider>
  </n-config-provider>
</template>

<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: system-ui, 'Segoe UI', 'Microsoft YaHei', sans-serif;
  background: #141517;
  color: #e8eaed;
  -webkit-font-smoothing: antialiased;
}
.shell { display: flex; flex-direction: column; height: 100vh; }
.topbar {
  display: flex; justify-content: space-between; align-items: center;
  padding: 14px 20px; border-bottom: 1px solid #2a2d31; background: #191b1e;
}
.brand { display: flex; align-items: center; gap: 10px; }
.logo { font-size: 20px; }
.title { font-size: 17px; font-weight: 600; letter-spacing: 0.5px; }
.right { display: flex; align-items: center; gap: 12px; }
.content { flex: 1; overflow-y: auto; padding: 16px 20px; }
.empty { margin-top: 15vh; }
.loading { margin-top: 40vh; text-align: center; color: #9aa0a6; }
.footbar {
  padding: 8px 20px; text-align: center; font-size: 12px; color: #9aa0a6;
  border-top: 1px solid #2a2d31; background: #191b1e;
}
::-webkit-scrollbar { width: 10px; }
::-webkit-scrollbar-thumb { background: #33363b; border-radius: 5px; }
::-webkit-scrollbar-track { background: transparent; }
</style>
