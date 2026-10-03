/**
 * 画布交互粘合层：把 store 的模型换算成 Vue Flow 视图，并把 Vue Flow 事件写回 store。
 *
 * 单独抽出是为了让 WorkflowView 只做排版：模型 → 视图是单向 computed，
 * 视图 → 模型只有下面这几个显式事件处理器，任何时刻都能从 store 重建画布。
 */
import { useVueFlow, type Connection, type NodeDragEvent, type NodeMouseEvent } from '@vue-flow/core'
import { computed } from 'vue'

import type { WorkflowStore } from '@/stores/workflow'
import { defToGraph, nextNodeId, type GraphView, type NodeState, type NodeType } from '@/utils/graph'

import { createNodeDef, NODE_DRAG_MIME, nodeMeta } from './registry'

export function useWorkflowCanvas(store: WorkflowStore) {
  const { screenToFlowCoordinate } = useVueFlow()

  const view = computed<GraphView>(() => {
    const def = store.current
    if (def === null) return { nodes: [], edges: [] }
    const states: Record<string, { state: NodeState; attempts: number; note: string }> = {}
    for (const run of store.instance?.nodes ?? []) {
      states[run.node_id] = { state: run.state, attempts: run.attempts, note: run.notes.at(-1) ?? '' }
    }
    return defToGraph(def, {
      positions: store.positions,
      states,
      descriptions: store.descriptionByType,
      selectedNodeId: store.selectedNodeId,
    })
  })

  function onConnect(connection: Connection): void {
    store.connect(connection.source, connection.target)
  }

  function onNodeClick({ node }: NodeMouseEvent): void {
    store.select(node.id)
  }

  function onPaneClick(): void {
    store.select(null)
  }

  /** 首次拖动前把自动布局坐标固化，避免新节点/旧节点混用两套坐标系而重叠。 */
  function onNodeDragStart(): void {
    store.seedPositions(Object.fromEntries(view.value.nodes.map((node) => [node.id, node.position])))
  }

  function onNodeDragStop({ node }: NodeDragEvent): void {
    store.moveNode(node.id, { x: node.position.x, y: node.position.y })
  }

  function onDragOver(event: DragEvent): void {
    event.preventDefault()
    if (event.dataTransfer !== null) event.dataTransfer.dropEffect = 'move'
  }

  function onDrop(event: DragEvent): void {
    event.preventDefault()
    const raw = event.dataTransfer?.getData(NODE_DRAG_MIME) ?? ''
    const def = store.current
    /** nodeMeta 非空即证明该类型在注册表内，故把 string 收窄为 NodeType 是安全的。 */
    if (def === null || nodeMeta(raw) === null) return
    const position = screenToFlowCoordinate({ x: event.clientX, y: event.clientY })
    store.addNode(createNodeDef(raw as NodeType, nextNodeId(def, raw)), { x: position.x, y: position.y })
  }

  return { view, onConnect, onNodeClick, onPaneClick, onNodeDragStart, onNodeDragStop, onDragOver, onDrop }
}
