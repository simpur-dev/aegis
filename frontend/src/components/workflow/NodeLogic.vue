<script setup lang="ts">
/** 逻辑编排类节点（threshold / branch / join / delay / feedback_collect）。 */
import type { NodeProps } from '@vue-flow/core'
import { computed } from 'vue'

import type { FlowNodeData, NodeDef } from '@/utils/graph'

import NodeChips from './NodeChips.vue'
import NodeShell from './NodeShell.vue'
import { configPreview } from './registry'
import { useNodeCard } from './useNodeCard'

const props = defineProps<NodeProps<FlowNodeData>>()
const card = useNodeCard(props)

/** 分支/判定类节点最关键的是"几条规则、往哪走"，比标量配置更值得占住卡片行。 */
const summary = computed<string>(() => {
  const def: NodeDef = props.data.def
  const count = (key: string): number => {
    const value = def.config[key]
    return Array.isArray(value) ? value.length : 0
  }
  switch (def.type) {
    case 'join':
      return `汇聚 ${count('upstream')} 路上游`
    case 'branch':
      return `${count('rules')} 条规则，兜底 ${String(def.config.default ?? 'else')}`
    case 'threshold':
      return `${count('conditions')} 条阈值，模式 ${String(def.config.mode ?? 'any')}`
    default:
      return ''
  }
})
</script>

<template>
  <NodeShell v-bind="card">
    <p v-if="summary !== ''" class="wf-logic__summary">{{ summary }}</p>
    <NodeChips v-else :chips="configPreview(props.data.def)" />
  </NodeShell>
</template>

<style scoped>
.wf-logic__summary {
  margin: 4px 0 0;
  color: #434343;
}
</style>
