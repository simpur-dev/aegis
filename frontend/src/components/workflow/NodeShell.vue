<script setup lang="ts">
/**
 * 全部画布节点共用的外框：类型标签、状态徽标、SLA/超时读数与左右 Handle。
 * 类别差异通过默认插槽注入，避免 16 类节点各写一份近似重复的模板。
 */
import { Handle, Position } from '@vue-flow/core'
import { computed } from 'vue'

import { stateStyle, type NodeState } from '@/utils/graph'

const props = defineProps<{
  label: string
  nodeType: string
  state: NodeState | null
  slaMs: number
  timeoutMs: number
  attempts: number
  selected: boolean
}>()

const style = computed(() => stateStyle(props.state ?? 'pending'))
const stateClass = computed(() => (props.state === null ? 'is-idle' : style.value.className))
const badge = computed(() => (props.state === null ? '未运行' : style.value.label))
</script>

<template>
  <div
    class="wf-node"
    :class="[stateClass, { 'is-selected': selected }]"
    :style="{ '--wf-accent': props.state === null ? '#8c8c8c' : style.color }"
  >
    <Handle type="target" :position="Position.Left" />
    <div class="wf-node__head">
      <span class="wf-node__title" :title="label">{{ label }}</span>
      <span class="wf-node__badge">{{ badge }}</span>
    </div>
    <div class="wf-node__type">{{ nodeType }}</div>
    <slot />
    <div class="wf-node__meta">
      <span>SLA {{ slaMs }} ms</span>
      <span>超时 {{ timeoutMs }} ms</span>
      <span v-if="props.state !== null">已试 {{ attempts }} 次</span>
    </div>
    <Handle type="source" :position="Position.Right" />
  </div>
</template>

<style scoped>
.wf-node {
  /* 宽度钉死：卡片原先只有 min-width，标题一长就把盒子往右撑，
     真机在一份 11 节点的定义上量到相邻两颗压叠 20×37px。长标题改省略号，
     全文看 title 提示与右侧检查器。 */
  width: 208px;
  padding: 8px 10px;
  border: 1px solid #d9d9d9;
  border-left: 3px solid var(--wf-accent, #8c8c8c);
  border-radius: 6px;
  background: #fff;
  color: #262626;
  font-size: 12px;
  line-height: 1.5;
  box-shadow: 0 1px 3px rgb(0 0 0 / 8%);
}
.wf-node.is-selected {
  border-color: #1677ff;
  box-shadow: 0 0 0 2px rgb(22 119 255 / 25%);
}
.wf-node.is-running {
  border-color: #13c2c2;
}
.wf-node.is-failed,
.wf-node.is-timeout {
  background: #fff1f0;
}
.wf-node.is-awaiting-human {
  background: #f9f0ff;
}
.wf-node.is-idle {
  color: #595959;
}
.wf-node__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.wf-node__title {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 600;
  font-size: 13px;
}
.wf-node__badge {
  padding: 0 6px;
  border-radius: 10px;
  background: var(--wf-accent, #8c8c8c);
  color: #fff;
  font-size: 11px;
  white-space: nowrap;
}
.wf-node__type {
  color: #5a6072;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.wf-node__meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 6px;
  padding-top: 4px;
  border-top: 1px dashed #f0f0f0;
  color: #5a6072;
}
</style>
