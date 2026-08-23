<script setup lang="ts">
defineProps<{ title: string; desc: string }>()
</script>

<template>
  <div class="placeholder">
    <div class="ph-icon">◈</div>
    <h2>{{ title }}</h2>
    <p>{{ desc }}</p>
    <span class="tag">M2 版本上线</span>
  </div>
</template>

<style scoped>
.placeholder {
  margin-top: 18vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  color: #9aa0a6;
}
.ph-icon {
  font-size: 40px;
  background: linear-gradient(135deg, #4f8cff, #8b5cf6);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
}
h2 { color: #e8eaed; font-size: 19px; }
p { font-size: 13.5px; }
.tag {
  margin-top: 6px;
  font-size: 11px;
  border: 1px solid #3a3f45;
  color: #6b7280;
  border-radius: 5px;
  padding: 2px 8px;
}
</style>
