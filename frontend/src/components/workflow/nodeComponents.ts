/**
 * 类别 → 自定义节点组件的映射（4 个组件按类别覆盖 NODE_SPECS 的全部类型，含内部原语）。
 * 单列一处是为了让 NodePalette / Inspector 等纯数据模块不被 SFC 依赖牵连。
 */
import { markRaw } from 'vue'

import type { NodeCategory } from '@/utils/graph'

import NodeHuman from './NodeHuman.vue'
import NodeIntelligence from './NodeIntelligence.vue'
import NodeIo from './NodeIo.vue'
import NodeLogic from './NodeLogic.vue'

/**
 * markRaw 是必须的：Vue Flow 把 node-types 收进自己的 reactive 状态，
 * 组件定义被 Proxy 包起来后每次渲染都会告 "received a Component that was made a reactive object"，
 * 并对组件对象做深度代理（真机控制台里 16 个节点刷了满屏 warning）。
 */
export const WORKFLOW_NODE_TYPES: Record<NodeCategory, typeof NodeIo> = {
  io: markRaw(NodeIo),
  logic: markRaw(NodeLogic),
  intelligence: markRaw(NodeIntelligence),
  human: markRaw(NodeHuman),
}
