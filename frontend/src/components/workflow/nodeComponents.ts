/**
 * 类别 → 自定义节点组件的映射（4 个组件按类别覆盖 NODE_SPECS 的全部类型，含内部原语），
 * 外加一个只存在于画布 layout 的 note 组件。
 * 单列一处是为了让 NodePalette / Inspector 等纯数据模块不被 SFC 依赖牵连。
 */
import { markRaw } from 'vue'

import NodeHuman from './NodeHuman.vue'
import NodeIntelligence from './NodeIntelligence.vue'
import NodeIo from './NodeIo.vue'
import NodeLogic from './NodeLogic.vue'
import WorkflowEdge from './WorkflowEdge.vue'
import NoteNode from './editor/NoteNode.vue'

/**
 * markRaw 是必须的：Vue Flow 把 node-types 收进自己的 reactive 状态，
 * 组件定义被 Proxy 包起来后每次渲染都会告 "received a Component that was made a reactive object"，
 * 并对组件对象做深度代理（真机控制台里 16 个节点刷了满屏 warning）。
 */
export const WORKFLOW_NODE_TYPES = {
  io: markRaw(NodeIo),
  logic: markRaw(NodeLogic),
  intelligence: markRaw(NodeIntelligence),
  human: markRaw(NodeHuman),
  note: markRaw(NoteNode),
}

/** 边类型与 `defToGraph` 写进 `FlowEdge.type` 的值一致；markRaw 的理由同节点。 */
export const WORKFLOW_EDGE_TYPES = { workflow: markRaw(WorkflowEdge) }
