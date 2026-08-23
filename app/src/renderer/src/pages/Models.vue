<script setup lang="ts">
import { computed, ref } from 'vue'
import {
  NButton,
  NCard,
  NEmpty,
  NProgress,
  NSpace,
  NTag,
  useMessage,
} from 'naive-ui'
import { api } from '../api'
import { refreshModels, store } from '../store'

const message = useMessage()
const tab = ref<'all' | 'anime' | 'general'>('all')

const models = computed(() =>
  store.models.filter((m) => tab.value === 'all' || m.content.includes(tab.value)),
)

const speedLabel = { fast: '⚡ 快速', balanced: '⚖ 均衡', slow: '🐢 高质量慢速' }
const contentLabel = { anime: '动漫', general: '真人/通用' }

async function onDownload(id: string) {
  const r = await api.downloadModel(id)
  if (!r.ok && r.status !== 409) message.error(`下载启动失败: ${r.status}`)
  refreshModels()
}

async function onDelete(id: string) {
  const r = await api.deleteModel(id)
  if (r.ok) {
    message.success('已删除已下载的权重（内置模型仍可继续使用）')
    refreshModels()
  } else {
    message.error(`删除失败: ${r.status}`)
  }
}
</script>

<template>
  <div class="models-page">
    <div class="page-head">
      <div>
        <h1>模型市场</h1>
        <p class="sub">按需下载 · sha256 校验 · 未安装的模型在任务启动时也会自动下载</p>
      </div>
      <NSpace>
        <NButton size="small" :type="tab === 'anime' ? 'primary' : 'default'" @click="tab = 'anime'">动漫</NButton>
        <NButton size="small" :type="tab === 'general' ? 'primary' : 'default'" @click="tab = 'general'">真人/通用</NButton>
        <NButton size="small" :type="tab === 'all' ? 'primary' : 'default'" @click="tab = 'all'">全部</NButton>
      </NSpace>
    </div>

    <NEmpty v-if="!models.length" description="该分类暂无模型" style="margin-top: 12vh" />

    <NSpace vertical :size="14">
      <NCard v-for="m in models" :key="m.id" size="small" class="model-card">
        <div class="row">
          <div class="info">
            <div class="name-row">
              <span class="name">{{ m.name }}</span>
              <NTag v-if="m.bundled" size="small" type="success" :bordered="false">内置</NTag>
              <NTag v-else-if="m.installed" size="small" type="success" :bordered="false">已安装</NTag>
              <NTag v-if="!m.vram_ok" size="small" type="warning" :bordered="false">显存不足</NTag>
            </div>
            <div class="desc">{{ m.description }}</div>
            <div class="tags">
              <NTag size="small" :bordered="false">{{ speedLabel[m.speed as 'fast'] }}</NTag>
              <NTag size="small" :bordered="false" v-for="c in m.content" :key="c">
                {{ contentLabel[c as 'anime'] ?? c }}
              </NTag>
              <NTag size="small" :bordered="false">原生 x{{ m.scale.join(' / x') }}</NTag>
              <NTag size="small" :bordered="false">≈{{ m.vram_gb }}GB 显存</NTag>
              <NTag size="small" :bordered="false">{{ m.size_mb }}MB</NTag>
            </div>
            <div v-if="m.vram_note" class="warn">{{ m.vram_note }}</div>
          </div>
          <div class="actions">
            <template v-if="m.bundled">
              <span class="bundled-note">随软件分发</span>
            </template>
            <template v-else-if="store.downloadProgress[m.id] !== undefined">
              <div class="dl">
                <NProgress
                  type="line"
                  :percentage="Math.round(store.downloadProgress[m.id] * 100)"
                  :show-indicator="false"
                  :height="8"
                />
                <span class="dl-pct">{{ Math.round(store.downloadProgress[m.id] * 100) }}%</span>
              </div>
            </template>
            <template v-else-if="m.installed">
              <NButton size="small" quaternary type="error" @click="onDelete(m.id)">删除权重</NButton>
            </template>
            <template v-else>
              <NButton size="small" type="primary" ghost @click="onDownload(m.id)">下载 ({{ m.size_mb }}MB)</NButton>
            </template>
          </div>
        </div>
      </NCard>
    </NSpace>
  </div>
</template>

<style scoped>
.models-page { display: flex; flex-direction: column; gap: 16px; }
.page-head { display: flex; justify-content: space-between; align-items: center; }
h1 { font-size: 20px; font-weight: 700; }
.sub { font-size: 12.5px; color: #9aa0a6; margin-top: 4px; }
.row { display: flex; justify-content: space-between; gap: 20px; }
.info { min-width: 0; flex: 1; }
.name-row { display: flex; align-items: center; gap: 8px; }
.name { font-size: 15px; font-weight: 600; }
.desc { color: #9aa0a6; font-size: 12.5px; margin: 6px 0; }
.tags { display: flex; flex-wrap: wrap; gap: 6px; }
.warn { margin-top: 6px; color: #fbbf24; font-size: 12px; }
.actions { display: flex; flex-direction: column; justify-content: center; gap: 8px; min-width: 180px; }
.bundled-note { color: #34d399; font-size: 12.5px; }
.dl { width: 180px; display: flex; align-items: center; gap: 8px; }
.dl-pct { font-size: 12px; color: #4f8cff; width: 40px; text-align: right; }
</style>
