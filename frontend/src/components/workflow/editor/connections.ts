// Ported from Dify web/app/components/workflow/utils/workflow.ts
// (tag 1.17.1, commit 8387590ace4a094de812b7847fc6a4c3a27cd52b).
// Copyright (c) LangGenius, Inc. Licensed under the modified Apache License 2.0.
// Changes: adapted to AEGIS; see THIRD_PARTY_NOTICES.md.

/** Vue Flow 的 handle 连接补齐：节点上哪些 source/target handle 已被边占用。 */
export interface ConnectionEdge {
  source: string
  target: string
  sourceHandle?: string | null
  targetHandle?: string | null
}

export interface ConnectionChange {
  type: 'add' | 'remove'
  edge: ConnectionEdge
}

export interface ConnectedHandleNode {
  id: string
  data?: {
    _connectedSourceHandleIds?: readonly string[]
    _connectedTargetHandleIds?: readonly string[]
  } | null
}

export interface ConnectedHandleIds {
  _connectedSourceHandleIds: string[]
  _connectedTargetHandleIds: string[]
}

/**
 * 上游用 `splice(findIndex(...), 1)` 删除 handle：找不到时 `findIndex=-1` 会误删末尾一项。
 * AEGIS 在删除不存在的 handle 时保持集合不变（fixture 的 remove-missing-handle 钉住）。
 */
export function getNodesConnectedSourceOrTargetHandleIdsMap(
  changes: readonly ConnectionChange[],
  nodes: readonly ConnectedHandleNode[],
): Record<string, ConnectedHandleIds> {
  const map: Record<string, ConnectedHandleIds> = {}

  const ensure = (nodeId: string): ConnectedHandleIds | null => {
    const existing = map[nodeId]
    if (existing !== undefined) return existing
    const node = nodes.find((item) => item.id === nodeId)
    if (node === undefined) return null
    const created: ConnectedHandleIds = {
      _connectedSourceHandleIds: [...(node.data?._connectedSourceHandleIds ?? [])],
      _connectedTargetHandleIds: [...(node.data?._connectedTargetHandleIds ?? [])],
    }
    map[nodeId] = created
    return created
  }

  const removeOnce = (handles: string[], handle: string | null | undefined): void => {
    const index = handles.findIndex((item) => item === handle)
    if (index >= 0) handles.splice(index, 1)
  }

  for (const change of changes) {
    const { edge, type } = change
    const source = ensure(edge.source)
    const target = ensure(edge.target)

    if (source !== null) {
      if (type === 'remove') removeOnce(source._connectedSourceHandleIds, edge.sourceHandle)
      else source._connectedSourceHandleIds.push(edge.sourceHandle ?? 'source')
    }

    if (target !== null) {
      if (type === 'remove') removeOnce(target._connectedTargetHandleIds, edge.targetHandle)
      else target._connectedTargetHandleIds.push(edge.targetHandle ?? 'target')
    }
  }

  return map
}
