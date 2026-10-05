<script setup lang="ts">
/** 画布卡片上的配置摘要条：仅展示标量或计数，避免把节点撑成表单。 */
import type { ConfigChip } from './registry'

defineProps<{ chips: ConfigChip[] }>()
</script>

<template>
  <ul v-if="chips.length > 0" class="wf-chips">
    <li v-for="chip in chips" :key="chip.key" class="wf-chips__item" :title="`${chip.label}：${chip.value}`">
      <span class="wf-chips__label">{{ chip.label }}</span>
      <span class="wf-chips__value">{{ chip.value }}</span>
    </li>
  </ul>
</template>

<style scoped>
.wf-chips {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin: 4px 0 0;
  padding: 0;
  list-style: none;
}
.wf-chips__item {
  display: flex;
  gap: 4px;
  max-width: 200px;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.wf-chips__label {
  color: #5a6072;
}
.wf-chips__value {
  color: #434343;
}
</style>
