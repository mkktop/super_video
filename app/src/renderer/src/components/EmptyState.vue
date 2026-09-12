<script setup lang="ts">
/**
 * 统一空状态：线条风内联 SVG 插画（描边跟随 --sv-text-faint，主色一笔点缀）。
 * 纯装饰插画对读屏隐藏；标题/描述保留语义。禁止图片资源。
 */
withDefaults(
  defineProps<{
    variant?: 'film' | 'cube' | 'log' | 'compare'
    title: string
    desc?: string
  }>(),
  { variant: 'film', desc: '' },
)
</script>

<template>
  <div class="empty-state">
    <!-- 胶片框：任务空态 -->
    <svg v-if="variant === 'film'" class="art" width="104" height="72" viewBox="0 0 104 72" aria-hidden="true">
      <rect x="10" y="8" width="84" height="56" rx="7" fill="none" stroke="currentColor" stroke-width="1.6" stroke-dasharray="6 5" />
      <path d="M20 8v5M32 8v5M44 8v5M56 8v5M68 8v5M80 8v5M20 59v5M32 59v5M44 59v5M56 59v5M68 59v5M80 59v5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" opacity="0.55" />
      <path d="M46 27l16 9-16 9z" class="accent-fill" />
    </svg>
    <!-- 立方体：模型市场空态 -->
    <svg v-else-if="variant === 'cube'" class="art" width="88" height="92" viewBox="0 0 88 92" aria-hidden="true">
      <path d="M44 6l34 19v38L44 82 10 63V25L44 6z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" />
      <path d="M10 25l34 19 34-19M44 44v38" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" opacity="0.55" />
      <circle class="accent-fill" cx="44" cy="25" r="6" />
    </svg>
    <!-- 终端行：日志空态 -->
    <svg v-else-if="variant === 'log'" class="art" width="92" height="72" viewBox="0 0 92 72" aria-hidden="true">
      <rect x="8" y="10" width="76" height="52" rx="7" fill="none" stroke="currentColor" stroke-width="1.6" />
      <path d="M8 24h76" stroke="currentColor" stroke-width="1.6" opacity="0.55" />
      <circle cx="16" cy="17" r="1.7" class="accent-fill" />
      <path d="M18 35l5 4-5 4M30 43h26M30 33h14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" opacity="0.75" />
    </svg>
    <!-- 分割线对比：对比页未选任务 -->
    <svg v-else class="art" width="104" height="70" viewBox="0 0 104 70" aria-hidden="true">
      <rect x="8" y="10" width="88" height="50" rx="7" fill="none" stroke="currentColor" stroke-width="1.6" />
      <path d="M52 10v50" class="accent-stroke" stroke-width="2" />
      <circle cx="52" cy="35" r="7" class="accent-fill" />
      <path d="M49.5 35h5M52 32.5v5" stroke="currentColor" stroke-width="0" />
      <path d="M20 24h18M20 32h12M64 40h18M64 48h12" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" opacity="0.45" />
    </svg>
    <div class="es-title">{{ title }}</div>
    <div v-if="desc" class="es-desc">{{ desc }}</div>
    <div v-if="$slots.default" class="es-action"><slot /></div>
  </div>
</template>

<style scoped>
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  padding: 42px 20px 36px;
  color: var(--sv-text-faint);
}
.art { color: var(--sv-text-faint); margin-bottom: 8px; }
.accent-fill { fill: var(--sv-accent); }
.accent-stroke { stroke: var(--sv-accent); }
.es-title { font-size: 14.5px; font-weight: 600; color: var(--sv-text-dim); }
.es-desc { font-size: 12.5px; color: var(--sv-text-faint); max-width: 380px; text-align: center; line-height: 1.6; }
.es-action { margin-top: 14px; display: flex; gap: 10px; }
</style>
