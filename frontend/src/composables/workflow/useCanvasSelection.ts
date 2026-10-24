// Ported from Dify web/app/components/workflow/hooks/use-selection-interactions.ts
// (tag 1.17.1, commit 8387590ace4a094de812b7847fc6a4c3a27cd52b).
// Copyright (c) LangGenius, Inc. Licensed under the modified Apache License 2.0.
// Changes: adapted to AEGIS; see THIRD_PARTY_NOTICES.md.
/**
 * 画布选择态（与 Dify `use-selection-interactions.ts` 的语义对齐，见
 * `__fixtures__/dify-1.17.1/selection.contract.json`）。
 *
 * 上游把选择写成节点/边 data 上的 `_isBundled` 标记；AEGIS 的选择只活在编辑层，
 * 不写进文档——否则"点一下节点"就会把草稿标脏。
 */
import { computed, ref } from 'vue'

import type { Position } from '@/utils/graph'

/** 框选按契约里的 240×100 包围盒判定（卡片实际高 132，框选取宽松下界）。 */
export const NODE_SELECTION_FOOTPRINT = { width: 240, height: 100 }

export interface SelectableNode {
  id: string
  position: Position
}

export interface MarqueeRect {
  x: number
  y: number
  width: number
  height: number
}

/** 相交即选中（含边贴边）：fixture 的 partial-intersection 恰好压着 b 的左沿。 */
export function nodesInMarquee(
  nodes: readonly SelectableNode[],
  rect: MarqueeRect,
  footprint: { width: number; height: number } = NODE_SELECTION_FOOTPRINT,
): string[] {
  return nodes
    .filter(
      (node) =>
        rect.x <= node.position.x + footprint.width &&
        rect.x + rect.width >= node.position.x &&
        rect.y <= node.position.y + footprint.height &&
        rect.y + rect.height >= node.position.y,
    )
    .map((node) => node.id)
}

export interface CanvasSelectionOptions {
  /** 实例/版本模式只读：允许选与定位，不允许移动。 */
  readOnly?: () => boolean
  onChange?: (selection: { nodeIds: readonly string[]; edgeIds: readonly string[] }) => void
}

export function useCanvasSelection(options: CanvasSelectionOptions = {}) {
  const selectedNodeIds = ref<string[]>([])
  const selectedEdgeIds = ref<string[]>([])

  const primaryNodeId = computed(() => selectedNodeIds.value[selectedNodeIds.value.length - 1] ?? null)
  const isEmpty = computed(() => selectedNodeIds.value.length === 0 && selectedEdgeIds.value.length === 0)
  const isReadOnly = computed(() => options.readOnly?.() ?? false)

  function notify(): void {
    options.onChange?.({ nodeIds: selectedNodeIds.value, edgeIds: selectedEdgeIds.value })
  }

  function isNodeSelected(nodeId: string): boolean {
    return selectedNodeIds.value.includes(nodeId)
  }

  function isEdgeSelected(edgeId: string): boolean {
    return selectedEdgeIds.value.includes(edgeId)
  }

  function setNodes(ids: readonly string[]): void {
    selectedNodeIds.value = [...new Set(ids)]
    notify()
  }

  function setEdges(ids: readonly string[]): void {
    selectedEdgeIds.value = [...new Set(ids)]
    notify()
  }

  /** 点节点：默认单选；Shift 在已有集合里增删。点节点总会清掉边的选择。 */
  function clickNode(nodeId: string, shift = false): void {
    selectedEdgeIds.value = []
    if (!shift) {
      selectedNodeIds.value = [nodeId]
    } else if (isNodeSelected(nodeId)) {
      selectedNodeIds.value = selectedNodeIds.value.filter((id) => id !== nodeId)
    } else {
      selectedNodeIds.value = [...selectedNodeIds.value, nodeId]
    }
    notify()
  }

  /** 点边：与点节点互斥；Shift 支持多条边。 */
  function clickEdge(edgeId: string, shift = false): void {
    selectedNodeIds.value = []
    if (!shift) {
      selectedEdgeIds.value = [edgeId]
    } else if (isEdgeSelected(edgeId)) {
      selectedEdgeIds.value = selectedEdgeIds.value.filter((id) => id !== edgeId)
    } else {
      selectedEdgeIds.value = [...selectedEdgeIds.value, edgeId]
    }
    notify()
  }

  function clickBlank(): void {
    selectedNodeIds.value = []
    selectedEdgeIds.value = []
    notify()
  }

  /** 框选：默认替换，Shift 与现有集合取并集。 */
  function marquee(nodes: readonly SelectableNode[], rect: MarqueeRect, shift = false): readonly string[] {
    const hit = nodesInMarquee(nodes, rect)
    if (shift) {
      selectedNodeIds.value = [...new Set([...selectedNodeIds.value, ...hit])]
    } else {
      selectedNodeIds.value = hit
      selectedEdgeIds.value = []
    }
    notify()
    return selectedNodeIds.value
  }

  /**
   * 多选拖动：只读模式或空选择时**原样返回**（同一引用），
   * 调用方据 `next === positions` 就能知道这次不该落盘。
   */
  function moveSelected(positions: Record<string, Position>, delta: Position): Record<string, Position> {
    if (isReadOnly.value || selectedNodeIds.value.length === 0) return positions

    let changed = false
    const next: Record<string, Position> = { ...positions }
    for (const nodeId of selectedNodeIds.value) {
      const current = positions[nodeId]
      if (current === undefined) continue
      next[nodeId] = { x: current.x + delta.x, y: current.y + delta.y }
      changed = true
    }
    return changed ? next : positions
  }

  return {
    selectedNodeIds,
    selectedEdgeIds,
    primaryNodeId,
    isEmpty,
    isReadOnly,
    isNodeSelected,
    isEdgeSelected,
    setNodes,
    setEdges,
    clickNode,
    clickEdge,
    clickBlank,
    marquee,
    moveSelected,
  }
}
