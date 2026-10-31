/**
 * 画布交互粘合层：把 store 的模型换算成 Vue Flow 视图，并把 Vue Flow 事件写回 store。
 *
 * 单独抽出是为了让 WorkflowView 只做排版：模型 → 视图是单向 computed，
 * 视图 → 模型只有下面这几个显式事件处理器，任何时刻都能从 store 重建画布。
 */
import {
  useVueFlow,
  type Connection,
  type EdgeMouseEvent,
  type NodeDragEvent,
  type NodeMouseEvent,
} from '@vue-flow/core'
import { computed, onBeforeUnmount, onMounted, ref, watch, type Ref } from 'vue'

import { useCanvasSelection } from '@/composables/workflow/useCanvasSelection'
import { readWorkflowClipboard, writeWorkflowClipboard } from '@/composables/workflow/useWorkflowClipboard'
import { focusNode } from '@/composables/workflow/useNodeNavigation'
import {
  CANVAS_DEFAULT_ZOOM,
  defToGraph,
  freeDropPosition,
  nextAvailableId,
  nextNodeId,
  strokeCompensation,
  type FlowBox,
  type GraphView,
  type NodeState,
  type NodeType,
  type Position,
} from '@/utils/graph'
import {
  clipboardErrorMessage,
  clipboardOffsetTo,
  extractWorkflowClipboardSubgraph,
  remapWorkflowClipboardSubgraph,
  type ClipboardPayload,
} from '@/utils/graph/clipboard'

import type { EditorContext } from './editor/context'
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

/** 画布上的不透明浮层（小地图、缩放控件、覆盖式编排面板）——落在它们底下的节点等于没显示出来。 */
export function overlayFlowBoxes(
  target: EventTarget | null,
  project: (point: Position) => Position,
): FlowBox[] {
  if (!(target instanceof HTMLElement)) return []
  /* 编排面板挂在 `.ed-canvas` **外面**（它是覆盖在画布上的兄弟节点），只量画布内部就永远躲不开它：
     真机量到第三颗节点落进面板那 400px 里，被压住 26×122px。所以以 `.ed-body` 为搜索根。 */
  const root = target.closest('.ed-body') ?? target
  return [...root.querySelectorAll('.vue-flow__minimap, .vue-flow__controls, .ed-toolbar, .ed-panel')]
    .map((el) => visibleFlowBox(el, project))
    .filter((box): box is FlowBox => box !== null)
}

export interface WorkflowCanvasOptions {
  /** 画布容器：粘贴没跟过鼠标时，用它把可见区中心换成 flow 坐标。 */
  canvasRef?: Ref<HTMLElement | null>
  /** 剪贴板/只读等失败原因由页面决定怎么展示（toast/banner）。 */
  notify?: (text: string, tone: 'error' | 'success') => void
}

export function useWorkflowCanvas(
  { catalog, document: documentStore, editor, run }: EditorContext,
  options: WorkflowCanvasOptions = {},
) {
  const { screenToFlowCoordinate, fitView, zoomIn, zoomOut, zoomTo, viewport, setViewport } = useVueFlow()

  /** 描边补偿系数（数学在 `strokeCompensation`，那里有单测）。 */
  const strokeScale = computed<number>(() => strokeCompensation(viewport.value.zoom))

  let lastPointer: { x: number; y: number } | null = null
  let suppressPaneClick = false

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

  onMounted(() => {
    document.addEventListener('visibilitychange', onVisibilityChange)
    window.addEventListener('click', onWindowClickCapture, true)
  })
  onBeforeUnmount(() => {
    document.removeEventListener('visibilitychange', onVisibilityChange)
    window.removeEventListener('click', onWindowClickCapture, true)
    stopMarquee()
  })

  /** 只有"换了一份文档"才收拢视野；每次编辑都会替换文档对象，不能拿它当信号。 */
  watch(
    () => documentStore.documentEpoch,
    () => {
      if (documentStore.definition !== null) void fitToGraph()
    },
  )

  const selection = useCanvasSelection({
    readOnly: () => !documentStore.autoSaveEnabled,
    onChange: ({ nodeIds }) => {
      const primary = nodeIds[nodeIds.length - 1] ?? null
      editor.select(primary)
    },
  })

  /**
   * 反向同步：节点面板/运行态等外部入口只写 editor.selectedNodeId，
   * 画布高亮以 selection 为准，不接这一步就会出现"右侧显示 X，画布上找不到 X"。
   */
  watch(
    () => editor.selectedNodeId,
    (nodeId) => {
      const selected = selection.selectedNodeIds.value
      if (nodeId === null) {
        if (selected.length > 0) selection.setNodes([])
        return
      }
      if (selected[selected.length - 1] === nodeId) return
      selection.setNodes([nodeId])
    },
    { flush: 'sync', immediate: true },
  )

  const view = computed<GraphView>(() => {
    const def = documentStore.definition
    if (def === null) return { nodes: [], edges: [] }
    const states: Record<string, { state: NodeState; attempts: number; note: string }> = {}
    for (const nodeRun of run.canvasRuns) {
      states[nodeRun.node_id] = {
        state: nodeRun.state,
        attempts: nodeRun.attempts,
        note: nodeRun.notes.at(-1) ?? '',
      }
    }
    const graph = defToGraph(def, {
      positions: documentStore.document?.layout.positions ?? {},
      states,
      descriptions: catalog.descriptionByType,
    })
    const nodes = graph.nodes.map((node) => ({ ...node, selected: selection.isNodeSelected(node.id) }))
    const edges = graph.edges.map((edge) => (selection.isEdgeSelected(edge.id) ? { ...edge, selected: true } : edge))
    /* 备注排在执行节点之前：同一叠放层里后渲染的节点盖住便签，与 Dify 的“便签垫底”一致。 */
    const notes = (documentStore.document?.layout.notes ?? []).map((note) => ({
      id: note.note_id,
      type: 'note' as const,
      position: { x: note.x, y: note.y },
      selected: editor.selectedNoteId === note.note_id,
      data: { note },
    }))
    return { nodes: [...notes, ...nodes], edges }
  })

  function onConnect(connection: Connection): void {
    documentStore.connect(connection.source, connection.target)
  }

  /** 工具栏/空白双击添加节点时，把视口坐标换成 flow 坐标（与拖放落点同一个投影函数）。 */
  function screenPointToFlow(x: number, y: number): Position {
    return screenToFlowCoordinate({ x, y })
  }

  function onNodeClick({ node, event }: NodeMouseEvent): void {
    if (node.type === 'note') {
      selection.clickBlank()
      editor.selectNote(node.id)
      return
    }
    selection.clickNode(node.id, event.shiftKey)
  }

  function onEdgeClick({ edge, event }: EdgeMouseEvent): void {
    selection.clickEdge(edge.id, event.shiftKey)
  }

  function onPaneClick(): void {
    if (suppressPaneClick) {
      suppressPaneClick = false
      return
    }
    selection.clickBlank()
  }

  /**
   * 框选松手后的那一下 pane click 必须整条拦住：Vue Flow 的 Pane 自己
   * 先 `emit paneClick` 再 `removeSelectedElements()`（库内没有关掉的口子），
   * 只在 `onPaneClick` 里 return 的话，库紧接着就把刚框出来的节点取消选中。
   * 捕获阶段挡掉；标记留到下一次 mousedown 复位，不再赌定时器。
   */
  function onWindowClickCapture(event: MouseEvent): void {
    if (!suppressPaneClick) return
    const target = event.target
    if (!(target instanceof HTMLElement) || target.closest('.vue-flow__pane') === null) return
    event.stopPropagation()
    event.preventDefault()
    suppressPaneClick = false
  }

  function editable(): boolean {
    return documentStore.autoSaveEnabled && documentStore.definition !== null
  }

  function notifyError(error: unknown): void {
    options.notify?.(clipboardErrorMessage(error), 'error')
  }

  function deleteSelection(): boolean {
    if (!editable()) return false
    const nodeIds = selection.selectedNodeIds.value
    const edgeIds = selection.selectedEdgeIds.value
    if (nodeIds.length === 0 && edgeIds.length === 0) {
      const noteId = editor.selectedNoteId
      if (noteId === null) return false
      documentStore.removeNote(noteId)
      editor.selectNote(null)
      return true
    }
    const changed = documentStore.removeSelection(nodeIds, edgeIds)
    if (changed) selection.clickBlank()
    return changed
  }

  async function copySelection(): Promise<boolean> {
    const document = documentStore.document
    if (!editable() || document === null) return false
    if (selection.selectedNodeIds.value.length === 0) return false

    try {
      await writeWorkflowClipboard(extractWorkflowClipboardSubgraph(document, selection.selectedNodeIds.value))
      return true
    } catch (error) {
      notifyError(error)
      return false
    }
  }

  async function cutSelection(): Promise<boolean> {
    if (!(await copySelection())) return false
    return deleteSelection()
  }

  function insertPayloadWithOffset(payload: ClipboardPayload, offset: Position): boolean {
    const def = documentStore.definition
    if (def === null) return false

    const taken = new Set(def.nodes.map((node) => node.node_id))
    const allocateId = (type: NodeType): string => {
      const nodeId = nextAvailableId(taken, type)
      taken.add(nodeId)
      return nodeId
    }

    const subgraph = remapWorkflowClipboardSubgraph(payload, { allocateId, createDefault: createNodeDef, offset })
    if (subgraph.nodes.length === 0) return false
    if (!documentStore.insertSubgraph(subgraph.nodes, subgraph.edges, subgraph.positions)) return false

    selection.setNodes(subgraph.nodes.map((node) => node.node_id))
    return true
  }

  /** 没跟过鼠标时落在画布可见区中心；否则落在最后一次指针位置（Dify 的粘贴手感）。 */
  function defaultDropPoint(): Position {
    if (lastPointer !== null) return screenToFlowCoordinate(lastPointer)
    const rect = options.canvasRef?.value?.getBoundingClientRect()
    if (rect === undefined) return { x: 0, y: 0 }
    return screenToFlowCoordinate({ x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 })
  }

  async function pasteClipboard(drop?: Position): Promise<boolean> {
    if (!editable()) return false
    try {
      const parsed = await readWorkflowClipboard()
      const point = drop ?? defaultDropPoint()
      return insertPayloadWithOffset(
        { nodes: parsed.nodes, edges: parsed.edges },
        clipboardOffsetTo(parsed.nodes, point),
      )
    } catch (error) {
      notifyError(error)
      return false
    }
  }

  function duplicateSelection(): boolean {
    const document = documentStore.document
    if (!editable() || document === null) return false
    if (selection.selectedNodeIds.value.length === 0) return false
    return insertPayloadWithOffset(extractWorkflowClipboardSubgraph(document, selection.selectedNodeIds.value), {
      x: 24,
      y: 24,
    })
  }

  function undo(): boolean {
    if (!documentStore.undo()) return false
    selection.clickBlank()
    return true
  }

  function redo(): boolean {
    if (!documentStore.redo()) return false
    selection.clickBlank()
    return true
  }

  function moveSelection(delta: Position): boolean {
    if (!editable()) return false
    const positions: Record<string, Position> = {}
    for (const node of view.value.nodes) {
      if (node.type !== 'note') positions[node.id] = node.position
    }
    const next = selection.moveSelected(positions, delta)
    if (next === positions) return false
    documentStore.moveNodes(next)
    return true
  }

  function selectNode(nodeId: string | null): void {
    if (nodeId === null) selection.clickBlank()
    else selection.setNodes([nodeId])
  }

  function organize(): void {
    if (!editable()) return
    documentStore.autoLayout()
  }

  function resetZoom(): void {
    void zoomTo(CANVAS_DEFAULT_ZOOM)
  }

  function zoomTo100(): void {
    void zoomTo(1)
  }

  function zoomTo50(): void {
    void zoomTo(0.5)
  }

  function zoomInStep(): void {
    void zoomIn()
  }

  function zoomOutStep(): void {
    void zoomOut()
  }

  async function focusNodeById(nodeId: string): Promise<boolean> {
    const nodes = view.value.nodes
      .filter((node) => node.type !== 'note')
      .map((node) => ({ id: node.id, position: node.position }))
    return focusNode(
      nodes,
      nodeId,
      { width: window.innerWidth, height: window.innerHeight },
      async (transform, transformOptions) => {
        await setViewport(transform, transformOptions)
      },
    )
  }

  interface MarqueeState {
    startClient: { x: number; y: number }
    currentClient: { x: number; y: number }
    flowStart: Position
    additive: boolean
    active: boolean
  }

  const marqueeState = ref<MarqueeState | null>(null)
  const marqueeBox = computed(() => {
    const state = marqueeState.value
    if (state === null || !state.active) return null
    return {
      left: Math.min(state.startClient.x, state.currentClient.x),
      top: Math.min(state.startClient.y, state.currentClient.y),
      width: Math.abs(state.currentClient.x - state.startClient.x),
      height: Math.abs(state.currentClient.y - state.startClient.y),
    }
  })

  function onCanvasMouseMove(event: MouseEvent): void {
    lastPointer = { x: event.clientX, y: event.clientY }
  }

  function onCanvasMouseDown(event: MouseEvent): void {
    /* 新手势开始即作废上一次框选留下的"待吞 click"标记：
       若上次框选结束在节点上（没有 pane click 可吞），它不该吃掉这一次真实的点空白。 */
    suppressPaneClick = false
    if (editor.canvasMode !== 'pointer' || event.button !== 0) return
    const target = event.target
    if (!(target instanceof HTMLElement) || target.closest('.vue-flow__pane') === null) return
    event.preventDefault()
    marqueeState.value = {
      startClient: { x: event.clientX, y: event.clientY },
      currentClient: { x: event.clientX, y: event.clientY },
      flowStart: screenToFlowCoordinate({ x: event.clientX, y: event.clientY }),
      additive: event.shiftKey,
      active: false,
    }
    window.addEventListener('mousemove', onMarqueeMove)
    window.addEventListener('mouseup', onMarqueeUp)
  }

  function onMarqueeMove(event: MouseEvent): void {
    const state = marqueeState.value
    if (state === null) return

    const moved = Math.hypot(event.clientX - state.startClient.x, event.clientY - state.startClient.y)
    if (!state.active && moved < 3) return

    const current = { x: event.clientX, y: event.clientY }
    marqueeState.value = { ...state, currentClient: current, active: true }

    const end = screenToFlowCoordinate(current)
    const rect = {
      x: Math.min(state.flowStart.x, end.x),
      y: Math.min(state.flowStart.y, end.y),
      width: Math.abs(end.x - state.flowStart.x),
      height: Math.abs(end.y - state.flowStart.y),
    }
    const nodes = view.value.nodes
      .filter((node) => node.type !== 'note')
      .map((node) => ({ id: node.id, position: node.position }))
    selection.marquee(nodes, rect, state.additive)
  }

  function onMarqueeUp(): void {
    if (marqueeState.value?.active === true) {
      /* 框选结束浏览器还会补一个 pane click；那个 click 不是"点空白取消选择"。
         不能用 setTimeout(0) 兜：实测 click 在 mouseup 后 0.7ms、但排在下一轮任务里，
         定时器先跑会把框选结果清掉。这里留成"下一次 click 消费掉"，新手势按下时复位。 */
      suppressPaneClick = true
    }
    stopMarquee()
  }

  function stopMarquee(): void {
    window.removeEventListener('mousemove', onMarqueeMove)
    window.removeEventListener('mouseup', onMarqueeUp)
    marqueeState.value = null
  }

  let dragStartPositions: Record<string, Position> | null = null

  /** 首次拖动前把自动布局坐标固化，避免新节点/旧节点混用两套坐标系而重叠。 */
  function onNodeDragStart(): void {
    const positions: Record<string, Position> = {}
    for (const node of view.value.nodes) {
      if (node.type !== 'note') positions[node.id] = node.position
    }
    documentStore.seedPositions(positions)
    dragStartPositions = positions
  }

  function onNodeDragStop({ node }: NodeDragEvent): void {
    if (node.type === 'note') {
      documentStore.updateNote(node.id, { x: node.position.x, y: node.position.y })
      dragStartPositions = null
      return
    }

    /* 多选时拖其中一颗，其余节点跟着同一个位移落盘；只写拖动节点会让选择框和实际位置错开。 */
    const selected = selection.selectedNodeIds.value
    const start = dragStartPositions?.[node.id]
    if (selected.length > 1 && start !== undefined && selection.isNodeSelected(node.id)) {
      const delta = { x: node.position.x - start.x, y: node.position.y - start.y }
      const next: Record<string, Position> = {}
      for (const nodeId of selected) {
        const from = dragStartPositions?.[nodeId]
        if (from !== undefined) next[nodeId] = { x: from.x + delta.x, y: from.y + delta.y }
      }
      documentStore.moveNodes(next)
      dragStartPositions = null
      return
    }

    documentStore.moveNode(node.id, { x: node.position.x, y: node.position.y })
    dragStartPositions = null
  }

  function onDragOver(event: DragEvent): void {
    event.preventDefault()
    if (event.dataTransfer !== null) event.dataTransfer.dropEffect = 'move'
  }

  function onDrop(event: DragEvent): void {
    event.preventDefault()
    const raw = event.dataTransfer?.getData(NODE_DRAG_MIME) ?? ''
    const def = documentStore.definition
    /** nodeMeta 非空即证明该类型在注册表内，故把 string 收窄为 NodeType 是安全的。 */
    if (def === null || nodeMeta(raw) === null) return
    const position = screenToFlowCoordinate({ x: event.clientX, y: event.clientY })
    /* 落点让位：连拖两个节点到同一处会叠住（真机量到 389×234px 压叠）；
       只躲压叠不躲可视区时又被推到画布下沿之外（y=704..957，画布底 880）；
       收进框内后又压在小地图那块不透明卡片下（39×156px）。三种"看不见"一起交给让位逻辑。 */
    const taken = view.value.nodes.map((node) => ({ x: node.position.x, y: node.position.y }))
    const created = createNodeDef(raw as NodeType, nextNodeId(def, raw))
    documentStore.addNode(
      created,
      freeDropPosition(
        taken,
        position,
        24,
        visibleFlowBox(event.currentTarget, screenToFlowCoordinate),
        overlayFlowBoxes(event.currentTarget, screenToFlowCoordinate),
      ),
    )
    selection.setNodes([created.node_id])
  }

  return {
    view,
    selection,
    marqueeBox,
    fitToGraph,
    strokeScale,
    onConnect,
    onNodeClick,
    onEdgeClick,
    onPaneClick,
    onCanvasMouseDown,
    onCanvasMouseMove,
    onNodeDragStart,
    onNodeDragStop,
    onDragOver,
    onDrop,
    screenPointToFlow,
    selectNode,
    deleteSelection,
    copySelection,
    cutSelection,
    pasteClipboard,
    duplicateSelection,
    undo,
    redo,
    moveSelection,
    organize,
    resetZoom,
    zoomTo100,
    zoomTo50,
    zoomInStep,
    zoomOutStep,
    focusNodeById,
  }
}
