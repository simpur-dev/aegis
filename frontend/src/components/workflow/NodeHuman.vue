<script setup lang="ts">
/** 人工介入类节点（human_review）：卡片直接露出提示语与候选决策值。 */
import type { NodeProps } from '@vue-flow/core'
import { computed } from 'vue'

import type { FlowNodeData, JsonValue } from '@/utils/graph'

import NodeShell from './NodeShell.vue'
import { useNodeCard } from './useNodeCard'

const props = defineProps<NodeProps<FlowNodeData>>()
const card = useNodeCard(props)

/** 兜底文案与 nodes.py:247 的 handler 默认值保持一致，否则画布与运行期提示会分叉。 */
const prompt = computed<string>(() => {
  const value: JsonValue | undefined = props.data.def.config.prompt
  return typeof value === 'string' && value !== '' ? value : '请值班指挥员确认'
})

const options = computed<string[]>(() => {
  const value: JsonValue | undefined = props.data.def.config.options
  const listed = Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
  return listed.length > 0 ? listed : ['approve', 'reject']
})
</script>

<template>
  <NodeShell v-bind="card">
    <p class="wf-human__prompt">{{ prompt }}</p>
    <p class="wf-human__options">候选决策：{{ options.join(' / ') }}</p>
  </NodeShell>
</template>

<style scoped>
.wf-human__prompt {
  margin: 4px 0 0;
  color: #434343;
}
.wf-human__options {
  margin: 2px 0 0;
  color: #531dab;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
</style>
