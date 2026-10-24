/**
 * 画布交互粘合层：把 store 的模型换算成 Vue Flow 视图，并把 Vue Flow 事件写回 store。
 *
 * 单独抽出是为了让 WorkflowView 只做排版：模型 → 视图是单向 computed，
 * 视图 → 模型只有下面这几个显式事件处理器，任何时刻都能从 store 重建画布。
 */
import { useVueFlow, type Connection, type NodeDragEvent, type NodeMouseEvent } from '@vue-flow/core'
import { computed, onBeforeUnmount, onMounted, watch } from 'vue'

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

export function useWorkflowCanvas({ catalog, document: documentStore, editor, run }: EditorContext) {
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
    if (event.defaultPrevented) return
    // Mod+S 立即落草稿：自动保存还在 800ms 防抖里时，让人自己决定"现在就存"。
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
      event.preventDefault()
      void documentStore.flush()
      return
    }
    if (isTypingAt(event.target)) return
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

  /** 只有"换了一份文档"才收拢视野；每次编辑都会替换文档对象，不能拿它当信号。 */
  watch(
    () => documentStore.documentEpoch,
    () => {
      if (documentStore.definition !== null) void fitToGraph()
    },
  )

  const view = computed<GraphView>(() => {
    const def = documentStore.definition
    if (def === null) return { nodes: [], edges: [] }
    const states: Record<string, { state: NodeState; attempts: number; note: string }> = {}
    for (const nodeRun of run.instance?.nodes ?? []) {
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
      selectedNodeId: editor.selectedNodeId,
    })
    /* 备注排在执行节点之前：同一叠放层里后渲染的节点盖住便签，与 Dify 的“便签垫底”一致。 */
    const notes = (documentStore.document?.layout.notes ?? []).map((note) => ({
      id: note.note_id,
      type: 'note' as const,
      position: { x: note.x, y: note.y },
      selected: editor.selectedNoteId === note.note_id,
      data: { note },
    }))
    return { nodes: [...notes, ...graph.nodes], edges: graph.edges }
  })

  function onConnect(connection: Connection): void {
    documentStore.connect(connection.source, connection.target)
  }

  /** 工具栏/空白双击添加节点时，把视口坐标换成 flow 坐标（与拖放落点同一个投影函数）。 */
  function screenPointToFlow(x: number, y: number): Position {
    return screenToFlowCoordinate({ x, y })
  }

  function onNodeClick({ node }: NodeMouseEvent): void {
    if (node.type === 'note') {
      editor.selectNote(node.id)
      return
    }
    editor.select(node.id)
  }

  function onPaneClick(): void {
    editor.select(null)
  }

  /** 首次拖动前把自动布局坐标固化，避免新节点/旧节点混用两套坐标系而重叠。 */
  function onNodeDragStart(): void {
    const positions: Record<string, Position> = {}
    for (const node of view.value.nodes) {
      if (node.type !== 'note') positions[node.id] = node.position
    }
    documentStore.seedPositions(positions)
  }

  function onNodeDragStop({ node }: NodeDragEvent): void {
    if (node.type === 'note') {
      documentStore.updateNote(node.id, { x: node.position.x, y: node.position.y })
      return
    }
    documentStore.moveNode(node.id, { x: node.position.x, y: node.position.y })
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
    editor.select(created.node_id)
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
    screenPointToFlow,
  }
}
