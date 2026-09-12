<script setup lang="ts">
/**
 * 全局命令面板（Ctrl/Cmd + K）：页面跳转 / 高频动作 / 最近任务，模糊匹配。
 * 数据全部来自现有 store（纯导航与既有桥方法），不新增后端接口。
 * 挂在 App 根部（不在 KeepAlive 内），监听随组件生命周期装拆，无重复绑定问题。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { openCompare, store, ui } from '../store'
import { navDirection, pageDir } from '../uiFx'
import { themeMode } from '../theme'

interface Cmd {
  id: string
  label: string
  sub: string
  group: '页面' | '动作' | '最近任务'
  run: () => void
}

const open = ref(false)
const q = ref('')
const sel = ref(0)
const inputEl = ref<HTMLInputElement | null>(null)
const listEl = ref<HTMLElement | null>(null)

function go(page: typeof ui.page) {
  pageDir.value = navDirection(ui.page, page)
  ui.page = page
}

const baseName = (p: string) => p.split(/[\\/]/).pop() ?? p

const commands = computed<Cmd[]>(() => {
  const pages: Array<[typeof ui.page, string]> = [
    ['home', '首页'],
    ['tasks', '任务队列'],
    ['newtask', '新建任务'],
    ['models', '模型市场'],
    ['mcompare', '模型对比'],
    ['trim', '视频剪切'],
    ['imagesr', '图片超分'],
    ['perf', '性能监控'],
    ['logs', '服务日志'],
    ['settings', '设置'],
  ]
  const out: Cmd[] = pages.map(([key, label]) => ({
    id: `page-${key}`,
    label,
    sub: '跳转页面',
    group: '页面',
    run: () => go(key),
  }))
  out.push(
    {
      id: 'act-newtask',
      label: '新建超分任务',
      sub: '三步向导',
      group: '动作',
      run: () => go('newtask'),
    },
    {
      id: 'act-mcompare',
      label: '打开模型对比',
      sub: '同段素材并排跑模型',
      group: '动作',
      run: () => go('mcompare'),
    },
    {
      id: 'act-outdir',
      label: '打开输出目录',
      sub: '在资源管理器中打开',
      group: '动作',
      run: () => {
        const dir = String(store.settings.output_dir ?? '').trim()
        if (dir) void window.sv.openPath(dir)
        else go('settings') // 未设置输出目录：带到设置页
      },
    },
    {
      id: 'act-theme',
      label: themeMode.value === 'light' ? '切换为深色外观' : '切换为浅色外观',
      sub: '深浅主题一键切换',
      group: '动作',
      run: () => {
        themeMode.value = themeMode.value === 'light' ? 'dark' : 'light'
      },
    },
  )
  for (const t of store.tasks.slice(0, 5)) {
    const st = t.status
    out.push({
      id: `task-${t.id}`,
      label: baseName(t.input_path),
      sub:
        st === 'done'
          ? `已完成 · 对比 ${t.src_w}x${t.src_h} → x${t.params?.target_scale ?? t.params?.scale ?? ''}`
          : st === 'running'
            ? '运行中 · 打开所在文件夹'
            : `${st} · 打开所在文件夹`,
      group: '最近任务',
      run: () => {
        if (st === 'done' && t.preview_src && t.preview_path) openCompare(t.id)
        else window.sv.showInFolder(st === 'done' ? t.output_path : t.input_path)
      },
    })
  }
  return out
})

/** 模糊匹配：完整子串 > 词首字母连拼 > 子序列，全部不中才剔除 */
function score(cmd: Cmd, kw: string): number {
  if (!kw) return 1
  const hay = `${cmd.label} ${cmd.sub} ${cmd.group}`.toLowerCase()
  if (hay.includes(kw)) return 100 - hay.indexOf(kw)
  // 拼音首字母习惯（如 rw=任务）：各词首字符按序出现
  const initials = cmd.label
    .split(/\s+/)
    .map((w) => w[0] ?? '')
    .join('')
    .toLowerCase()
  if (initials.includes(kw)) return 60
  let i = 0
  for (const ch of hay) {
    if (ch === kw[i]) i++
    if (i >= kw.length) return 20
  }
  return -1
}

const filtered = computed(() => {
  const kw = q.value.trim().toLowerCase()
  if (!kw) return commands.value
  return commands.value
    .map((c) => ({ c, s: score(c, kw) }))
    .filter((x) => x.s > 0)
    .sort((a, b) => b.s - a.s)
    .map((x) => x.c)
})

const grouped = computed(() => {
  type Group = { name: string; items: Array<{ cmd: Cmd; i: number }> }
  const g: Group[] = []
  const byName = new Map<string, Group>()
  filtered.value.forEach((cmd, i) => {
    if (!byName.has(cmd.group)) {
      const entry: Group = { name: cmd.group, items: [] }
      byName.set(cmd.group, entry)
      g.push(entry)
    }
    byName.get(cmd.group)!.items.push({ cmd, i })
  })
  return g
})

function run(cmd: Cmd) {
  open.value = false
  cmd.run()
}

function move(d: 1 | -1) {
  const n = filtered.value.length
  if (!n) return
  sel.value = (sel.value + d + n) % n
}

function close() {
  open.value = false
}

function onKeydown(e: KeyboardEvent) {
  const isK = e.key.toLowerCase() === 'k'
  if ((e.ctrlKey || e.metaKey) && isK) {
    e.preventDefault()
    e.stopImmediatePropagation()
    open.value = !open.value
    return
  }
  if (!open.value) return
  // 面板开着时吞掉其余按键，避免穿透到页面级快捷键（模型对比数字键/分割线方向键）
  if (e.ctrlKey || e.metaKey || e.altKey) return // 放行复制/粘贴等编辑组合
  e.preventDefault()
  e.stopImmediatePropagation()
  if (e.key === 'Escape') close()
  else if (e.key === 'ArrowDown') move(1)
  else if (e.key === 'ArrowUp') move(-1)
  else if (e.key === 'Enter') {
    const cmd = filtered.value[sel.value]
    if (cmd) run(cmd)
  }
}

onMounted(() => window.addEventListener('keydown', onKeydown, true))
onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown, true))

watch(open, async (v) => {
  if (v) {
    q.value = ''
    sel.value = 0
    await nextTick()
    inputEl.value?.focus()
  }
})
watch(q, () => (sel.value = 0))
watch(sel, async () => {
  await nextTick()
  listEl.value
    ?.querySelector('[data-sel="1"]')
    ?.scrollIntoView({ block: 'nearest' })
})
</script>

<template>
  <Teleport to="body">
    <Transition name="cmdz">
      <div v-if="open" class="cmd-overlay" @mousedown.self="close">
        <div class="cmd-panel" role="dialog" aria-label="命令面板">
          <div class="cmd-input-row">
            <svg class="cmd-search-ico" width="15" height="15" viewBox="0 0 15 15" aria-hidden="true">
              <circle cx="6.5" cy="6.5" r="4.6" fill="none" stroke="currentColor" stroke-width="1.4" />
              <path d="M10 10l3.2 3.2" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
            </svg>
            <input
              ref="inputEl"
              v-model="q"
              class="cmd-input"
              type="text"
              placeholder="搜索页面、动作或最近任务…"
              spellcheck="false"
            />
            <span class="cmd-esc">ESC</span>
          </div>
          <div ref="listEl" class="cmd-list">
            <template v-for="g in grouped" :key="g.name">
              <div class="cmd-group">{{ g.name }}</div>
              <button
                v-for="it in g.items"
                :key="it.cmd.id"
                class="cmd-item"
                :data-sel="it.i === sel ? '1' : '0'"
                :class="{ sel: it.i === sel }"
                @mouseenter="sel = it.i"
                @click="run(it.cmd)"
              >
                <span class="ci-label">{{ it.cmd.label }}</span>
                <span class="ci-sub">{{ it.cmd.sub }}</span>
                <svg v-if="it.i === sel" class="ci-enter" width="13" height="13" viewBox="0 0 13 13" aria-hidden="true">
                  <path d="M2.5 6.5h6M6 3l3.5 3.5L6 10" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" />
                </svg>
              </button>
            </template>
            <div v-if="!filtered.length" class="cmd-empty">没有匹配「{{ q.trim() }}」的结果</div>
          </div>
          <div class="cmd-foot">
            <span><b>↑↓</b> 选择</span>
            <span><b>Enter</b> 执行</span>
            <span><b>Esc</b> 关闭</span>
            <span class="cmd-foot-spacer" />
            <span class="cmd-foot-k">Ctrl K</span>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.cmd-overlay {
  position: fixed;
  inset: 0;
  z-index: 200;
  background: rgba(0, 0, 0, 0.45);
  display: flex;
  justify-content: center;
  align-items: flex-start;
  padding-top: 14vh;
}
.cmd-panel {
  width: min(560px, 92vw);
  background: var(--sv-panel);
  border: 1px solid var(--sv-border-mid);
  border-radius: var(--sv-radius-sm);
  box-shadow: var(--sv-shadow-pop), var(--sv-card-inset);
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
.cmd-input-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 13px 16px;
  border-bottom: 1px solid var(--sv-border-soft);
}
.cmd-search-ico { color: var(--sv-text-faint); flex-shrink: 0; }
.cmd-input {
  flex: 1;
  border: none;
  outline: none;
  background: transparent;
  color: var(--sv-text);
  font-size: 15px;
  font-family: inherit;
}
.cmd-input::placeholder { color: var(--sv-text-faint); }
.cmd-esc {
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.5px;
  color: var(--sv-text-faint);
  border: 1px solid var(--sv-border-mid);
  border-radius: 4px;
  padding: 1px 6px;
}
.cmd-list {
  max-height: 46vh;
  overflow-y: auto;
  padding: 6px;
}
.cmd-group {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 1px;
  color: var(--sv-text-faint);
  padding: 8px 10px 4px;
}
.cmd-item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 8px 10px;
  border: none;
  border-radius: var(--sv-radius-sm);
  background: transparent;
  color: var(--sv-text);
  font-size: 13.5px;
  font-family: inherit;
  cursor: pointer;
  text-align: left;
}
.cmd-item.sel { background: var(--sv-fill-2); }
.ci-label { flex-shrink: 0; max-width: 55%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ci-sub {
  flex: 1;
  font-size: 12px;
  color: var(--sv-text-faint);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ci-enter { color: var(--sv-accent-strong); flex-shrink: 0; }
.cmd-empty {
  padding: 26px 0;
  text-align: center;
  font-size: 12.5px;
  color: var(--sv-text-faint);
}
.cmd-foot {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 8px 14px;
  border-top: 1px solid var(--sv-border-soft);
  font-size: 11px;
  color: var(--sv-text-faint);
}
.cmd-foot b { font-weight: 600; color: var(--sv-text-dim); }
.cmd-foot-spacer { flex: 1; }
.cmd-foot-k {
  font-size: 10px;
  border: 1px solid var(--sv-border-mid);
  border-radius: 4px;
  padding: 1px 6px;
}
/* 150ms 缩放淡入（scale 0.98 → 1） */
.cmdz-enter-active { transition: opacity 0.15s ease-out; }
.cmdz-leave-active { transition: opacity 0.1s ease-in; }
.cmdz-enter-from, .cmdz-leave-to { opacity: 0; }
.cmdz-enter-from .cmd-panel { transform: scale(0.98); }
.cmdz-enter-active .cmd-panel { transition: transform 0.15s var(--sv-ease); }
</style>
