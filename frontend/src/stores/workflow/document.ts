/**
 * 文档层：草稿、保存状态、自动保存、发布与版本恢复。
 *
 * 服务器草稿是真源，本地只持有"当前画布的一份快照 + 基线哈希"：
 * 脏 = 快照与基线不同；自动保存 = 800ms 防抖后带 base_revision 写回。
 * 409 不覆盖、不重试，直接停在冲突态等人决定。
 */
import { useDebounceFn } from '@vueuse/core'
import { defineStore } from 'pinia'
import { computed, ref, shallowRef, watch } from 'vue'

import {
  WorkflowError,
  type WorkflowErrorCode,
  type WorkflowGateway,
  type WorkflowVersionSummary,
} from '@/api/workflow/contract'
import { workflowApi } from '@/api/workflow'
import {
  edgeId,
  layerPositions,
  patchNode as patchNodeDef,
  removeEdge as removeEdgeFromDef,
  removeNodes as removeNodesFromDef,
  setEdgeCondition as setEdgeConditionInDef,
  upsertEdge,
  validateGraph,
  type EdgeDef,
  type JsonValue,
  type NodeDef,
  type NodePatch,
  type Position,
  type WorkflowDef,
} from '@/utils/graph'
import {
  autoLayoutDocument,
  canonicalDocumentJson,
  emptyLayout,
  ensureDocumentLayout,
  definitionFromDocument,
  documentsEqual,
  MAX_STICKY_NOTES,
  prunePositions,
  validateDocument,
  type StickyNote,
  type WorkflowDocument,
  type WorkflowDraft,
} from '@/utils/graph/document'
import {
  canRedoHistory,
  canUndoHistory,
  cloneWorkflowDocument,
  commitHistory,
  createHistoryState,
  HISTORY_LABELS,
  redoHistory,
  redoHistoryLabel,
  undoHistory,
  undoHistoryLabel,
  type WorkflowHistoryState,
} from '@/utils/graph/history'

import { describeFailure, isAbortError, nowIso } from './shared'

const AUTOSAVE_DELAY_MS = 800
const RETRY_DELAYS_MS = [1_000, 2_000, 4_000]
const DEFAULT_NAME = '未命名防控链路'
const PLACEHOLDER_LINEAGE = 'wf_000000000000'

export type SaveState =
  | { kind: 'clean'; savedAt: string }
  | { kind: 'dirty'; since: string }
  | { kind: 'saving'; startedAt: string }
  | { kind: 'error'; code: WorkflowErrorCode; retryable: boolean }
  | { kind: 'conflict'; serverRevision: number; localRevision: number }

/**
 * 进实例视图前把草稿整份收起来。
 *
 * 实例画布要换成它自己绑定的那一版（含运行中插入的节点），而草稿可能还有没保存的改动；
 * 不收起来就只能二选一：要么冲掉草稿，要么不给实例画布。返回草稿时从这里原样放回，
 * 不再打一趟接口——草稿的真源如果比本地旧，人刚改的那几笔就凭空没了。
 */
interface DraftStash {
  document: WorkflowDocument
  baseline: string
  revision: number
  baseWorkflowId: string | null
  versions: WorkflowVersionSummary[]
  saveState: SaveState
  error: string | null
  history: WorkflowHistoryState | null
}

function saveStateLabel(state: SaveState): string {
  if (state.kind === 'clean') {
    const time = new Date(state.savedAt)
    return `草稿已自动保存 ${Number.isNaN(time.getTime()) ? '--:--:--' : time.toLocaleTimeString('zh-CN', { hour12: false })}`
  }
  if (state.kind === 'dirty') return '有未发布的更改'
  if (state.kind === 'saving') return '正在保存草稿…'
  if (state.kind === 'conflict') return '草稿已被其他会话修改'
  return '草稿保存失败'
}

function blankDocument(name: string): WorkflowDocument {
  return {
    schema_version: 1,
    // 本地空白画布还没有 lineage；首次保存由 createLineage 分配后再补上。
    lineage_id: '',
    name,
    description: '',
    nodes: [],
    edges: [],
    layout: emptyLayout(),
  }
}

/** 结构校验用占位 lineage：空白草稿的 lineage 由服务端分配，不该因此挡住节点编辑。 */
function structuralErrorsOf(document: WorkflowDocument): string[] {
  const candidate = document.lineage_id === '' ? { ...document, lineage_id: PLACEHOLDER_LINEAGE } : document
  return validateDocument(candidate)
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms))
}

export function createDocumentStore(gateway: WorkflowGateway = workflowApi) {
  return defineStore('workflow.document', () => {
    const initialDocument = blankDocument(DEFAULT_NAME)
    const document = ref<WorkflowDocument | null>(initialDocument)
    /** 每换一份文档加一；编辑只替换文档内容，不加这个数——画布据此决定要不要收拢视野。 */
    const documentEpoch = ref(0)
    const revision = ref(0)
    const baseWorkflowId = ref<string | null>(null)
    const versions = ref<WorkflowVersionSummary[]>([])
    const saveState = ref<SaveState>({ kind: 'clean', savedAt: nowIso() })
    const error = ref<string | null>(null)
    const loading = ref(false)
    const autoSaveEnabled = ref(true)
    const draftStash = ref<DraftStash | null>(null)

    /**
     * 基线必须是响应式的：`isDirty` 是 computed，只改普通变量不会让它失效，
     * 于是"保存成功后仍显示有未发布的更改"，直到下一次编辑才重新计算。
     */
    const baseline = ref(canonicalDocumentJson(initialDocument))
    /** 撤销栈只活在本地；换文档时整栈重开，不跨会话复用。 */
    const historyState = shallowRef<WorkflowHistoryState | null>(createHistoryState(initialDocument))
    let suppressWatch = false
    let suppressHistory = false
    let skipNextHistory = false
    let pendingHistoryLabel: string | null = null
    let loadToken = 0
    let saveToken = 0
    let inFlight: Promise<boolean> | null = null
    let inFlightController: AbortController | null = null
    let saveQueued = false

    const definition = computed<WorkflowDef | null>(() =>
      document.value === null ? null : definitionFromDocument(document.value),
    )

    const lineageId = computed<string>(() => document.value?.lineage_id ?? '')

    const isDirty = computed<boolean>(
      () => document.value !== null && canonicalDocumentJson(document.value) !== baseline.value,
    )

    const structuralErrors = computed<string[]>(() =>
      document.value === null ? ['画布尚未初始化'] : structuralErrorsOf(document.value),
    )

    const definitionErrors = computed<string[]>(() =>
      definition.value === null ? [] : validateGraph(definition.value),
    )

    const validationErrors = computed<string[]>(() =>
      structuralErrors.value.length > 0 ? structuralErrors.value : definitionErrors.value,
    )

    const canSave = computed<boolean>(
      () => document.value !== null && structuralErrors.value.length === 0 && definitionErrors.value.length === 0,
    )

    const canAutosave = computed<boolean>(
      () =>
        autoSaveEnabled.value &&
        document.value !== null &&
        structuralErrors.value.length === 0 &&
        document.value.name.trim().length > 0,
    )

    const publishedWorkflowId = computed<string | null>(
      () =>
        versions.value.find((row) => row.is_current)?.workflow_id ??
        versions.value[0]?.workflow_id ??
        baseWorkflowId.value,
    )

    const publishedVersion = computed<number | null>(
      () => versions.value.find((row) => row.is_current)?.version ?? versions.value[0]?.version ?? null,
    )

    const statusText = computed<string>(() => saveStateLabel(saveState.value))

    /** 只读版本/实例视图照常渲染，但不允许撤销把内容写回。 */
    const canUndo = computed<boolean>(() => autoSaveEnabled.value && canUndoHistory(historyState.value))
    const canRedo = computed<boolean>(() => autoSaveEnabled.value && canRedoHistory(historyState.value))
    const undoLabel = computed<string | null>(() => undoHistoryLabel(historyState.value))
    const redoLabel = computed<string | null>(() => redoHistoryLabel(historyState.value))

    const debouncedSave = useDebounceFn(() => saveNow('auto'), AUTOSAVE_DELAY_MS)

    watch(
      document,
      () => {
        if (suppressWatch) return
        if (document.value === null) return
        if (!suppressHistory && !skipNextHistory) {
          const label = pendingHistoryLabel ?? HISTORY_LABELS.updateNode
          historyState.value =
            historyState.value === null
              ? createHistoryState(document.value, label)
              : commitHistory(historyState.value, document.value, label)
        }
        skipNextHistory = false
        pendingHistoryLabel = null
        if (saveState.value.kind === 'conflict') return
        if (!autoSaveEnabled.value) {
          saveState.value = { kind: 'dirty', since: nowIso() }
          return
        }
        if (!isDirty.value) return
        saveState.value = { kind: 'dirty', since: nowIso() }
        if (canAutosave.value) void debouncedSave()
      },
      { flush: 'sync' },
    )

    function cancelScheduledSave(): void {
      debouncedSave.cancel()
    }

    function abortInFlight(): void {
      inFlightController?.abort()
      inFlightController = null
    }

    function applyDocument(next: WorkflowDocument): void {
      suppressWatch = true
      suppressHistory = false
      skipNextHistory = false
      pendingHistoryLabel = null
      document.value = next
      baseline.value = canonicalDocumentJson(next)
      documentEpoch.value += 1
      historyState.value = createHistoryState(next)
      suppressWatch = false
    }

    function applyServerDraft(draft: WorkflowDraft): void {
      const laidOut = ensureDocumentLayout(draft.document)
      revision.value = draft.revision
      baseWorkflowId.value = draft.base_workflow_id
      applyDocument(laidOut)
      saveState.value = { kind: 'clean', savedAt: draft.updated_at }
      error.value = null
    }

    function setMeta(meta: { name?: string; description?: string }): void {
      const current = document.value
      if (current === null) return
      pendingHistoryLabel = HISTORY_LABELS.rename
      document.value = {
        ...current,
        ...(meta.name === undefined ? {} : { name: meta.name }),
        ...(meta.description === undefined ? {} : { description: meta.description }),
      }
    }

    function seedPositions(next: Readonly<Record<string, Position>>): void {
      const current = document.value
      if (current === null) return
      let changed = false
      const positions = { ...current.layout.positions }
      for (const [nodeId, position] of Object.entries(next)) {
        if (positions[nodeId] === undefined) {
          positions[nodeId] = { x: position.x, y: position.y }
          changed = true
        }
      }
      if (!changed) return
      /* 拖动开始时固化自动布局坐标只是把"屏幕上的位置"写进文档，不构成一步可撤销的修改。 */
      skipNextHistory = true
      document.value = { ...current, layout: { ...current.layout, positions } }
    }

    /** at 为拖放落点（画布坐标）；省略时继续用视图的自动分层坐标。 */
    function addNode(node: NodeDef, at?: Position): void {
      const current = document.value
      if (current === null) return
      const positions = { ...current.layout.positions }
      if (at !== undefined) {
        if (Object.keys(positions).length === 0 && current.nodes.length > 0) {
          Object.assign(
            positions,
            layerPositions(
              current.nodes.map((item) => item.node_id),
              current.edges,
            ),
          )
        }
        positions[node.node_id] = { x: at.x, y: at.y }
      }
      pendingHistoryLabel = HISTORY_LABELS.addNode
      document.value = {
        ...current,
        nodes: [...current.nodes, node],
        layout: { ...current.layout, positions },
      }
    }

    /** 粘贴/复制副本落地：节点、内部边与位置一次性写入，历史里只留一步。 */
    function insertSubgraph(
      nodes: readonly NodeDef[],
      edges: readonly EdgeDef[],
      positions: Readonly<Record<string, Position>>,
    ): boolean {
      const current = document.value
      if (current === null || nodes.length === 0) return false
      const nextPositions = { ...current.layout.positions }
      for (const [nodeId, position] of Object.entries(positions)) {
        nextPositions[nodeId] = { x: position.x, y: position.y }
      }
      pendingHistoryLabel = HISTORY_LABELS.paste
      document.value = {
        ...current,
        nodes: [...current.nodes, ...nodes],
        edges: [...current.edges, ...edges],
        layout: { ...current.layout, positions: nextPositions },
      }
      return true
    }

    function moveNodes(positions: Readonly<Record<string, Position>>): void {
      const current = document.value
      if (current === null) return
      let changed = false
      const next = { ...current.layout.positions }
      for (const [nodeId, position] of Object.entries(positions)) {
        const previous = next[nodeId]
        if (previous !== undefined && previous.x === position.x && previous.y === position.y) continue
        next[nodeId] = { x: position.x, y: position.y }
        changed = true
      }
      if (!changed) return
      pendingHistoryLabel = HISTORY_LABELS.moveNodes
      document.value = { ...current, layout: { ...current.layout, positions: next } }
    }

    function moveNode(nodeId: string, position: Position): void {
      const current = document.value
      if (current === null) return
      pendingHistoryLabel = HISTORY_LABELS.moveNodes
      document.value = {
        ...current,
        layout: {
          ...current.layout,
          positions: { ...current.layout.positions, [nodeId]: { x: position.x, y: position.y } },
        },
      }
    }

    /** 备注只活在版式里：不进 WorkflowDef，因此不改变引擎眼中的图身份。 */
    function addNote(at?: Position): string | null {
      const current = document.value
      if (current === null || current.layout.notes.length >= MAX_STICKY_NOTES) return null
      const taken = new Set(current.layout.notes.map((note) => note.note_id))
      let index = current.layout.notes.length + 1
      while (taken.has(`note_${index}`)) index += 1
      const note: StickyNote = {
        note_id: `note_${index}`,
        x: at?.x ?? 0,
        y: at?.y ?? 0,
        width: 220,
        height: 140,
        text: '',
        color: 'amber',
      }
      pendingHistoryLabel = HISTORY_LABELS.setNotes
      document.value = { ...current, layout: { ...current.layout, notes: [...current.layout.notes, note] } }
      return note.note_id
    }

    type NotePatch = Partial<Pick<StickyNote, 'x' | 'y' | 'width' | 'height' | 'text' | 'color'>>

    function updateNote(noteId: string, patch: NotePatch): void {
      const current = document.value
      if (current === null) return
      const index = current.layout.notes.findIndex((note) => note.note_id === noteId)
      if (index < 0) return
      const notes = current.layout.notes.slice()
      notes[index] = { ...notes[index], ...patch } as StickyNote
      pendingHistoryLabel = HISTORY_LABELS.setNotes
      document.value = { ...current, layout: { ...current.layout, notes } }
    }

    function removeNote(noteId: string): void {
      const current = document.value
      if (current === null) return
      pendingHistoryLabel = HISTORY_LABELS.setNotes
      document.value = {
        ...current,
        layout: { ...current.layout, notes: current.layout.notes.filter((note) => note.note_id !== noteId) },
      }
    }

    function autoLayout(): void {
      const current = document.value
      if (current === null) return
      const next = autoLayoutDocument(current)
      if (next === current) return
      pendingHistoryLabel = HISTORY_LABELS.moveNodes
      document.value = next
    }

    function applyGraph(next: WorkflowDef): void {
      const current = document.value
      if (current === null) return
      const ids = new Set(next.nodes.map((node) => node.node_id))
      document.value = {
        ...current,
        nodes: next.nodes,
        edges: next.edges,
        layout: { ...current.layout, positions: prunePositions(current.layout.positions, ids) },
      }
    }

    function patchNode(nodeId: string, patch: NodePatch): void {
      const current = definition.value
      if (current === null) return
      pendingHistoryLabel = HISTORY_LABELS.updateNode
      applyGraph(patchNodeDef(current, nodeId, patch))
    }

    /** 返回 false 表示被本地规则（自环/重复边）挡下。 */
    function connect(source: string, target: string): boolean {
      const current = definition.value
      if (current === null || source === target) return false
      const next = upsertEdge(current, source, target, '')
      if (next === current) return false
      pendingHistoryLabel = HISTORY_LABELS.connect
      applyGraph(next)
      return true
    }

    /**
     * 边中点插入：把 source→target 拆成 source→新节点→target，一次替换整张图。
     * 条件留在进入新节点的那一段（分支语义仍作用在原上游），新节点出边无条件；
     * join/branch 这类声明了 upstream 的配置同步改指新节点，避免“连了线但 upstream 还是旧的”。
     */
    function insertNodeBetween(edge: { source: string; target: string }, node: NodeDef, at?: Position): boolean {
      const current = document.value
      if (current === null) return false
      const index = current.edges.findIndex((item) => item.source === edge.source && item.target === edge.target)
      const original = current.edges[index]
      if (original === undefined) return false
      const edges = current.edges.filter((_, position) => position !== index)
      edges.splice(
        index,
        0,
        { source: original.source, target: node.node_id, condition: original.condition },
        { source: node.node_id, target: original.target, condition: '' },
      )
      const remapUpstream = (config: Record<string, JsonValue>, source: string): Record<string, JsonValue> => {
        if (!Array.isArray(config.upstream)) {
          return config.upstream === source ? { ...config, upstream: node.node_id } : config
        }
        return {
          ...config,
          upstream: config.upstream.map((entry) => (entry === source ? node.node_id : entry)),
        }
      }
      const inserted: NodeDef =
        'upstream' in node.config
          ? {
              ...node,
              config: {
                ...node.config,
                upstream: Array.isArray(node.config.upstream) ? [edge.source] : edge.source,
              },
            }
          : node
      const nodes = current.nodes.map((item) =>
        item.node_id === edge.target && 'upstream' in item.config
          ? { ...item, config: remapUpstream(item.config, edge.source) }
          : item,
      )
      const positions =
        at === undefined ? current.layout.positions : { ...current.layout.positions, [node.node_id]: at }
      pendingHistoryLabel = HISTORY_LABELS.insertOnEdge
      document.value = {
        ...current,
        nodes: [...nodes, inserted],
        edges,
        layout: { ...current.layout, positions },
      }
      return true
    }

    function updateEdgeCondition(id: string, condition: string): void {
      const current = definition.value
      if (current === null) return
      if (!current.edges.some((edge, index) => edgeId(index, edge) === id)) return
      const next = setEdgeConditionInDef(current, id, condition)
      pendingHistoryLabel = HISTORY_LABELS.updateNode
      applyGraph(next)
    }

    function removeEdge(id: string): void {
      const current = definition.value
      if (current === null) return
      const next = removeEdgeFromDef(current, id)
      if (next.edges.length === current.edges.length) return
      pendingHistoryLabel = HISTORY_LABELS.removeEdges
      applyGraph(next)
    }

    function removeNodes(nodeIds: Iterable<string>): void {
      const current = definition.value
      if (current === null) return
      const next = removeNodesFromDef(current, nodeIds)
      if (next.nodes.length === current.nodes.length) return
      pendingHistoryLabel = HISTORY_LABELS.removeNodes
      applyGraph(next)
    }

    /** Delete/Backspace 一次删掉选中的节点与连线；只写一次文档，历史里也是一步。 */
    function removeSelection(nodeIds: Iterable<string>, edgeIds: Iterable<string>): boolean {
      const current = definition.value
      if (current === null) return false
      const doomedNodes = new Set(nodeIds)
      const doomedEdges = new Set(edgeIds)
      const edges = current.edges.filter(
        (edge, index) =>
          !doomedEdges.has(edgeId(index, edge)) && !doomedNodes.has(edge.source) && !doomedNodes.has(edge.target),
      )
      const nodes = current.nodes.filter((node) => !doomedNodes.has(node.node_id))
      if (nodes.length === current.nodes.length && edges.length === current.edges.length) return false
      pendingHistoryLabel = HISTORY_LABELS.removeNodes
      applyGraph({ ...current, nodes, edges })
      return true
    }

    function syncUpstreamConfig(nodeId: string): void {
      const current = definition.value
      if (current === null) return
      const node = current.nodes.find((item) => item.node_id === nodeId)
      if (node === undefined || !('upstream' in node.config)) return
      const sources = current.edges.filter((edge) => edge.target === nodeId).map((edge) => edge.source)
      const asList = Array.isArray(node.config.upstream)
      patchNode(nodeId, { config: { ...node.config, upstream: asList ? sources : (sources[0] ?? '') } })
    }

    /**
     * 撤销目标直接换掉文档内容，但**不加 documentEpoch**：换 epoch 会让画布重新收拢视野，
     * 而撤销一步只该把节点挪回原处，不该顺手把视口也改了。
     */
    function writeHistoryDocument(next: WorkflowDocument): void {
      suppressHistory = true
      document.value = cloneWorkflowDocument(next)
      suppressHistory = false
    }

    function undo(): boolean {
      if (!autoSaveEnabled.value) return false
      const state = historyState.value
      if (state === null || !canUndoHistory(state)) return false
      const next = undoHistory(state)
      if (next === state) return false
      writeHistoryDocument(next.present.document)
      historyState.value = next
      return true
    }

    function redo(): boolean {
      if (!autoSaveEnabled.value) return false
      const state = historyState.value
      if (state === null || !canRedoHistory(state)) return false
      const next = redoHistory(state)
      if (next === state) return false
      writeHistoryDocument(next.present.document)
      historyState.value = next
      return true
    }

    /** lineage 由服务端补发时文档变了，但这不是用户的一步：把当前快照重钉到新文档上。 */
    function rebaseHistory(next: WorkflowDocument): void {
      const state = historyState.value
      if (state === null) return
      historyState.value = {
        ...state,
        present: { document: cloneWorkflowDocument(next), label: state.present.label },
      }
    }

    function resetDocument(name = DEFAULT_NAME): void {
      cancelScheduledSave()
      abortInFlight()
      clearDraftStash()
      loadToken += 1
      saveToken += 1
      revision.value = 0
      baseWorkflowId.value = null
      versions.value = []
      autoSaveEnabled.value = true
      applyDocument(blankDocument(name))
      saveState.value = { kind: 'clean', savedAt: nowIso() }
      error.value = null
    }

    async function refreshVersions(): Promise<void> {
      const lineage = lineageId.value
      if (lineage === '') {
        versions.value = []
        return
      }
      try {
        versions.value = await gateway.listVersions(lineage)
      } catch (caught) {
        // 还没有任何发布版本时 listVersions 回 404；这不是"打开失败"。
        if (caught instanceof WorkflowError && caught.code === 'not_found') versions.value = []
        else throw caught
      }
    }

    async function runLoad(label: string, action: () => Promise<void>): Promise<boolean> {
      loading.value = true
      error.value = null
      try {
        await action()
        return true
      } catch (caught) {
        error.value = `${label}：${describeFailure(caught)}`
        return false
      } finally {
        loading.value = false
      }
    }

    async function openLineage(lineage: string): Promise<boolean> {
      return runLoad('打开定义失败', async () => {
        clearDraftStash()
        const token = ++loadToken
        cancelScheduledSave()
        abortInFlight()
        const [draft, rows] = await Promise.all([
          gateway.getDraft(lineage),
          gateway.listVersions(lineage).catch((caught: unknown) => {
            if (caught instanceof WorkflowError && caught.code === 'not_found') return []
            throw caught
          }),
        ])
        if (token !== loadToken) return
        autoSaveEnabled.value = true
        applyServerDraft(draft)
        versions.value = rows
      })
    }

    async function openVersion(workflowId: string): Promise<boolean> {
      return runLoad('打开版本失败', async () => {
        clearDraftStash()
        const token = ++loadToken
        cancelScheduledSave()
        abortInFlight()
        const doc = await gateway.getVersionDocument(workflowId)
        if (token !== loadToken) return
        // 版本/实例是只读视图：编辑照常渲染，但不会把改动写进这条 lineage 的草稿。
        autoSaveEnabled.value = false
        revision.value = 0
        baseWorkflowId.value = workflowId
        applyDocument(ensureDocumentLayout(doc))
        saveState.value = { kind: 'clean', savedAt: nowIso() }
        await refreshVersions()
      })
    }

    /** 实例详情已经带着它绑定的版本文档，直接放进画布，不再多打一趟接口。 */
    function bindInstanceDocument(doc: WorkflowDocument): void {
      // 进实例前收起草稿：实例画布换的是另一张图，而草稿可能还有没保存的改动。
      stashDraft()
      cancelScheduledSave()
      abortInFlight()
      loadToken += 1
      autoSaveEnabled.value = false
      revision.value = 0
      baseWorkflowId.value = null
      applyDocument(ensureDocumentLayout(doc))
      saveState.value = { kind: 'clean', savedAt: nowIso() }
      error.value = null
    }

    /** 收起当前草稿（只收一份；连续开多条实例时保留最早那份，才是人真正要回来的地方）。 */
    function stashDraft(): boolean {
      const current = document.value
      if (current === null) return false
      if (draftStash.value !== null) return true
      if (!autoSaveEnabled.value) return false
      draftStash.value = {
        document: cloneWorkflowDocument(current),
        baseline: baseline.value,
        revision: revision.value,
        baseWorkflowId: baseWorkflowId.value,
        versions: [...versions.value],
        saveState: saveState.value,
        error: error.value,
        history: historyState.value,
      }
      return true
    }

    /** 返回草稿：原样放回收起的那份，不重新拉取——服务器草稿可能比本地旧。 */
    function restoreDraft(): boolean {
      const stash = draftStash.value
      if (stash === null) return false
      draftStash.value = null
      autoSaveEnabled.value = true
      revision.value = stash.revision
      baseWorkflowId.value = stash.baseWorkflowId
      versions.value = stash.versions
      applyDocument(stash.document)
      // applyDocument 会把基线打在当前内容上；草稿原本是否"脏"要按收起时的基线算。
      baseline.value = stash.baseline
      historyState.value = stash.history
      saveState.value = stash.saveState.kind === 'saving' ? { kind: 'dirty', since: nowIso() } : stash.saveState
      error.value = stash.error
      if (isDirty.value && canAutosave.value) void debouncedSave()
      return true
    }

    function clearDraftStash(): void {
      draftStash.value = null
    }

    /**
     * 实例运行中插节点后把画布上的图换成新快照。
     *
     * 不换 epoch：插入只该多出一颗节点，不该顺手把视口收拢到别处。
     * 只在只读实例视图里生效——草稿画布绝不能因为一条实例的图变了而被替换。
     */
    function syncInstanceDocument(next: WorkflowDocument): boolean {
      const current = document.value
      if (current === null || autoSaveEnabled.value) return false
      const laidOut = ensureDocumentLayout(next)
      if (documentsEqual(current, laidOut)) return false
      suppressWatch = true
      document.value = laidOut
      suppressWatch = false
      return true
    }

    async function createRemoteLineage(): Promise<string> {
      const current = document.value
      if (current === null) throw new Error('画布尚未初始化')
      const name = current.name.trim() === '' ? DEFAULT_NAME : current.name.trim()
      const draft = await gateway.createLineage(name)
      revision.value = draft.revision
      baseWorkflowId.value = draft.base_workflow_id
      // createLineage 分配 lineage；本地快照要跟上，PUT 才过得了"路径与文档一致"的校验。
      if (document.value !== null && document.value.lineage_id !== draft.lineage_id) {
        suppressWatch = true
        document.value = { ...document.value, lineage_id: draft.lineage_id }
        suppressWatch = false
        rebaseHistory(document.value)
      }
      return draft.lineage_id
    }

    async function putDraftWithRetry(
      lineage: string,
      payload: WorkflowDocument,
      baseRevision: number,
      signal: AbortSignal,
    ): Promise<WorkflowDraft> {
      let attempt = 0
      for (;;) {
        try {
          return await gateway.putDraft(lineage, { base_revision: baseRevision, document: payload }, signal)
        } catch (caught) {
          const retryable = caught instanceof WorkflowError && caught.code === 'network_error'
          if (!retryable || attempt >= RETRY_DELAYS_MS.length || signal.aborted) throw caught
          await sleep(RETRY_DELAYS_MS[attempt] ?? 4_000)
          attempt += 1
          if (signal.aborted) throw new WorkflowError('aborted', 0, '请求已取消')
        }
      }
    }

    async function doSave(): Promise<boolean> {
      const current = document.value
      if (current === null || !autoSaveEnabled.value) return false
      if (saveState.value.kind === 'conflict') return false
      // 改回原样（或空白画布）时不制造一趟空保存，也不凭空建一条 lineage。
      if (!isDirty.value) {
        if (revision.value > 0 && saveState.value.kind !== 'clean') {
          saveState.value = { kind: 'clean', savedAt: nowIso() }
        }
        return true
      }
      if (!canAutosave.value) return false

      cancelScheduledSave()
      const token = ++saveToken
      const loadAtStart = loadToken
      const controller = new AbortController()
      inFlightController = controller
      saveState.value = { kind: 'saving', startedAt: nowIso() }

      try {
        let lineage = lineageId.value
        if (lineage === '') lineage = await createRemoteLineage()
        if (token !== saveToken || loadAtStart !== loadToken) return false

        const sent = document.value
        if (sent === null) return false
        const sentCanonical = canonicalDocumentJson(sent)
        const draft = await putDraftWithRetry(lineage, sent, revision.value, controller.signal)
        if (token !== saveToken || loadAtStart !== loadToken) return false

        revision.value = draft.revision
        baseWorkflowId.value = draft.base_workflow_id
        error.value = null
        if (document.value !== null && canonicalDocumentJson(document.value) === sentCanonical) {
          baseline.value = sentCanonical
          saveState.value = { kind: 'clean', savedAt: draft.updated_at }
          return true
        }
        // 保存在路上时又改了：这一趟只证明"前面那版已落盘"，剩下的继续排下一趟。
        saveState.value = { kind: 'dirty', since: nowIso() }
        if (autoSaveEnabled.value) void debouncedSave()
        return true
      } catch (caught) {
        if (isAbortError(caught)) return false
        if (caught instanceof WorkflowError && caught.code === 'revision_conflict') {
          const raw = caught.details.server_revision
          saveState.value = {
            kind: 'conflict',
            serverRevision: typeof raw === 'number' ? raw : revision.value,
            localRevision: revision.value,
          }
          error.value = caught.message
          cancelScheduledSave()
          return false
        }
        const code: WorkflowErrorCode = caught instanceof WorkflowError ? caught.code : 'internal'
        saveState.value = { kind: 'error', code, retryable: code === 'network_error' }
        error.value = `草稿保存失败：${describeFailure(caught)}`
        return false
      } finally {
        if (inFlightController === controller) inFlightController = null
      }
    }

    async function saveNow(_reason: 'auto' | 'manual' = 'auto'): Promise<boolean> {
      if (inFlight !== null) {
        saveQueued = true
        await inFlight
        if (saveQueued) {
          saveQueued = false
          return saveNow(_reason)
        }
        return !isDirty.value
      }
      inFlight = doSave()
      try {
        return await inFlight
      } finally {
        inFlight = null
      }
    }

    function flush(): Promise<boolean> {
      debouncedSave.cancel()
      return saveNow('manual')
    }

    async function publish(): Promise<boolean> {
      const current = document.value
      if (current === null || !autoSaveEnabled.value) {
        error.value = '当前是只读版本视图，先恢复为草稿再发布'
        return false
      }
      if (!canSave.value) {
        error.value = validationErrors.value.join('；')
        return false
      }

      loading.value = true
      error.value = null
      try {
        const saved = await flush()
        if (!saved) return false
        const lineage = lineageId.value
        if (lineage === '') {
          error.value = '草稿尚未建立 lineage，无法发布'
          return false
        }
        await gateway.publish(lineage, revision.value)
        const [draft, rows] = await Promise.all([gateway.getDraft(lineage), gateway.listVersions(lineage)])
        applyServerDraft(draft)
        versions.value = rows
        return true
      } catch (caught) {
        error.value = `发布失败：${describeFailure(caught)}`
        return false
      } finally {
        loading.value = false
      }
    }

    async function restoreVersion(workflowId: string): Promise<boolean> {
      const lineage = lineageId.value
      if (lineage === '') {
        error.value = '当前画布还没有 lineage，无法恢复版本'
        return false
      }
      return runLoad('恢复版本失败', async () => {
        cancelScheduledSave()
        abortInFlight()
        const draft = await gateway.revertToDraft(lineage, workflowId)
        autoSaveEnabled.value = true
        applyServerDraft(draft)
        await refreshVersions()
      })
    }

    /** 冲突处置一：以服务器草稿为准，本地未保存的修改丢掉（调用方先确认）。 */
    async function reloadDraft(): Promise<boolean> {
      const lineage = lineageId.value
      if (lineage === '') return false
      return runLoad('读取服务器草稿失败', async () => {
        const draft = await gateway.getDraft(lineage)
        autoSaveEnabled.value = true
        applyServerDraft(draft)
        await refreshVersions()
      })
    }

    /** 冲突处置二：拿服务器 revision 覆盖写回，本地内容保留。 */
    async function overwriteDraft(): Promise<boolean> {
      const lineage = lineageId.value
      if (lineage === '') return false
      loading.value = true
      error.value = null
      try {
        const server = await gateway.getDraft(lineage)
        revision.value = server.revision
        baseWorkflowId.value = server.base_workflow_id
        saveState.value = { kind: 'dirty', since: nowIso() }
        return await saveNow('manual')
      } catch (caught) {
        error.value = `覆盖草稿失败：${describeFailure(caught)}`
        return false
      } finally {
        loading.value = false
      }
    }

    function dispose(): void {
      cancelScheduledSave()
      abortInFlight()
    }

    return {
      document,
      documentEpoch,
      definition,
      lineageId,
      revision,
      baseWorkflowId,
      versions,
      saveState,
      statusText,
      error,
      loading,
      isDirty,
      autoSaveEnabled,
      canUndo,
      canRedo,
      undoLabel,
      redoLabel,
      undo,
      redo,
      structuralErrors,
      validationErrors,
      canSave,
      canAutosave,
      publishedWorkflowId,
      publishedVersion,
      setMeta,
      seedPositions,
      addNode,
      insertSubgraph,
      moveNode,
      moveNodes,
      addNote,
      updateNote,
      removeNote,
      autoLayout,
      patchNode,
      connect,
      insertNodeBetween,
      updateEdgeCondition,
      removeEdge,
      removeNodes,
      removeSelection,
      syncUpstreamConfig,
      resetDocument,
      openLineage,
      openVersion,
      bindInstanceDocument,
      syncInstanceDocument,
      stashDraft,
      restoreDraft,
      clearDraftStash,
      refreshVersions,
      saveNow,
      flush,
      publish,
      restoreVersion,
      reloadDraft,
      overwriteDraft,
      dispose,
    }
  })
}

export const useDocumentStore = createDocumentStore()
export type DocumentStore = ReturnType<typeof useDocumentStore>
