/**
 * 类别 → 自定义节点组件的映射（4 个组件覆盖 16 类节点，见 @/utils/graph 的 NODE_SPECS）。
 * 单列一处是为了让 NodePalette / Inspector 等纯数据模块不被 SFC 依赖牵连。
 */
import type { NodeCategory } from '@/utils/graph'

import NodeHuman from './NodeHuman.vue'
import NodeIntelligence from './NodeIntelligence.vue'
import NodeIo from './NodeIo.vue'
import NodeLogic from './NodeLogic.vue'

export const WORKFLOW_NODE_TYPES: Record<NodeCategory, typeof NodeIo> = {
  io: NodeIo,
  logic: NodeLogic,
  intelligence: NodeIntelligence,
  human: NodeHuman,
}
