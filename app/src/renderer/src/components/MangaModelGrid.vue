<script setup lang="ts">
import { NTag } from 'naive-ui'
import type { ModelInfo } from '../api'

/** 漫画页模型卡片网格：单选高亮 + 点击上抛（选中纪律/倍率回落由父页处理）。 */
const props = defineProps<{
  models: ModelInfo[]
  selected: string
}>()
const emit = defineEmits<{ (e: 'select', id: string): void }>()

const contentLabel = { anime: '动漫', comic: '漫画', general: '真人/通用', real: '真人/通用' } as Record<string, string>

function onClick(m: ModelInfo) {
  if (!m.vram_ok) return
  emit('select', m.id)
}
</script>

<template>
  <div class="model-grid">
    <div
      v-for="m in props.models"
      :key="m.id"
      class="model-card"
      :class="{ selected: props.selected === m.id, disabled: !m.vram_ok }"
      @click="onClick(m)"
    >
      <span v-if="props.selected === m.id" class="m-check">✓</span>
      <div class="m-head">
        <span class="m-name">{{ m.name }}</span>
        <NTag v-if="!m.installed && !m.bundled" size="tiny" :bordered="false" type="warning">需下载 {{ m.size_mb }}MB</NTag>
        <NTag v-if="!m.vram_ok" size="tiny" :bordered="false" type="error">显存不足</NTag>
        <span v-if="(m.scenes ?? ['video', 'image']).some((s) => s !== 'image')" class="m-scenes">
          <NTag v-for="s in (m.scenes ?? []).filter((k) => k !== 'image')" :key="s"
            size="tiny" type="info" :bordered="false">{{ s === 'manga' ? '漫画' : '视频' }}</NTag>
        </span>
      </div>
      <div class="m-desc">{{ m.description }}</div>
      <div class="m-tags">
        <span>x{{ m.scale.join('/x') }}</span>
        <span>{{ m.vram_gb }}GB 显存</span>
        <span v-for="c in m.content" :key="c" class="m-content">{{ contentLabel[c] ?? c }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.model-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px; }
.model-card {
  position: relative;
  border: 1.5px solid var(--sv-border-mid);
  border-radius: var(--sv-radius-md);
  padding: 14px;
  cursor: pointer;
  background: var(--sv-fill-1);
  transition: border-color 0.16s, background 0.16s, transform 0.16s, box-shadow 0.16s;
}
.model-card:hover { border-color: var(--sv-border-strong); transform: translateY(-2px); }
.model-card.selected {
  border-color: var(--sv-accent);
  background: linear-gradient(180deg, rgba(var(--sv-accent-rgb), 0.1), rgba(var(--sv-accent2-rgb), 0.05));
  box-shadow: 0 0 0 1px rgba(var(--sv-accent-rgb), 0.45), 0 6px 18px rgba(var(--sv-accent-rgb), 0.16);
}
.model-card.disabled { opacity: 0.45; cursor: not-allowed; }
.m-check {
  position: absolute;
  top: 10px;
  right: 10px;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: var(--sv-grad);
  color: #fff;
  font-size: 11px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 0 10px rgba(var(--sv-accent-rgb), 0.5);
}
.m-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.m-scenes { margin-left: auto; display: inline-flex; gap: 4px; }
.m-name { font-weight: 600; font-size: 14px; }
.m-desc {
  color: var(--sv-text-faint);
  font-size: 12px;
  margin: 6px 0;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.m-tags { display: flex; gap: 10px; font-size: 12px; color: var(--sv-text-faint); flex-wrap: wrap; }
.m-content { color: var(--sv-text-dim); }
</style>
