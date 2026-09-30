import type { NodeProps } from '@vue-flow/core'
import { computed } from 'vue'

import type { FlowNodeData } from '@/utils/graph'

/**
 * NodeProps → NodeShell 入参的唯一换算点。
 * 返回 computed 对象是为了配合模板 `v-bind="card"`：Vue 会整体解包一次，
 * 若返回多个散落的 computed，展开绑定时容易漏字段。
 */
export function useNodeCard(props: NodeProps<FlowNodeData>) {
  return computed(() => ({
    label: props.data.def.name === '' ? props.data.def.node_id : props.data.def.name,
    nodeType: props.data.def.type,
    state: props.data.state,
    slaMs: props.data.def.sla_ms,
    timeoutMs: props.data.def.timeout_ms,
    attempts: props.data.attempts,
    selected: props.selected,
  }))
}
