<script setup lang="ts">
/** 数据与执行类节点（data_fetch / api_call / device_control / notify）。 */
import type { NodeProps } from '@vue-flow/core'

import type { FlowNodeData } from '@/utils/graph'

import NodeChips from './NodeChips.vue'
import NodeShell from './NodeShell.vue'
import { configPreview } from './registry'
import { useNodeCard } from './useNodeCard'

const props = defineProps<NodeProps<FlowNodeData>>()
const card = useNodeCard(props)
</script>

<template>
  <NodeShell v-bind="card">
    <NodeChips :chips="configPreview(props.data.def)" />
  </NodeShell>
</template>
