<script setup lang="ts">
/**
 * 工作流编排页（沉浸式）：56px 编辑器头部 + 全屏 Vue Flow 画布 + 400px 覆盖式右栏。
 *
 * 结构对齐 Dify 工作流编辑器：应用侧栏/顶栏/页脚在 `/workflow` 路由上全部让位，
 * 画布不再被左右两列挤压；面板从头部下沿盖住画布右侧，关闭后画布恢复满宽。
 * 四个 store 仍由本页注入，画布事件粘合在 useWorkflowCanvas。
 */
import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'
import '@vue-flow/minimap/dist/style.css'
import '@vue-flow/controls/dist/style.css'

import { VueFlow, MarkerType, type EdgeMouseEvent, type NodeMouseEvent } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import { MiniMap } from '@vue-flow/minimap'
import { message, Modal } from 'ant-design-vue'
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import DefinitionInspector from '@/components/workflow/DefinitionInspector.vue'
import NodeInspector from '@/components/workflow/NodeInspector.vue'
import RuntimePanel from '@/components/workflow/RuntimePanel.vue'
import BlockSelector from '@/components/workflow/editor/BlockSelector.vue'
import CanvasContextMenu from '@/components/workflow/editor/CanvasContextMenu.vue'
import EditorHeader from '@/components/workflow/editor/EditorHeader.vue'
import EditorToolbar from '@/components/workflow/editor/EditorToolbar.vue'
import NodeLocator from '@/components/workflow/editor/NodeLocator.vue'
import NodePanel from '@/components/workflow/editor/NodePanel.vue'
import { type ContextMenuItem, type LocatorNode } from '@/components/workflow/editor/menus'
import ShortcutHelp from '@/components/workflow/editor/ShortcutHelp.vue'
import { confirmDiscardUnsaved, installUnsavedGuard } from '@/components/workflow/confirmDiscard'
import { provideEditorContext } from '@/components/workflow/editor/context'
import { provideBlockInsertion, type InsertionRequest } from '@/components/workflow/editor/insertion'
import { createNodeDef } from '@/components/workflow/registry'
import { WORKFLOW_EDGE_TYPES, WORKFLOW_NODE_TYPES } from '@/components/workflow/nodeComponents'
import { overlayFlowBoxes, useWorkflowCanvas, visibleFlowBox } from '@/components/workflow/useWorkflowCanvas'
import { useWorkflowShortcuts } from '@/components/workflow/useWorkflowShortcuts'
import {
  desiredWorkflowQuery,
  nextWorkflowQuery,
  readWorkflowDeepLink,
  type WorkflowUrlState,
} from '@/composables/workflow/urlState'
import { layout as layoutTokens } from '@/theme/tokens'
import {
  CANVAS_DEFAULT_ZOOM,
  NODE_FOOTPRINT,
  freeDropPosition,
  nextNodeId,
  type NodeType,
  type Position,
} from '@/utils/graph'

const context = provideEditorContext()
const { catalog, document: documentStore, editor, run } = context
const route = useRoute()
const router = useRouter()

const canvasRef = ref<HTMLElement | null>(null)

const {
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
  resetZoom,
  zoomTo100,
  zoomTo50,
  zoomInStep,
  zoomOutStep,
  focusNodeById,
} = useWorkflowCanvas(context, {
  canvasRef,
  notify: (text, tone) => {
    if (tone === 'error') message.error(text)
    else message.success(text)
  },
})
const request = ref<InsertionRequest | null>(null)
provideBlockInsertion({
  request,
  open: (next) => {
    request.value = next
  },
  close: () => {
    request.value = null
  },
})

const helpOpen = ref(false)
const locateOpen = ref(false)

type MenuKind = 'node' | 'selection' | 'edge' | 'pane'
interface MenuState {
  x: number
  y: number
  kind: MenuKind
  items: ContextMenuItem[]
  point?: Position
}

const menu = ref<MenuState | null>(null)

function nodeMenuItems(multiple: boolean): ContextMenuItem[] {
  const items: ContextMenuItem[] = []
  if (!multiple) items.push({ id: 'open', label: '打开配置' })
  items.push(
    { id: 'copy', label: '复制', shortcut: 'Mod+C' },
    { id: 'cut', label: '剪切', shortcut: 'Mod+X' },
    { id: 'duplicate', label: '创建副本', shortcut: 'Mod+D' },
    { id: 'delete', label: '删除', shortcut: 'Delete', danger: true, separator: false },
  )
  return items
}

function paneMenuItems(): ContextMenuItem[] {
  const writable = documentStore.autoSaveEnabled
  return [
    { id: 'paste', label: '粘贴', shortcut: 'Mod+V', disabled: !writable },
    { id: 'add', label: '添加节点', disabled: !writable },
    { id: 'note', label: '添加备注', disabled: !writable },
    { id: 'sep-edit', label: '', separator: true },
    { id: 'undo', label: '撤销', shortcut: 'Mod+Z', disabled: !documentStore.canUndo },
    { id: 'redo', label: '重做', shortcut: 'Mod+Y', disabled: !documentStore.canRedo },
    { id: 'sep-view', label: '', separator: true },
    { id: 'organize', label: '自动整理', shortcut: 'Mod+O', disabled: !writable },
    { id: 'fit', label: '适配画布', shortcut: 'Mod+1' },
    { id: 'sep-help', label: '', separator: true },
    { id: 'help', label: '快捷键', shortcut: '?' },
  ]
}

function pointerCoords(event: MouseEvent | TouchEvent): { x: number; y: number } {
  if ('clientX' in event) return { x: event.clientX, y: event.clientY }
  const touch = event.touches[0] ?? event.changedTouches[0]
  return { x: touch?.clientX ?? 0, y: touch?.clientY ?? 0 }
}

function openNodeMenu({ event, node }: NodeMouseEvent): void {
  event.preventDefault()
  const { x, y } = pointerCoords(event)
  if (node.type === 'note') {
    selectNode(null)
    editor.selectNote(node.id)
    menu.value = {
      x,
      y,
      kind: 'node',
      items: [{ id: 'delete-note', label: '删除备注', shortcut: 'Delete', danger: true }],
    }
    return
  }
  if (!selection.isNodeSelected(node.id)) selectNode(node.id)
  const multiple = selection.selectedNodeIds.value.length > 1
  menu.value = {
    x,
    y,
    kind: multiple ? 'selection' : 'node',
    items: nodeMenuItems(multiple),
  }
}

function openEdgeMenu({ event, edge }: EdgeMouseEvent): void {
  event.preventDefault()
  const { x, y } = pointerCoords(event)
  if (!selection.isEdgeSelected(edge.id)) selection.clickEdge(edge.id)
  menu.value = {
    x,
    y,
    kind: 'edge',
    items: [{ id: 'delete-edge', label: '删除连线', shortcut: 'Delete', danger: true }],
  }
}

function openPaneMenu(event: MouseEvent): void {
  event.preventDefault()
  if (!selection.isEmpty.value) selection.clickBlank()
  menu.value = {
    x: event.clientX,
    y: event.clientY,
    kind: 'pane',
    items: paneMenuItems(),
    point: screenPointToFlow(event.clientX, event.clientY),
  }
}

function addNoteAt(point: Position): void {
  const noteId = documentStore.addNote({ x: Math.round(point.x), y: Math.round(point.y) })
  if (noteId !== null) editor.selectNote(noteId)
}

function onMenuSelect(id: string): void {
  const current = menu.value
  menu.value = null
  switch (id) {
    case 'open':
      revealPanel('node')
      return
    case 'copy':
      void copySelection()
      return
    case 'cut':
      void cutSelection()
      return
    case 'duplicate':
      duplicateSelection()
      return
    case 'delete':
      deleteSelection()
      return
    case 'delete-note': {
      const noteId = editor.selectedNoteId
      if (noteId !== null) {
        documentStore.removeNote(noteId)
        editor.selectNote(null)
      }
      return
    }
    case 'delete-edge':
      deleteSelection()
      return
    case 'paste':
      void pasteClipboard(current?.point)
      return
    case 'add':
      if (current !== null) request.value = { x: current.x, y: current.y, placement: 'cursor' }
      return
    case 'note':
      if (current?.point !== undefined) addNoteAt(current.point)
      return
    case 'undo':
      undo()
      return
    case 'redo':
      redo()
      return
    case 'organize':
      autoLayoutAndFit()
      return
    case 'fit':
      void fitToGraph()
      return
    case 'help':
      helpOpen.value = true
      return
    default:
      return
  }
}

const locatorNodes = computed<LocatorNode[]>(() =>
  (documentStore.document?.nodes ?? []).map((node) => ({ id: node.node_id, name: node.name, type: node.type })),
)

function pickLocatedNode(id: string): void {
  void focusNodeById(id)
  selectNode(id)
  revealPanel('node')
}

useWorkflowShortcuts({
  save: saveDraft,
  deleteSelection,
  copy: copySelection,
  cut: cutSelection,
  paste: () => pasteClipboard(),
  duplicate: duplicateSelection,
  undo,
  redo,
  setMode: (mode) => editor.setCanvasMode(mode),
  organize: autoLayoutAndFit,
  zoomToFit: () => void fitToGraph(),
  resetZoom,
  zoomTo100,
  zoomTo50,
  zoomIn: zoomInStep,
  zoomOut: zoomOutStep,
  help: () => {
    helpOpen.value = true
  },
  locate: () => {
    locateOpen.value = true
  },
  moveSelection,
})

type PanelTab = 'run' | 'def' | 'node'
const PANEL_STORAGE_KEY = 'aegis.workflow.panel'
const panelOpen = ref(false)
const panelTab = ref<PanelTab>(readPanelTab())

function readPanelTab(): PanelTab {
  try {
    const saved = localStorage.getItem(PANEL_STORAGE_KEY)
    if (saved === 'run' || saved === 'def' || saved === 'node') return saved
  } catch {
    /* 隐私模式或存储被禁：用默认档 */
  }
  return 'run'
}

function openPanel(tab: PanelTab): void {
  if (panelOpen.value && panelTab.value === tab) {
    panelOpen.value = false
    return
  }
  revealPanel(tab)
}

/**
 * 内容驱动的开档（选中节点、插入节点）：已经开着就只换档。
 * 走 openPanel 会把"再点一次就关"的手感带进来——连加两颗节点，第二颗刚选中就把面板关了。
 */
function revealPanel(tab: PanelTab): void {
  panelTab.value = tab
  panelOpen.value = true
  try {
    localStorage.setItem(PANEL_STORAGE_KEY, tab)
  } catch {
    /* 存不下就算了 */
  }
}

function closePanel(): void {
  panelOpen.value = false
}

function selectPanel(tab: PanelTab): void {
  panelTab.value = tab
  try {
    localStorage.setItem(PANEL_STORAGE_KEY, tab)
  } catch {
    /* 存不下就算了 */
  }
}

/**
 * 深链：`?lineage=` 打开草稿，`?workflow=` 打开只读版本，`?instance=` 直接落在一张工单上。
 *
 * 开页失败一律走页面横幅，不再弹 toast：toast 浮在同一个顶部居中位置，会把
 * 带"重试"的目录横幅整个盖住（真机实测：两句话叠成一团，按钮点不到）。
 */
async function bootstrap(): Promise<void> {
  const deepLink = readWorkflowDeepLink(route.query)

  const [, , instances] = await Promise.allSettled([
    catalog.loadNodeTypes(),
    catalog.syncDefinitionsQuietly('定义列表没加载'),
    run.loadInstances(),
  ])
  // 目录进 `catalog.nodeTypesError`、定义进 `catalog.error`，各自由横幅展示；
  // 实例是运行态，失败只在控制面留痕，不在画布上抢注意力。
  if (instances.status === 'rejected') console.warn('实例目录没加载', instances.reason)

  if (deepLink.lineage !== '') {
    if (await documentStore.openLineage(deepLink.lineage)) editor.setMode({ kind: 'draft' })
  } else if (deepLink.workflow !== '') {
    if (await documentStore.openVersion(deepLink.workflow)) {
      editor.setMode({ kind: 'version', workflowId: deepLink.workflow })
    }
  }
  if (deepLink.instance !== '') await run.focusInstance(deepLink.instance)

  urlReady = true
  syncUrl()
}

function currentUrlState(): WorkflowUrlState {
  return {
    lineageId: documentStore.lineageId,
    publishedWorkflowId: documentStore.publishedWorkflowId,
    mode: editor.mode,
    instance:
      run.instance === null ? null : { instanceId: run.instance.instance_id, workflowId: run.instance.workflow_id },
  }
}

let urlReady = false

function syncUrl(): void {
  const next = nextWorkflowQuery(route.query, desiredWorkflowQuery(currentUrlState()))
  if (!next.changed) return
  void router.replace({ query: next.query })
}

// 打开、发布、切换实例都会改这几样；地址栏跟着走，F5 才能回到同一张画布。
watch(
  () =>
    [documentStore.lineageId, documentStore.publishedWorkflowId, editor.mode, run.instance?.instance_id ?? ''] as const,
  () => {
    if (urlReady) syncUrl()
  },
  { deep: true },
)

async function saveDraft(): Promise<void> {
  if (await documentStore.flush()) message.success('草稿已保存')
  else if (documentStore.error !== null) message.error(documentStore.error)
}

async function publish(): Promise<void> {
  if (await documentStore.publish()) {
    const version = documentStore.publishedVersion
    message.success(version === null ? '版本已发布' : `已发布 v${version}`)
    return
  }
  if (documentStore.error !== null) message.error(documentStore.error)
}

function createDraft(): void {
  const proceed = (): void => {
    documentStore.resetDocument()
    editor.setMode({ kind: 'draft' })
    selectNode(null)
    editor.selectNote(null)
    message.success('已新建空白画布')
  }
  // 有未保存改动时先确认：一按就清空、只弹一句 toast 的话，重画的成本全在值班员身上
  if (documentStore.isDirty) confirmDiscardUnsaved('新建画布', proceed)
  else proceed()
}

/** 覆盖会用本地内容冲掉别的会话刚存的草稿，草稿没有历史可退——先问一句。 */
function overwriteDraft(): void {
  Modal.confirm({
    title: '用本地内容覆盖服务器草稿？',
    content: '另一会话已保存过这一版；覆盖会把它那一版冲掉，且无法从草稿历史撤回。',
    okText: '覆盖服务器',
    cancelText: '取消',
    onOk: () => void documentStore.overwriteDraft(),
  })
}

/** 重排之后把整条链路收进视野——不然重排完节点跑到视口外，看上去像链路被删了。 */
function autoLayoutAndFit(): void {
  documentStore.autoLayout()
  void fitToGraph()
}

let detachUnsavedGuard: (() => void) | null = null

/** 顶部胶囊只回答“存没存上”；完整句子（保存时刻、失败原因）在 title 里。 */
const saveIndicator = computed(() => {
  const state = documentStore.saveState
  if (state.kind === 'clean') return { tone: 'ok' as const, text: '已保存', title: documentStore.statusText }
  if (state.kind === 'dirty') return { tone: 'warn' as const, text: '未保存', title: documentStore.statusText }
  if (state.kind === 'saving') return { tone: 'idle' as const, text: '保存中…', title: documentStore.statusText }
  if (state.kind === 'conflict')
    return { tone: 'bad' as const, text: '草稿冲突', title: '草稿已被其他会话修改，自动保存已暂停' }
  return { tone: 'bad' as const, text: '保存失败', title: documentStore.statusText }
})

const waitingTickets = computed(() => [...run.instances].reverse().filter((row) => row.status === 'waiting'))

const conflict = computed(() => (documentStore.saveState.kind === 'conflict' ? documentStore.saveState : null))

/** 三处错误各自负责自己的请求；页面按出现顺序合成一条，不吞掉任何一处。 */
const surfaceErrors = computed<string[]>(() => {
  const rows = [documentStore.error, editor.error, catalog.error]
  return [...new Set(rows.filter((row): row is string => row !== null && row !== ''))]
})

function rename(next: string): void {
  documentStore.setMeta({ name: next })
}

/** 目录重试与首次加载走同一路径；成功后横幅自己消失，不再留一条陈旧错误。 */
async function retryNodeTypes(): Promise<void> {
  if (await catalog.retryNodeTypes()) message.success('节点目录已重新加载')
  else if (catalog.nodeTypesError !== null) message.error(catalog.nodeTypesError)
}

function handleNodeClick(event: NodeMouseEvent): void {
  onNodeClick(event)
  if (event.node.type !== 'note') revealPanel('node')
}

/** 空白处双击：打开块选择器，新节点落在光标处（Dify 画布的同一条动作）。 */
function onCanvasDoubleClick(event: MouseEvent): void {
  const target = event.target
  if (!(target instanceof HTMLElement)) return
  if (
    target.closest(
      '.vue-flow__node, .vue-flow__edge, .vue-flow__controls, .vue-flow__minimap, .vue-flow__panel, .ed-toolbar, .ed-panel, button, input, textarea, a',
    ) !== null
  )
    return
  request.value = { x: event.clientX, y: event.clientY, placement: 'cursor' }
}

function openAddAtCenter(event: MouseEvent): void {
  const rect = (event.currentTarget as HTMLElement).getBoundingClientRect()
  request.value = { x: rect.right, y: rect.bottom, placement: 'center' }
}

function openFirstBlock(): void {
  const rect = canvasRef.value?.getBoundingClientRect()
  if (rect === undefined) return
  request.value = { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2, placement: 'center' }
}

/** 屏幕锚点 → 新节点左上角（flow 坐标）：光标居中，工具栏/空态落在可视区中心。 */
function placementOf(next: InsertionRequest): Position {
  const rect = canvasRef.value?.getBoundingClientRect()
  const screenX = next.placement === 'center' && rect !== undefined ? rect.left + rect.width / 2 : next.x
  const screenY = next.placement === 'center' && rect !== undefined ? rect.top + rect.height / 2 : next.y
  const point = screenPointToFlow(screenX, screenY)
  return { x: point.x - NODE_FOOTPRINT.width / 2, y: point.y - NODE_FOOTPRINT.height / 2 }
}

/** `visibleFlowBox` 收的是点对点投影，画布给的是 (x, y) 两参数，这里补一层适配。 */
function project(point: Position): Position {
  return screenPointToFlow(point.x, point.y)
}

function addNoteAtCenter(): void {
  const rect = canvasRef.value?.getBoundingClientRect()
  if (rect === undefined) return
  /* 面板是覆盖式的：中心按“面板之外仍可见的那块”算，新备注才不会一出生就藏在面板底下。 */
  const usableWidth = panelOpen.value ? Math.max(rect.width - layoutTokens.editorPanelWidth, 240) : rect.width
  const point = screenPointToFlow(rect.left + usableWidth / 2, rect.top + rect.height / 2)
  /* 备注垫在所有执行节点下面：落点若压着节点，便签会整张被盖住、连拖都拖不出来。 */
  const taken = view.value.nodes.map((item) => ({ x: item.position.x, y: item.position.y }))
  const at = freeDropPosition(
    taken,
    { x: point.x - 110, y: point.y - 70 },
    24,
    visibleFlowBox(canvasRef.value, project),
    overlayFlowBoxes(canvasRef.value, project),
  )
  const noteId = documentStore.addNote({ x: Math.round(at.x), y: Math.round(at.y) })
  if (noteId !== null) editor.selectNote(noteId)
}

function selectBlock(type: NodeType): void {
  const def = documentStore.definition
  const next = request.value
  request.value = null
  if (def === null || next === null) return

  const node = createNodeDef(type, nextNodeId(def, type))
  if (next.edge !== undefined) {
    const source = view.value.nodes.find((item) => item.id === next.edge?.source)
    const target = view.value.nodes.find((item) => item.id === next.edge?.target)
    const at =
      source === undefined || target === undefined
        ? undefined
        : {
            x: Math.round((source.position.x + target.position.x) / 2),
            y: Math.round((source.position.y + target.position.y) / 2),
          }
    if (documentStore.insertNodeBetween(next.edge, node, at)) {
      selectNode(node.node_id)
      revealPanel('node')
      return
    }
  }

  const taken = view.value.nodes.map((item) => ({ x: item.position.x, y: item.position.y }))
  documentStore.addNode(
    node,
    freeDropPosition(
      taken,
      placementOf(next),
      24,
      visibleFlowBox(canvasRef.value, project),
      overlayFlowBoxes(canvasRef.value, project),
    ),
  )
  selectNode(node.node_id)
  revealPanel('node')
}

onMounted(() => {
  void bootstrap()
  // 队列要自己长出新单子：值班员盯着这一页等工单时，页面不能一直是开页那一刻的快照
  run.startQueuePolling()
  // 「服务端已存定义」那一列同理：助手或同事刚存的那一版，不该靠刷新整页才看得见
  catalog.startDefinitionPolling()
  // 自动保存还在防抖里、或保存失败时，F5 仍会丢内容：守卫照旧装着
  detachUnsavedGuard = installUnsavedGuard(() => documentStore.isDirty)
})
onUnmounted(() => {
  run.stopPolling()
  run.stopQueuePolling()
  catalog.stopDefinitionPolling()
  documentStore.dispose()
  detachUnsavedGuard?.()
  detachUnsavedGuard = null
})
</script>

<template>
  <div class="ed">
    <EditorHeader
      :name="documentStore.document?.name ?? '未命名防控链路'"
      :save-tone="saveIndicator.tone"
      :save-text="saveIndicator.text"
      :save-title="saveIndicator.title"
      :version="documentStore.publishedVersion"
      :waiting="waitingTickets.length"
      :polling="run.polling"
      :can-publish="documentStore.canSave"
      :busy="documentStore.loading"
      @rename="rename"
      @save="saveDraft"
      @publish="publish"
      @refresh="run.refreshAll"
      @new-draft="createDraft"
      @layout="autoLayoutAndFit"
      @fit="fitToGraph"
      @open-panel="openPanel"
    />

    <div class="ed-body" :class="{ 'has-panel': panelOpen }">
      <div
        ref="canvasRef"
        class="ed-canvas"
        :style="{ '--canvas-stroke-k': strokeScale }"
        data-testid="workflow-canvas"
        @drop="onDrop"
        @dragover="onDragOver"
        @dblclick="onCanvasDoubleClick"
        @mousedown="onCanvasMouseDown"
        @mousemove="onCanvasMouseMove"
      >
        <!-- 这里刻意不开 fit-view-on-init：它跑在"第一颗被测出尺寸的节点"上、且只跑一次，
             既会被手动拖入的第一颗节点吃掉（之后打开已存定义就不再 fit），又会按 maxZoom 把
             单颗节点放大到 2.0（真机量到可视区只剩 414×339 flow 单位，第二颗怎么放都出画布）。
             收拢视野改由 fitToGraph 显式做（见 useWorkflowCanvas），只在换定义时跑、且不许放大。 -->
        <VueFlow
          :nodes="view.nodes"
          :edges="view.edges"
          :node-types="WORKFLOW_NODE_TYPES"
          :edge-types="WORKFLOW_EDGE_TYPES"
          :edges-updatable="false"
          :delete-key-code="null"
          :selection-key-code="null"
          :elements-selectable="false"
          :pan-on-drag="editor.canvasMode === 'hand' ? true : [1, 2]"
          :nodes-draggable="editor.canvasMode === 'pointer'"
          :min-zoom="0.2"
          :max-zoom="2"
          :default-viewport="{ x: 40, y: 40, zoom: CANVAS_DEFAULT_ZOOM }"
          :default-edge-options="{ type: 'workflow', markerEnd: MarkerType.ArrowClosed }"
          @connect="onConnect"
          @node-click="handleNodeClick"
          @edge-click="onEdgeClick"
          @pane-click="onPaneClick"
          @node-drag-start="onNodeDragStart"
          @node-drag-stop="onNodeDragStop"
          @node-context-menu="openNodeMenu"
          @edge-context-menu="openEdgeMenu"
          @pane-context-menu="openPaneMenu"
        >
          <Background pattern-color="var(--aeg-color-editor-dot)" :gap="14" :size="2" />
          <!-- 空画布上不画小地图（里面什么都没有，只是块白框），但**得挂着**：
               落点让位要把它的真实尺寸当禁落区——第一颗节点贴着右下角落时它还没挂载，
               于是禁落区为空，节点落好后立刻被它盖住（真机量到 171×114px）。 -->
          <MiniMap
            :class="{ 'is-ghost': view.nodes.length === 0 }"
            node-color="var(--aeg-color-text-muted)"
            mask-color="color-mix(in srgb, var(--aeg-color-editor-canvas) 55%, transparent)"
          />
          <Controls />
        </VueFlow>

        <div
          v-if="marqueeBox !== null"
          class="ed-marquee"
          :style="{
            left: `${marqueeBox.left}px`,
            top: `${marqueeBox.top}px`,
            width: `${marqueeBox.width}px`,
            height: `${marqueeBox.height}px`,
          }"
        />

        <EditorToolbar
          class="ed-toolbar"
          :disabled="documentStore.definition === null"
          :mode="editor.canvasMode"
          @add="openAddAtCenter"
          @note="addNoteAtCenter"
          @layout="autoLayoutAndFit"
          @fit="fitToGraph"
          @mode="editor.setCanvasMode"
        />

        <div v-if="view.nodes.length === 0" class="ed-empty" data-testid="canvas-empty">
          <span class="ed-empty__icon" aria-hidden="true">◇</span>
          <h2 class="ed-empty__title">从第一步开始</h2>
          <p class="ed-empty__desc">添加一个节点，或打开一份已存定义；也可以双击空白处挑选节点。</p>
          <div class="ed-empty__actions">
            <button type="button" class="ed-empty__btn is-primary" @click="openFirstBlock">添加第一个节点</button>
            <button type="button" class="ed-empty__btn" @click="openPanel('def')">打开定义</button>
          </div>
        </div>

        <!-- 浮层横幅共用一条栈：多条同时出现时上下排开，不互相盖住（重试按钮不能被盖）。 -->
        <div class="ed-banners">
          <p v-if="waitingTickets.length > 0" class="ed-banner is-todo" data-testid="waiting-banner">
            <span>有 {{ waitingTickets.length }} 张工单在等人签。</span>
            <a-button size="small" type="primary" @click="run.openFirstWaiting">打开第一张</a-button>
          </p>

          <!-- 冲突是两个会话同时写草稿时唯一正确的停手点：自动保存已暂停，等人选保留哪一版 -->
          <p v-if="conflict !== null" class="ed-banner is-bad" data-testid="draft-conflict">
            <span>
              草稿已被其他会话修改（服务器 r{{ conflict.serverRevision }} ／ 本地 r{{
                conflict.localRevision
              }}），自动保存已暂停。
            </span>
            <a-button size="small" @click="documentStore.reloadDraft()">以服务器为准</a-button>
            <a-button size="small" danger @click="overwriteDraft">覆盖服务器</a-button>
          </p>

          <p v-if="surfaceErrors.length > 0" class="ed-banner is-bad">{{ surfaceErrors.join('；') }}</p>
          <p v-if="catalog.nodeTypesError !== null" class="ed-banner is-bad" data-testid="catalog-retry">
            <span>{{ catalog.nodeTypesError }}</span>
            <button type="button" class="ed-banner__retry" :disabled="catalog.nodeTypesLoading" @click="retryNodeTypes">
              重试
            </button>
          </p>
          <p v-else-if="catalog.hasDrift" class="ed-banner is-warn" data-testid="catalog-drift">
            节点目录与服务端不一致：{{ catalog.driftSummary }}
          </p>
          <p v-else-if="view.nodes.length > 0 && documentStore.validationErrors.length > 0" class="ed-banner is-warn">
            画布尚有 {{ documentStore.validationErrors.length }} 处待修正，详见「定义」面板的本地校验。
          </p>
        </div>
      </div>

      <NodePanel
        v-if="panelOpen"
        :panel="panelTab"
        :waiting="waitingTickets.length"
        :has-selection="editor.selectedNodeId !== null"
        @close="closePanel"
        @select="selectPanel"
      >
        <NodeInspector v-if="panelTab === 'node'" />
        <DefinitionInspector v-else-if="panelTab === 'def'" />
        <RuntimePanel v-else />
      </NodePanel>

      <BlockSelector v-if="request !== null" :request="request" @select="selectBlock" @close="request = null" />
    </div>

    <CanvasContextMenu
      v-if="menu !== null"
      :x="menu.x"
      :y="menu.y"
      :items="menu.items"
      @select="onMenuSelect"
      @close="menu = null"
    />
    <ShortcutHelp v-if="helpOpen" @close="helpOpen = false" />
    <NodeLocator v-if="locateOpen" :nodes="locatorNodes" @close="locateOpen = false" @pick="pickLocatedNode" />
  </div>
</template>

<style scoped>
.ed {
  display: flex;
  flex-direction: column;
  height: 100vh;
  background: var(--aeg-color-editor-canvas);
  color: var(--aeg-color-text);
}
.ed-body {
  position: relative;
  flex: 1;
  min-height: 0;
  overflow: hidden;
}
.ed-canvas {
  position: absolute;
  inset: 0;
  background: var(--aeg-color-editor-canvas);
}
.ed-toolbar {
  position: absolute;
  top: 12px;
  left: 12px;
  z-index: 6;
}
/* 框选矩形按客户端坐标 fixed 定位（鼠标事件给的就是视口坐标），不随画布平移缩放。 */
.ed-marquee {
  position: fixed;
  z-index: 5;
  border: 1px solid var(--aeg-color-brand);
  background: color-mix(in srgb, var(--aeg-color-brand) 12%, transparent);
  pointer-events: none;
}
.ed-empty {
  position: absolute;
  top: 50%;
  left: 50%;
  z-index: 4;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  width: min(380px, calc(100% - 48px));
  margin: 0;
  transform: translate(-50%, -50%);
  text-align: center;
  pointer-events: none;
}
.ed-empty__icon {
  display: grid;
  place-items: center;
  width: 44px;
  height: 44px;
  border: 1px dashed var(--aeg-color-brand-line);
  border-radius: 14px;
  background: var(--aeg-color-brand-soft);
  color: var(--aeg-color-brand);
  font-size: 20px;
}
.ed-empty__title {
  margin: 0;
  font-size: 16px;
  font-weight: 700;
}
.ed-empty__desc {
  margin: 0;
  color: var(--aeg-color-text-secondary);
  font-size: 12px;
  line-height: 1.7;
}
.ed-empty__actions {
  display: flex;
  gap: 8px;
  margin-top: 4px;
  pointer-events: all;
}
.ed-empty__btn {
  height: 32px;
  padding: 0 14px;
  border: 1px solid var(--aeg-color-border);
  border-radius: 8px;
  background: var(--aeg-color-editor-panel);
  color: var(--aeg-color-text-secondary);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}
.ed-empty__btn.is-primary {
  border-color: transparent;
  background: var(--aeg-color-brand);
  color: var(--aeg-color-text-inverse);
  font-weight: 600;
}
.ed-empty__btn:hover {
  border-color: var(--aeg-color-brand-line);
  color: var(--aeg-color-text);
}
.ed-empty__btn.is-primary:hover {
  background: var(--aeg-color-brand-hover);
  color: var(--aeg-color-text-inverse);
}
.ed-banners {
  position: absolute;
  top: 12px;
  left: 50%;
  z-index: 7;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  max-width: calc(100% - 320px);
  transform: translateX(-50%);
  pointer-events: none;
}
.ed-banner {
  display: flex;
  align-items: center;
  gap: 10px;
  max-width: 100%;
  margin: 0;
  padding: 6px 12px;
  border: 1px solid var(--aeg-color-border);
  border-radius: 10px;
  background: var(--aeg-color-editor-panel);
  box-shadow: var(--aeg-shadow-md);
  color: var(--aeg-color-text-secondary);
  font-size: 12px;
  pointer-events: all;
}
.ed-banner.is-todo {
  border-color: var(--aeg-color-brand-line);
  color: var(--aeg-color-brand-hover);
}
.ed-banner.is-bad {
  border-color: var(--aeg-tag-red-line);
  background: var(--aeg-tag-red-bg);
  color: var(--aeg-tag-red-text);
}
.ed-banner.is-warn {
  border-color: var(--aeg-tag-orange-line);
  background: var(--aeg-tag-orange-bg);
  color: var(--aeg-tag-orange-text);
}
.ed-banner__retry {
  padding: 0;
  border: none;
  background: transparent;
  color: inherit;
  font: inherit;
  font-weight: 600;
  text-decoration: underline;
  cursor: pointer;
}
.ed-banner__retry:disabled {
  opacity: 0.55;
  cursor: progress;
}
/* 浮层的样子（描边/圆角/投影/控件按钮）在 `theme.css` 的 `.ed-canvas` 那一段，这里只管站位：
   面板是覆盖式的，小地图要让开那 400px，否则节点会被它盖住。 */
.ed-body.has-panel :deep(.vue-flow__minimap) {
  right: calc(var(--aeg-editor-panel-width, 400px) + 12px);
}
.ed-canvas :deep(.vue-flow__minimap) {
  right: 12px;
  bottom: 12px;
}
.ed-canvas :deep(.vue-flow__controls) {
  left: 12px;
  bottom: 12px;
}
@media (max-width: 900px) {
  .ed-banners {
    top: 56px;
    max-width: calc(100% - 24px);
  }
}
</style>
