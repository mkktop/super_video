<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
  NButton,
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
import { api, type ModelInfo } from '../api'
import { refreshModels, store } from '../store'

const message = useMessage()
const tab = ref<'all' | 'installed' | 'anime' | 'comic' | 'general'>('all')
// 场景标签筛选（与上方状态/内容 tab 取交集）：一个模型可属多个场景
const scene = ref<'all' | 'video' | 'manga' | 'image'>('all')
const sceneLabel: Record<string, string> = { video: '视频', manga: '漫画', image: '图片' }
const SCENES = ['video', 'manga', 'image'] as const
const hasScene = (m: ModelInfo, s: string) => (m.scenes ?? ['video', 'image']).includes(s)

const speedLabel = { fast: '⚡ 快速', balanced: '⚖ 均衡', slow: '🐢 高质量慢速' }
// 'real' 与 'general' 是注册表两代写入口径，展示与筛选统一按「真人/通用」
const contentLabel = { anime: '动漫', comic: '漫画', general: '真人/通用', real: '真人/通用' }

/** 家族分节（纯展示层，按 id 前缀派生）：模型上量后一个家族十几张变体卡平铺
 *  难扫，按家族分节、家族顺序手工定推荐位；注册表新增未匹配的家族落「其他」，
 *  不改前端也不丢展示。ids 用于没有公共前缀的家族（社区精选三款） */
const FAMILIES: Array<{ key: string; name: string; note?: string; ids?: string[] }> = [
  { key: 'animejanai-v31', name: 'AnimeJaNai V3.1 HD', note: '新一代 SPAN 架构 · 当前推荐' },
  { key: 'animejanai-v3', name: 'AnimeJaNai V3 HD', note: '上一代架构' },
  { key: 'real-cugan', name: 'Real-CUGAN', note: '动漫高画质' },
  { key: 'artcnn', name: 'ArtCNN', note: '动漫 · 轻量高速（亮度通道超分）' },
  { key: 'ani4k-v2', name: 'Ani4K v2' },
  { key: 'mangajanai', name: 'MangaJaNai', note: '漫画专模 · 黑白页按源高度自适应' },
  { key: 'illustrationjanai', name: 'IllustrationJaNai', note: '彩色漫画页 / 插画' },
  { key: 'realesrgan', name: 'Real-ESRGAN', note: '真人/通用' },
  { key: 'hat', name: 'HAT-L Real GAN', note: '真人图片 4x 画质旗舰' },
  { key: 'swinir', name: 'SwinIR', note: '真人图片 4x · transformer' },
  { key: 'dis', name: 'DIS', note: '真人/通用 · 2x 轻量修复' },
  { key: 'seemore', name: 'SeemoRe', note: '通用 · 轻量多倍率' },
  { key: 'community', name: '社区精选', note: 'OpenModelDB 口碑款 · CC BY-NC-SA', ids: ['ultrasharp-4x', 'animesharp-4x', 'remacri-4x'] },
  { key: 'realesr-animevideov3', name: 'AnimeVideo', note: '动漫 · 随软件内置' },
  { key: 'animejanai-v2', name: 'AnimeJaNai V2', note: '旧代' },
  { key: 'rife', name: 'RIFE 补帧', note: '插帧（帧率翻倍），功能不同于超分' },
]
const inFamily = (id: string, f: { key: string; ids?: string[] }) =>
  f.ids ? f.ids.includes(id) : id === f.key || id.startsWith(`${f.key}-`)

const groups = computed<Array<{ key: string; name: string; note?: string; models: ModelInfo[] }>>(() => {
  const list = store.models
    .filter((m) => {
      if (tab.value === 'all') return true
      if (tab.value === 'installed') return m.installed || m.bundled
      if (tab.value === 'anime') return m.content.includes('anime')
      if (tab.value === 'comic') return m.content.includes('comic')
      return m.content.includes('general') || m.content.includes('real')
    })
    .filter((m) => scene.value === 'all' || hasScene(m, scene.value))
    // 组内已下载在前（稳定排序保持注册表顺序），扫一眼就知道哪些已就绪
    .sort((a, b) => Number(b.installed || b.bundled) - Number(a.installed || a.bundled))
  const out = FAMILIES.map((f) => ({
    key: f.key, name: f.name, note: f.note,
    models: list.filter((m) => inFamily(m.id, f)),
  })).filter((g) => g.models.length > 0)
  const known = new Set(out.flatMap((g) => g.models.map((m) => m.id)))
  const other = list.filter((m) => !known.has(m.id))
  if (other.length) out.push({ key: '__other', name: '其他模型', note: undefined, models: other })
  return out
})

const totalShown = computed(() => groups.value.reduce((n, g) => n + g.models.length, 0))

function dropProgress(id: string) {
  const { [id]: _drop, ...rest } = store.downloadProgress
  store.downloadProgress = rest
}

// 0% 阶段 = 正在连远端（直连 GitHub 常以十秒计），文案区分于真实下载进度
const dlPctText = (p: number) => (p === 0 ? '连接中…' : `${Math.round(p * 100)}%`)

async function onDownload(id: string) {
  // 即时反馈：首个进度事件要等远端连上才来，本地先挂 0% 条让点击立刻可见
  store.downloadProgress = { ...store.downloadProgress, [id]: 0 }
  let r: Response
  try {
    r = await api.downloadModel(id)
  } catch {
    // fetch 网络层 reject（本地服务未连接等）：不回滚的话占位条永久卡「连接中…」
    dropProgress(id)
    message.error('下载启动失败：无法连接本地服务')
    return
  }
  if (r.status === 409) {
    dropProgress(id)
    message.warning('已有下载正在进行，请稍候')
    return
  }
  if (!r.ok) {
    dropProgress(id)
    message.error(`下载启动失败: ${r.status}`)
    return
  }
  const j = (await r.json().catch(() => ({}))) as { already?: boolean }
  if (j.already) dropProgress(id) // 已下载：不会有任何进度事件，撤掉占位条
  refreshModels()
}

// 失败事件从 WS 异步到达：store 记一条，这里弹 toast（进度条由 store 统一摘除）
watch(
  () => store.downloadFailed,
  (f) => {
    if (f) message.error(`模型下载失败：${f.msg}`)
  },
)

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
        <NButton size="small" :type="tab === 'all' ? 'primary' : 'default'" @click="tab = 'all'">全部</NButton>
        <NButton size="small" :type="tab === 'installed' ? 'primary' : 'default'" @click="tab = 'installed'">已下载</NButton>
        <NButton size="small" :type="tab === 'anime' ? 'primary' : 'default'" @click="tab = 'anime'">动漫</NButton>
        <NButton size="small" :type="tab === 'comic' ? 'primary' : 'default'" @click="tab = 'comic'">漫画</NButton>
        <NButton size="small" :type="tab === 'general' ? 'primary' : 'default'" @click="tab = 'general'">真人/通用</NButton>
      </NSpace>
    </div>
    <div class="scene-bar">
      <span class="scene-lbl">场景</span>
      <NButton size="tiny" :type="scene === 'all' ? 'primary' : 'default'" secondary @click="scene = 'all'">全部</NButton>
      <NButton v-for="s in SCENES" :key="s" size="tiny"
               :type="scene === s ? 'primary' : 'default'" secondary @click="scene = s">
        {{ sceneLabel[s] }}
      </NButton>
    </div>

    <NEmpty v-if="!totalShown" description="该分类暂无模型" style="margin-top: 12vh" />

    <section v-for="g in groups" :key="g.key" class="family">
      <div class="fam-head">
        <span class="fam-name">{{ g.name }}</span>
        <span class="fam-count">{{ g.models.length }}</span>
        <span v-if="g.note" class="fam-note">{{ g.note }}</span>
      </div>
      <div class="model-grid">
        <div v-for="m in g.models" :key="m.id" class="card mcard">
          <div class="m-head">
            <span class="name">{{ m.name }}</span>
            <NTag v-if="m.bundled" size="small" type="success" :bordered="false">内置</NTag>
            <NTag v-else-if="m.installed" size="small" type="success" :bordered="false">已安装</NTag>
            <NTag v-if="!m.vram_ok" size="small" type="warning" :bordered="false">显存不足</NTag>
            <span class="m-scenes">
              <NTag v-for="s in SCENES.filter((k) => hasScene(m, k))" :key="s"
                    size="small" type="info" :bordered="false">{{ sceneLabel[s] }}</NTag>
            </span>
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
          <div class="m-foot">
            <template v-if="m.bundled">
              <span class="bundled-note">✓ 随软件分发</span>
            </template>
            <template v-else-if="store.downloadProgress[m.id] !== undefined">
              <div class="dl">
                <NProgress
                  type="line"
                  :percentage="Math.round(store.downloadProgress[m.id] * 100)"
                  :show-indicator="false"
                  :height="8"
                  :processing="store.downloadProgress[m.id] === 0"
                />
                <span class="dl-pct">{{ dlPctText(store.downloadProgress[m.id]) }}</span>
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
      </div>
    </section>

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
.page-head { display: flex; justify-content: space-between; align-items: center; gap: 16px; flex-wrap: wrap; }
h1 { font-size: 20px; font-weight: 700; }
.sub { font-size: 12.5px; color: #9aa0a6; margin-top: 4px; }

/* 场景标签筛选行：与上方状态/内容 tab 取交集 */
.scene-bar { display: flex; align-items: center; gap: 6px; margin: 2px 0 4px; }
.scene-lbl { font-size: 12px; color: #9aa0a6; }

/* 家族分节：组头一行（名称+数量+定位），组内仍是响应式网格 */
.family { display: flex; flex-direction: column; gap: 10px; }
.fam-head {
  display: flex; align-items: baseline; gap: 10px;
  padding: 2px 2px 0;
}
.fam-name { font-size: 14.5px; font-weight: 700; }
.fam-count {
  font-size: 11.5px; color: #7c838c;
  padding: 0 8px; border: 1px solid #2e3237; border-radius: 9px;
  line-height: 17px;
}
.fam-note { font-size: 12px; color: #9aa0a6; }

/* 响应式网格:宽窗多列、窄窗自动落单列 */
.model-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(330px, 1fr));
  gap: 14px;
  align-items: stretch;
}
.card {
  background: #1e2023;
  border: 1px solid #26292e;
  border-radius: 12px;
}
.mcard { padding: 16px 18px 14px; display: flex; flex-direction: column; gap: 9px; }
.m-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.m-scenes { margin-left: auto; display: inline-flex; gap: 4px; }
.name { font-size: 15px; font-weight: 600; }
.desc {
  color: #9aa0a6;
  font-size: 12.5px;
  line-height: 1.55;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  min-height: 39px; /* 描述短/长卡片脚对齐 */
}
.tags { display: flex; flex-wrap: wrap; gap: 6px; }
.warn { color: #fbbf24; font-size: 12px; }
.m-foot {
  margin-top: auto;
  padding-top: 12px;
  border-top: 1px solid #26292e;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  min-height: 42px;
}
.bundled-note { color: #34d399; font-size: 12.5px; }
.dl { flex: 1; display: flex; align-items: center; gap: 8px; min-width: 0; }
.dl-pct { font-size: 12px; color: #4f8cff; min-width: 38px; text-align: right; flex-shrink: 0; }
.imp-file { display: flex; align-items: center; gap: 10px; min-width: 0; flex: 1; }
.imp-path { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #9aa0a6; font-size: 12.5px; }
.imp-hint { margin-left: 10px; font-size: 11.5px; color: #9aa0a6; }
.imp-note { font-size: 12px; color: #9aa0a6; margin: 8px 0 0; }
</style>
