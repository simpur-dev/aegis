// Ported from Dify web/app/components/workflow/hooks/use-nodes-available-var-list.ts
// (tag 1.17.1, commit 8387590ace4a094de812b7847fc6a4c3a27cd52b).
// Copyright (c) LangGenius, Inc. Licensed under the modified Apache License 2.0.
// Changes: only ancestors + catalog output keys + last-run outputs; env/conversation/snippet/tool
// sources are not ported. See THIRD_PARTY_NOTICES.md.
import { computed, type ComputedRef } from 'vue'

import type { NodeTypeEntry } from '@/api/workflow/contract'
import { layoutLayers, NODE_SPECS, type EdgeDef, type JsonValue, type NodeDef } from '@/utils/graph'
import { isSafeVariableKey } from '@/utils/graph/variable'

/** 变量面板里的一组：一个祖先节点 + 这一组能引用的输出键。 */
export interface UpstreamVariableGroup {
  nodeId: string
  nodeType: string
  keys: string[]
}

export interface UpstreamVariableInput {
  nodes: readonly Pick<NodeDef, 'node_id' | 'type'>[]
  edges: readonly EdgeDef[]
  targetNodeId: string
  /** 节点类型 → 该类型的输出键（顺序即面板顺序）。 */
  outputsByType: Readonly<Record<string, readonly string[]>>
  /** 节点 ID → 上次运行真实产出的键（只取键，不看值）。 */
  lastRunOutputsByNode?: Readonly<Record<string, Readonly<Record<string, unknown>>>>
}

/** 反向可达：沿入边一直走到底，目标节点自身永远不算自己的上游。 */
function ancestorIds(targetNodeId: string, edges: readonly EdgeDef[], known: ReadonlySet<string>): Set<string> {
  const parents = new Map<string, string[]>()
  for (const edge of edges) {
    if (!known.has(edge.source) || !known.has(edge.target)) continue
    const rows = parents.get(edge.target)
    if (rows === undefined) parents.set(edge.target, [edge.source])
    else if (!rows.includes(edge.source)) rows.push(edge.source)
  }

  const seen = new Set<string>()
  const queue = [...(parents.get(targetNodeId) ?? [])]
  while (queue.length > 0) {
    const nodeId = queue.shift() as string
    if (nodeId === targetNodeId || seen.has(nodeId)) continue
    seen.add(nodeId)
    queue.push(...(parents.get(nodeId) ?? []))
  }
  return seen
}

/**
 * 分组顺序 = 拓扑层升序、同层 node_id 升序。
 * 环上的图 `layoutLayers` 会抛错：这里退回"全部祖先按 ID 排"，变量面板不该因一张待修图整块消失。
 */
function orderedLayers(nodeIds: readonly string[], edges: readonly EdgeDef[]): string[][] {
  try {
    return layoutLayers(nodeIds, edges)
  } catch {
    return [[...nodeIds].sort()]
  }
}

/**
 * 目标节点的真实祖先（有向路径可达），每组的键 = catalog 声明顺序 ∪ 上次运行多出来的键。
 * 没有任何键的组整组省略——空组在面板里只是一行不能点的占位。
 */
export function upstreamVariableGroups(input: UpstreamVariableInput): UpstreamVariableGroup[] {
  const nodeById = new Map(input.nodes.map((node) => [node.node_id, node]))
  if (!nodeById.has(input.targetNodeId)) return []
  const ancestors = ancestorIds(input.targetNodeId, input.edges, new Set(nodeById.keys()))

  const groups: UpstreamVariableGroup[] = []
  for (const layer of orderedLayers([...nodeById.keys()], input.edges)) {
    for (const nodeId of layer) {
      if (!ancestors.has(nodeId)) continue
      const node = nodeById.get(nodeId)
      if (node === undefined) continue
      const keys = (input.outputsByType[node.type] ?? []).filter(isSafeVariableKey)
      for (const key of Object.keys(input.lastRunOutputsByNode?.[nodeId] ?? {})) {
        if (isSafeVariableKey(key) && !keys.includes(key)) keys.push(key)
      }
      if (keys.length > 0) groups.push({ nodeId, nodeType: node.type, keys })
    }
  }
  return groups
}

export interface UseUpstreamVariablesOptions {
  /** 当前文档；加载中返回 null。只读节点身份与连线，不碰节点配置。 */
  definition: () => { nodes: readonly Pick<NodeDef, 'node_id' | 'type'>[]; edges: readonly EdgeDef[] } | null
  /** 正在编辑的节点；未选中时返回 null。 */
  targetNodeId: () => string | null
  /** 服务端节点目录；为空时退回本地镜像的输出键。 */
  catalog: () => readonly NodeTypeEntry[]
  /** 当前实例各节点的产出；没选实例时返回 null。 */
  lastRunOutputs?: () => Readonly<Record<string, Record<string, JsonValue>>> | null
}

function outputsByTypeOf(catalog: readonly NodeTypeEntry[]): Record<string, readonly string[]> {
  const merged: Record<string, readonly string[]> = {}
  for (const [type, spec] of Object.entries(NODE_SPECS)) merged[type] = spec.outputs
  for (const entry of catalog) merged[entry.type] = entry.outputs
  return merged
}

/** 选中节点变化、目录刷新、实例轮询都会重算；只依赖调用方给的三份快照。 */
export function useUpstreamVariables(options: UseUpstreamVariablesOptions): ComputedRef<UpstreamVariableGroup[]> {
  return computed(() => {
    const definition = options.definition()
    const targetNodeId = options.targetNodeId()
    if (definition === null || targetNodeId === null) return []
    return upstreamVariableGroups({
      nodes: definition.nodes,
      edges: definition.edges,
      targetNodeId,
      outputsByType: outputsByTypeOf(options.catalog()),
      lastRunOutputsByNode: options.lastRunOutputs?.() ?? {},
    })
  })
}
