<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { NButton, NSwitch, NTag, useMessage } from 'naive-ui'
import { api } from '../api'

const message = useMessage()
const mcpEnabled = ref(true)
const mcpCommand = ref<{ command: string; args: string[] } | null>(null)
const mcpBase = ref('')
const appVer = ref('')

onMounted(async () => {
  appVer.value = await window.sv.appVersion().catch(() => '')
  const s = (await api.settings().catch(() => ({}))) as { mcp_enabled?: boolean }
  mcpEnabled.value = s.mcp_enabled !== false
  void window.sv.backendInfo().then((info) => {
    mcpCommand.value = info.mcpCommand ?? null
    mcpBase.value = info.baseUrl
  }).catch(() => { /* 片段区隐藏即可，不报错 */ })
})

/** 三段客户端配置片段：路径由主进程按安装形态预填（backend:info 的 mcpCommand） */
const mcpSnippets = computed(() => {
  if (!mcpCommand.value) return []
  const { command, args } = mcpCommand.value
  const snippet = (o: unknown) => JSON.stringify(o, null, 2)
  return [
    { name: 'Claude Desktop', file: 'claude_desktop_config.json', text: snippet({ mcpServers: { rainframe: { command, args } } }) },
    { name: 'Cursor', file: '~/.cursor/mcp.json', text: snippet({ mcpServers: { rainframe: { command, args } } }) },
    { name: 'ZCode', file: '~/.zcode/cli/config.json', text: snippet({ mcp: { servers: { rainframe: { command, args } } } }) },
  ]
})

/** 通用接入指令：没有专属片段的客户端，把这段发给 AI——它知道自己宿主的
 *  MCP 配置格式与文件位置，自己写入、自己重启验证（跨客户端没有统一深链
 *  标准的现状下，这是唯一真正通用的 onboarding 路径）。 */
const universalInstruction = computed(() => {
  if (!mcpCommand.value) return ''
  const { command, args } = mcpCommand.value
  return [
    '请帮我接入本机的 MCP 服务「雨帧 RainFrame」（视频/图片/漫画超分工具），步骤：',
    `1. 在你当前环境的 MCP 配置里新增一个 stdio 服务：name 为 rainframe，command 为 "${command}"，args 为 ${JSON.stringify(args)}；`,
    '2. 重启会话/客户端使配置生效；',
    '3. 调用工具 rf_status 验证：返回 version 即接入成功。若提示——',
    '   「未发现运行中的雨帧」→ 先打开雨帧应用再重试；',
    '   「MCP 接入已被关闭」→ 在雨帧「MCP 服务」页打开「允许 AI 客户端接入」。',
    '已知客户端的配置位置：Claude Desktop→claude_desktop_config.json 的 mcpServers；Cursor→~/.cursor/mcp.json；ZCode→~/.zcode/cli/config.json 的 mcp.servers；其他环境按你自己的 MCP 配置格式写入。',
  ].join('\n')
})

async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text)
    message.success('已复制，粘贴进客户端配置文件后重启该客户端')
  } catch {
    message.error('复制失败，请手动选择文本复制')
  }
}

async function saveMcpEnabled(v: boolean) {
  const r = await api.saveSettings({ mcp_enabled: v })
  if (!r.ok) {
    message.error(`保存失败: ${(await r.json()).detail ?? r.status}`)
    mcpEnabled.value = !v
  }
}

const canDo = [
  '探测视频 / 图片信息，按内容推荐模型、倍率与预处理',
  '下载缺失模型，创建视频、图片与漫画批量任务',
  '取消任务、失败后断点续跑',
  '轮询进度与速度，完成时汇报产物路径',
]
const cannotDo = [
  '删除模型或任务',
  '修改本应用的任何设置',
  '覆盖已有输出文件（冲突时需经你确认后重交）',
]
</script>

<template>
  <div class="ai-page">
    <div class="page-head">
      <h1>MCP 服务</h1>
      <p class="head-sub">
        通过 MCP 协议把雨帧接入 Claude Desktop、Cursor、ZCode 等 AI 客户端——
        对话即可探测媒体、选模型、下超分任务、盯进度。
      </p>
    </div>

    <!-- 接入状态 -->
    <section class="card sv-card">
      <header class="card-head">
        <div class="card-title">接入状态</div>
        <div class="card-sub">本地服务与准入开关；关闭后已连接的客户端会收到「接入已关闭」提示</div>
      </header>
      <div class="card-body">
        <div class="row switch-row">
          <span class="row-text">
            本地服务
            <NTag size="small" :bordered="false" type="success">已连接</NTag>
            <span class="svc-addr">{{ mcpBase || '…' }}</span>
            <span v-if="appVer" class="svc-ver">v{{ appVer }}</span>
          </span>
        </div>
        <div class="row switch-row bordered-top">
          <span class="row-text">
            允许 AI 客户端接入
            <small>默认开启；走本机令牌鉴权，仅你配置过的客户端可连，应用内功能不受影响</small>
          </span>
          <NSwitch v-model:value="mcpEnabled" size="small" @update:value="saveMcpEnabled" />
        </div>
      </div>
    </section>

    <!-- 客户端配置 -->
    <section class="card sv-card">
      <header class="card-head">
        <div class="card-title">客户端配置</div>
        <div class="card-sub">已知客户端复制专属片段；其他 AI 客户端复制通用指令发给它，由它自己完成配置</div>
      </header>
      <div class="card-body">
        <p class="hint">
          客户端会自动发现本地服务，无需填端口；使用时保持雨帧运行。
          之后直接对 AI 说「把这个视频超成 4K」「这个文件夹的漫画全部处理」即可。
        </p>
        <div v-for="sn in mcpSnippets" :key="sn.name" class="mcp-snippet">
          <div class="mcp-snippet-head">
            <span class="mcp-snippet-name">{{ sn.name }}</span>
            <span class="mcp-snippet-file">{{ sn.file }}</span>
            <NButton size="tiny" quaternary @click="copyText(sn.text)">复制</NButton>
          </div>
          <pre class="mcp-code">{{ sn.text }}</pre>
        </div>
        <div v-if="universalInstruction" class="mcp-snippet">
          <div class="mcp-snippet-head">
            <span class="mcp-snippet-name">任意 AI 客户端（通用）</span>
            <span class="mcp-snippet-file">把这段话发给你的 AI，让它自己完成接入与验证</span>
            <NButton size="tiny" quaternary @click="copyText(universalInstruction)">复制</NButton>
          </div>
          <pre class="mcp-code">{{ universalInstruction }}</pre>
        </div>
      </div>
    </section>

    <!-- 能力与边界 -->
    <section class="card sv-card">
      <header class="card-head">
        <div class="card-title">能力与边界</div>
        <div class="card-sub">AI 客户端能做什么、被明确挡在门外的是什么</div>
      </header>
      <div class="card-body">
        <div class="bounds">
          <div class="bounds-col">
            <div class="bounds-head can">AI 可以</div>
            <ul class="bounds-list">
              <li v-for="t in canDo" :key="t">{{ t }}</li>
            </ul>
          </div>
          <div class="bounds-col">
            <div class="bounds-head cannot">AI 不能</div>
            <ul class="bounds-list">
              <li v-for="t in cannotDo" :key="t">{{ t }}</li>
            </ul>
          </div>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.ai-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  width: 100%;
  min-width: 560px;
  max-width: 760px;
}
h1 { font-size: 22px; font-weight: 600; letter-spacing: 0.3px; }
.head-sub { color: var(--sv-text-dim); font-size: 13px; margin-top: 6px; line-height: 1.6; }

.card { overflow: hidden; }
.card-head {
  padding: 14px 18px 12px;
  border-bottom: 1px solid var(--sv-border-soft);
  background: linear-gradient(180deg, var(--sv-fill-1), transparent);
}
.card-title { font-size: 14px; font-weight: 650; color: var(--sv-text); }
.card-sub { font-size: 12px; color: var(--sv-text-dim); margin-top: 3px; }
.card-body { padding: 4px 18px 14px; }

.row { padding: 12px 0; }
.row.bordered-top { border-top: 1px solid var(--sv-border-soft); margin-top: 4px; }
.switch-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.row-text { font-weight: 600; font-size: 13px; color: var(--sv-text); display: inline-flex; align-items: center; gap: 8px; }
.row-text small {
  display: block;
  font-weight: 400;
  font-size: 12px;
  color: var(--sv-text-dim);
  margin-top: 3px;
  max-width: 540px;
}
.hint { color: var(--sv-text-dim); font-size: 12px; margin: 8px 0 6px; line-height: 1.55; }

.svc-addr {
  font-family: Consolas, 'Courier New', monospace;
  font-size: 12px;
  color: var(--sv-text-code);
}
.svc-ver { font-size: 12px; color: var(--sv-text-faint); }

/* 客户端配置片段：客户端名 + 配置文件路径 + 复制按钮，下挂等宽代码块 */
.mcp-snippet { margin-top: 10px; }
.mcp-snippet-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
}
.mcp-snippet-name { font-weight: 600; font-size: 12.5px; color: var(--sv-text); }
.mcp-snippet-file {
  flex: 1;
  min-width: 0;
  font-family: Consolas, 'Courier New', monospace;
  font-size: 11.5px;
  color: var(--sv-text-faint);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.mcp-code {
  margin: 0;
  padding: 10px 12px;
  background: var(--sv-well);
  border: 1px solid var(--sv-border-soft);
  border-radius: 8px;
  font-family: Consolas, 'Courier New', monospace;
  font-size: 12px;
  line-height: 1.55;
  color: var(--sv-text-code);
  overflow-x: auto;
}

/* 能力与边界：左右两栏列表 */
.bounds {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  padding-top: 10px;
}
.bounds-head {
  font-size: 12.5px;
  font-weight: 650;
  margin-bottom: 8px;
}
.bounds-head.can { color: var(--sv-accent); }
.bounds-head.cannot { color: var(--sv-danger-strong); }
.bounds-list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.bounds-list li {
  position: relative;
  padding-left: 16px;
  font-size: 12.5px;
  color: var(--sv-text-dim);
  line-height: 1.5;
}
.bounds-list li::before {
  content: '';
  position: absolute;
  left: 2px;
  top: 7px;
  width: 5px;
  height: 5px;
  border-radius: 50%;
}
.bounds-col:first-child .bounds-list li::before { background: var(--sv-accent); }
.bounds-col:last-child .bounds-list li::before { background: var(--sv-danger); }

@media (max-width: 700px) {
  .bounds { grid-template-columns: 1fr; }
}
</style>
