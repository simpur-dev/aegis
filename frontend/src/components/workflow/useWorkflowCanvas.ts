/**
 * 画布交互粘合层：把 store 的模型换算成 Vue Flow 视图，并把 Vue Flow 事件写回 store。
 *
 * 单独抽出是为了让 WorkflowView 只做排版：模型 → 视图是单向 computed，
 * 视图 → 模型只有下面这几个显式事件处理器，任何时刻都能从 store 重建画布。
 */
import { useVueFlow, type Connection, type NodeDragEvent, type NodeMouseEvent } from '@vue-flow/core'
import { computed, onBeforeUnmount, onMounted, watch } from 'vue'

import type { WorkflowStore } from '@/stores/workflow'
import {
  CANVAS_DEFAULT_ZOOM,
  canvasShortcut,
  defToGraph,
  freeDropPosition,
  nextNodeId,
  strokeCompensation,
  type FlowBox,
  type GraphView,
  type NodeState,
  type NodeType,
  type Position,
} from '@/utils/graph'

import { createNodeDef, NODE_DRAG_MIME, nodeMeta } from './registry'

/**
 * 画布可视区（或画布上某块浮层）在 flow 坐标系里的矩形。
 *
 * 取 drop 事件的 currentTarget（监听器挂在 `.wf__canvas` 上）而不是 Vue Flow 的 dimensions：
 * 前者才是用户眼前那块。corners 必须用与落点同一个投影函数换算，否则缩放/平移之后
 * 框和节点坐标不是一套单位。
 */
export function visibleFlowBox(
  target: EventTarget | null,
  project: (point: Position) => Position,
): FlowBox | null {
  if (!(target instanceof HTMLElement)) return null
  const rect = target.getBoundingClientRect()
  if (rect.width === 0 || rect.height === 0) return null
  const tl = project({ x: rect.left, y: rect.top })
  const br = project({ x: rect.right, y: rect.bottom })
  return { x: tl.x, y: tl.y, width: br.x - tl.x, height: br.y - tl.y }
}

/** 画布上的不透明浮层（小地图、缩放控件）——落在它们底下的节点等于没显示出来。 */
export function overlayFlowBoxes(
  target: EventTarget | null,
  project: (point: Position) => Position,
): FlowBox[] {
  if (!(target instanceof HTMLElement)) return []
  return [...target.querySelectorAll('.vue-flow__minimap, .vue-flow__controls')]
    .map((el) => visibleFlowBox(el, project))
    .filter((box): box is FlowBox => box !== null)
}

export function useWorkflowCanvas(store: WorkflowStore) {
  const { screenToFlowCoordinate, fitView, zoomIn, zoomOut, zoomTo, viewport } = useVueFlow()

  /** 描边补偿系数（数学在 `strokeCompensation`，那里有单测）。 */
  const strokeScale = computed<number>(() => strokeCompensation(viewport.value.zoom))

  /**
   * 把整条链路收进视野，且只看不放大。
   *
   * 库里的 fit-view-on-init 不能用在这里：它跑在"第一颗被测出尺寸的节点"上，
   * 既会被用户手动拖入的第一颗节点吃掉（此后打开已存定义就不再 fit），
   * 又因为不带参数调 fitView，单颗节点会按 maxZoom=2 放大——可视区缩到不到两个节点宽，
   * 第二颗节点怎么放都出画布（真机量到 414×339 flow 单位）。
   */
  async function fitToGraph(): Promise<void> {
    /* 标签页在背后时 Vue Flow 量不出容器尺寸（退回 500×500），按那份假尺寸收拢一次
       等于把节点推到看不见的地方——先记账，等切回可见再补做。 */
    if (document.hidden) {
      pendingFit = true
      return
    }
    for (let attempt = 0; attempt < 12; attempt += 1) {
      /* 节点尺寸要等渲染完才测得出，fitView 在没有可 fit 的节点时返回 false。 */
      if (await fitView({ padding: 0.2, maxZoom: 1 })) return
      await new Promise((resolve) => window.setTimeout(resolve, 50))
    }
  }

  let pendingFit = false

  function onVisibilityChange(): void {
    if (document.hidden || !pendingFit) return
    pendingFit = false
    void fitToGraph()
  }

  /** 打字的时候不许抢键：输入框里按 1 是输入"1"，不是收拢视野。 */
  function isTypingAt(target: EventTarget | null): boolean {
    if (!(target instanceof HTMLElement)) return false
    return target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)
  }

  function onKeydown(event: KeyboardEvent): void {
    if (event.defaultPrevented || isTypingAt(event.target)) return
    const action = canvasShortcut(event.key, event.ctrlKey || event.metaKey || event.altKey)
    if (action === null) return
    event.preventDefault()
    if (action === 'fit') void fitToGraph()
    else if (action === 'reset') void zoomTo(CANVAS_DEFAULT_ZOOM)
    else if (action === 'zoom-in') void zoomIn()
    else void zoomOut()
  }

  onMounted(() => {
    window.addEventListener('keydown', onKeydown)
    document.addEventListener('visibilitychange', onVisibilityChange)
  })
  onBeforeUnmount(() => {
    window.removeEventListener('keydown', onKeydown)
    document.removeEventListener('visibilitychange', onVisibilityChange)
  })

  watch(
    () => store.current?.workflow_id ?? '',
    (workflowId) => {
      if (workflowId !== '') void fitToGraph()
    },
  )

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
    /* 落点让位：连拖两个节点到同一处会叠住（真机量到 389×234px 压叠）；
       只躲压叠不躲可视区时又被推到画布下沿之外（y=704..957，画布底 880）；
       收进框内后又压在小地图那块不透明卡片下（39×156px）。三种"看不见"一起交给让位逻辑。 */
    const taken = view.value.nodes.map((node) => ({ x: node.position.x, y: node.position.y }))
    store.addNode(
      createNodeDef(raw as NodeType, nextNodeId(def, raw)),
      freeDropPosition(
        taken,
        position,
        24,
        visibleFlowBox(event.currentTarget, screenToFlowCoordinate),
        overlayFlowBoxes(event.currentTarget, screenToFlowCoordinate),
      ),
    )
  }

  return {
    view,
    fitToGraph,
    strokeScale,
    onConnect,
    onNodeClick,
    onPaneClick,
    onNodeDragStart,
    onNodeDragStop,
    onDragOver,
    onDrop,
  }
}
