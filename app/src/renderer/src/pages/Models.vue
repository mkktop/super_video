<script setup lang="ts">
import { computed, ref } from 'vue'
import {
  NButton,
  NCard,
  NEmpty,
  NForm,
  NFormItem,
  NInput,
  NInputNumber,
  NModal,
  NProgress,
  NRadioGroup,
  NRadioButton,
  NSelect,
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

// ---- 自定义模型导入 ----
const showImport = ref(false)
const importing = ref(false)
const impPath = ref('')
const impId = ref('')
const impName = ref('')
const impScale = ref(2)
const impColor = ref<'rgb' | 'bgr'>('rgb')
const impRange = ref<'0-1' | '0-255'>('0-1')
const impTile = ref<number | null>(0)

async function pickOnnx() {
  const p = await window.sv.pickModel()
  if (p) {
    impPath.value = p
    if (!impId.value) {
      impId.value = p.split(/[\\/]/).pop()!.replace(/\.onnx$/i, '').toLowerCase()
        .replace(/[^a-z0-9-]+/g, '-').slice(0, 40)
    }
    if (!impName.value) impName.value = p.split(/[\\/]/).pop()!.replace(/\.onnx$/i, '')
  }
}

async function doImport() {
  if (!impPath.value || !impId.value || !impName.value) return
  importing.value = true
  const r = await api.importModel({
    path: impPath.value, id: impId.value, name: impName.value,
    scale: impScale.value, color: impColor.value, value_range: impRange.value,
    tile: impTile.value ?? 0,
  })
  importing.value = false
  if (r.ok) {
    message.success(`已导入「${impName.value}」并通过验证`)
    showImport.value = false
    impPath.value = impId.value = impName.value = ''
    refreshModels()
  } else {
    const e = await r.json().catch(() => ({ detail: r.status }))
    message.error(`导入失败: ${e.detail ?? r.status}`)
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
        <NButton size="small" @click="showImport = true">导入自定义模型</NButton>
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

    <NModal
      v-model:show="showImport"
      preset="card"
      title="导入自定义 ONNX 模型"
      style="width: 560px"
    >
      <NForm label-placement="left" label-width="88">
        <NFormItem label="模型文件">
          <div class="imp-file">
            <span class="imp-path">{{ impPath || '未选择' }}</span>
            <NButton size="small" @click="pickOnnx">选择 .onnx…</NButton>
          </div>
        </NFormItem>
        <NFormItem label="模型 ID">
          <NInput v-model:value="impId" placeholder="小写字母/数字/连字符" />
        </NFormItem>
        <NFormItem label="显示名称">
          <NInput v-model:value="impName" />
        </NFormItem>
        <NFormItem label="放大倍率">
          <NInputNumber v-model:value="impScale" :min="2" :max="8" style="width: 120px" />
        </NFormItem>
        <NFormItem label="颜色序">
          <NRadioGroup v-model:value="impColor">
            <NRadioButton value="rgb">RGB</NRadioButton>
            <NRadioButton value="bgr">BGR</NRadioButton>
          </NRadioGroup>
        </NFormItem>
        <NFormItem label="值域">
          <NRadioGroup v-model:value="impRange">
            <NRadioButton value="0-1">0-1 归一化</NRadioButton>
            <NRadioButton value="0-255">0-255</NRadioButton>
          </NRadioGroup>
        </NFormItem>
        <NFormItem label="分块 (px)">
          <NInputNumber v-model:value="impTile" :min="0" :max="1024" :step="64" style="width: 120px" />
          <span class="imp-hint">0 = 不分块；固定输入尺寸的模型填其尺寸（如 64）</span>
        </NFormItem>
      </NForm>
      <p class="imp-note">导入时会真跑一帧验证（倍率/IO 约定），验证不过会给出原因并回滚。</p>
      <template #footer>
        <NSpace justify="end">
          <NButton @click="showImport = false">取消</NButton>
          <NButton type="primary" :loading="importing" :disabled="!impPath || !impId || !impName" @click="doImport">
            导入并验证
          </NButton>
        </NSpace>
      </template>
    </NModal>
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
.imp-file { display: flex; align-items: center; gap: 10px; min-width: 0; flex: 1; }
.imp-path { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #9aa0a6; font-size: 12.5px; }
.imp-hint { margin-left: 10px; font-size: 11.5px; color: #9aa0a6; }
.imp-note { font-size: 12px; color: #9aa0a6; margin: 8px 0 0; }
</style>
