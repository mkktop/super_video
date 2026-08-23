<script setup lang="ts">
import { NButton, NEmpty, NSpace } from 'naive-ui'
import TaskCard from '../components/TaskCard.vue'
import { store, ui } from '../store'
</script>

<template>
  <div class="tasks-page">
    <div class="page-head">
      <div>
        <h1>任务队列</h1>
        <p class="sub">严格串行执行 · 一次处理一个视频 · 每个任务参数独立</p>
      </div>
      <NButton type="primary" @click="ui.showNewTask = true">＋ 新建任务</NButton>
    </div>

    <NEmpty
      v-if="store.ready && store.tasks.length === 0"
      description="队列为空，点击右上角「新建任务」添加视频"
      style="margin-top: 15vh"
    />
    <NSpace v-else vertical :size="12">
      <TaskCard v-for="t in [...store.tasks].reverse()" :key="t.id" :task="t" />
    </NSpace>
    <div v-if="!store.ready" class="loading">正在连接后端服务…</div>
  </div>
</template>

<style scoped>
.tasks-page { display: flex; flex-direction: column; gap: 16px; }
.page-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
h1 { font-size: 20px; font-weight: 700; }
.sub { font-size: 12.5px; color: #9aa0a6; margin-top: 4px; }
.loading { margin-top: 30vh; text-align: center; color: #9aa0a6; }
</style>
