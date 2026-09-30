<script setup lang="ts">
/** 智能分析类节点（hazard_identify / risk_assess / situation_simulate / warning_generate / warning_publish / degrade_to_rule）。 */
import type { NodeProps } from '@vue-flow/core'
import { computed } from 'vue'

import type { FlowNodeData } from '@/utils/graph'

import NodeChips from './NodeChips.vue'
import NodeShell from './NodeShell.vue'
import { configPreview } from './registry'
import { useNodeCard } from './useNodeCard'

const props = defineProps<NodeProps<FlowNodeData>>()
const card = useNodeCard(props)

/**
 * 除 degrade_to_rule 外都要经注入的智能体服务（nodes.py 中 require_service 的调用点），
 * 卡片上区分出来，便于判断该节点是否可能触发失败降级。
 */
const dependsOnAgent = computed(() => props.data.def.type !== 'degrade_to_rule')
</script>

<template>
  <NodeShell v-bind="card">
    <div class="wf-intel__tags">
      <span class="wf-intel__tag">{{ dependsOnAgent ? '依赖智能服务' : '纯规则兜底' }}</span>
    </div>
    <NodeChips :chips="configPreview(props.data.def)" />
  </NodeShell>
</template>

<style scoped>
.wf-intel__tags {
  margin-top: 4px;
}
.wf-intel__tag {
  display: inline-block;
  padding: 0 6px;
  border: 1px solid #d3adf7;
  border-radius: 3px;
  background: #f9f0ff;
  color: #531dab;
  font-size: 11px;
}
</style>
