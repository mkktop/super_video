<script setup lang="ts">
import { onMounted } from 'vue'
import {
  NConfigProvider,
  NGlobalStyle,
  NMessageProvider,
  darkTheme,
  dateZhCN,
  zhCN,
  type GlobalThemeOverrides,
} from 'naive-ui'
import { initStore, ui } from './store'
import TitleBar from './components/TitleBar.vue'
import Sidebar from './components/Sidebar.vue'
import Home from './pages/Home.vue'
import NewTask from './pages/NewTask.vue'
import Trim from './pages/Trim.vue'
import Tasks from './pages/Tasks.vue'
import Models from './pages/Models.vue'
import Perf from './pages/Perf.vue'
import Logs from './pages/Logs.vue'
import Settings from './pages/Settings.vue'
import Compare from './pages/Compare.vue'

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
      <div class="app">
        <TitleBar />
        <div class="body">
          <Sidebar v-if="ui.page !== 'compare'" />
          <main class="page" :class="{ 'page-full': ui.page === 'compare' }">
            <!-- 新建任务页 v-show 常驻挂载：填一半切去别的页再回来，草稿不丢 -->
            <NewTask v-show="ui.page === 'newtask'" />
            <Home v-if="ui.page === 'home'" />
            <Trim v-else-if="ui.page === 'trim'" />
            <Tasks v-else-if="ui.page === 'tasks'" />
            <Models v-else-if="ui.page === 'models'" />
            <Perf v-else-if="ui.page === 'perf'" />
            <Logs v-else-if="ui.page === 'logs'" />
            <Compare v-else-if="ui.page === 'compare'" />
            <Settings v-else-if="ui.page === 'settings'" />
          </main>
        </div>
      </div>
    </n-message-provider>
  </n-config-provider>
</template>

<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body, #app { height: 100%; overflow: hidden; }
body {
  font-family: system-ui, 'Segoe UI', 'Microsoft YaHei', sans-serif;
  background: #141517;
  color: #e8eaed;
  -webkit-font-smoothing: antialiased;
}
.app { display: flex; flex-direction: column; height: 100vh; }
.body { display: flex; flex: 1; min-height: 0; }
.page {
  flex: 1;
  overflow-y: auto;
  padding: 22px 26px;
  min-width: 0;
}
.page-full {
  padding: 12px 14px;
  overflow: hidden;
  display: flex;
}
::-webkit-scrollbar { width: 10px; }
::-webkit-scrollbar-thumb { background: #33363b; border-radius: 5px; }
::-webkit-scrollbar-track { background: transparent; }
</style>
