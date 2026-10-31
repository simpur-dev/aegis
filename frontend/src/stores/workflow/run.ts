/**
 * 运行层：实例队列、当前实例、轮询与人工核签。
 *
 * 与文档层的边界：这里只读文档（节点名、决策候选、已发布版本号），
 * 不修改草稿；focusInstance 把那一版文档作为只读快照放进画布。
 */
import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'

import {
  encodeDocument,
  toNodeInput,
  workflowApi,
  type DecisionInput,
  type InsertNodeInput,
  type InstanceDetail,
  type InstanceNodeRun,
  type LabelCapture,
  type WorkflowGateway,
} from '@/api/workflow'
import {
  canBypass,
  canDecide,
  canPatchRuntimeConfig,
  findNode,
  isInstanceStateTerminal,
  isInstanceStatus,
  layerPositions,
  type InstanceStatus,
  type JsonValue,
  type NodeDef,
  type NodeState,
  type Position,
} from '@/utils/graph'
import {
  DOCUMENT_SCHEMA_VERSION,
  canonicalDocumentJson,
  copyNodeDef,
  type WorkflowDocument,
} from '@/utils/graph/document'

import { useDocumentStore } from './document'
import { useEditorStore, type EditorMode } from './editor'
import { describeFailure } from './shared'

const POLL_INTERVAL_MS = 1_500
const QUEUE_POLL_INTERVAL_MS = 15_000

function describeCapture(raw: LabelCapture | undefined): { text: string; landed: boolean } {
  if (raw === undefined) {
    return { text: '这一单没有"结论裁决"标记，后端不收进标注池（画布上手工起的实例就是这种）', landed: false }
  }
  if (raw.status === '已沉淀') {
    return { text: `这一票已进真值表：${raw.真值 ?? ''}（事件 ${raw.case_id ?? '未知'}）`, landed: true }
  }
  return { text: `这一票没进真值表：${raw.原因 ?? '后端没说明原因'}`, landed: false }
}

export function createRunStore(gateway: WorkflowGateway = workflowApi) {
  return defineStore('workflow.run', () => {
    const documentStore = useDocumentStore()
    const editorStore = useEditorStore()

    const instances = ref<InstanceDetail[]>([])
    const instancesUpdatedAt = ref<string | null>(null)
    const instance = ref<InstanceDetail | null>(null)
    /** 试运行结果：与正式实例分开存放，实例列表/轮询都不认它。 */
    const preview = ref<InstanceDetail | null>(null)
    const polling = ref(false)
    const capture = ref<{ instanceId: string; nodeId: string; text: string; landed: boolean } | null>(null)

    let timer: ReturnType<typeof setInterval> | null = null
    let queueTimer: ReturnType<typeof setInterval> | null = null
    let listToken = 0
    let viewToken = 0
    let previewToken = 0
    /** 试运行对应的是哪一份文档；草稿一改，旧结果就不能再画在画布上。 */
    let previewSignature: string | null = null
    /** 进实例前在哪一档：返回时回到同一处，而不是一律当草稿。 */
    let modeBeforeInstance: EditorMode | null = null

    const runs = computed<Map<string, InstanceNodeRun>>(
      () => new Map((instance.value?.nodes ?? []).map((run) => [run.node_id, run])),
    )

    const instanceStatus = computed<InstanceStatus | null>(() => {
      const raw = instance.value?.status
      return raw !== undefined && isInstanceStatus(raw) ? raw : null
    })

    const previewStatus = computed<InstanceStatus | null>(() => {
      const raw = preview.value?.status
      return raw !== undefined && isInstanceStatus(raw) ? raw : null
    })

    /**
     * 画布上该叠哪一份运行态。
     *
     * 实例模式只认实例（它画的是实例自己的图）；只读版本不叠状态；
     * 草稿模式只认试运行——正式实例的状态属于另一张图，叠上来会把两版节点认错。
     */
    const canvasRuns = computed<InstanceNodeRun[]>(() => {
      if (editorStore.mode.kind === 'instance') return instance.value?.nodes ?? []
      if (editorStore.mode.kind === 'version') return []
      return preview.value?.nodes ?? []
    })

    /** 实例仍可操作 = 未进入终态（model.py TERMINAL）。 */
    const instanceMutable = computed(() => {
      const status = instanceStatus.value
      return status !== null && !isInstanceStateTerminal(status)
    })

    const awaitingNodes = computed<InstanceNodeRun[]>(() =>
      (instance.value?.nodes ?? []).filter((run) => run.state === 'awaiting_human'),
    )

    function nodeStateOf(nodeId: string): NodeState | null {
      return runs.value.get(nodeId)?.state ?? null
    }

    function nodeRun(nodeId: string): InstanceNodeRun | null {
      return runs.value.get(nodeId) ?? null
    }

    function canPatchRuntime(nodeId: string): boolean {
      return instanceMutable.value && canPatchRuntimeConfig(nodeStateOf(nodeId))
    }

    function canBypassNode(nodeId: string): boolean {
      return instanceMutable.value && canBypass(nodeStateOf(nodeId))
    }

    function canDecideNode(nodeId: string): boolean {
      return instanceMutable.value && canDecide(nodeStateOf(nodeId))
    }

    /** 候选取自定义 options；为空表示引擎未登记候选，UI 退化为自由文本输入。 */
    function decisionOptions(nodeId: string): string[] {
      const def = documentStore.definition
      const options = def === null ? undefined : findNode(def, nodeId)?.config.options
      return Array.isArray(options) ? options.filter((item): item is string => typeof item === 'string') : []
    }

    async function loadInstances(): Promise<void> {
      const token = ++listToken
      const next = (await gateway.instances()).items
      // 后发的那一趟赢：旧快照晚到会把数字往回带（7 条又变回 6 条）。
      if (token !== listToken) return
      instances.value = next
      instancesUpdatedAt.value = new Date().toISOString()
    }

    async function syncInstancesQuietly(): Promise<void> {
      try {
        await loadInstances()
      } catch (caught) {
        editorStore.setError(`实例列表没刷新：${describeFailure(caught)}`)
      }
    }

    /**
     * 实例详情 → 画布文档。
     *
     * 图来自详情自身的快照（含运行中插入的节点），不是版本文档；坐标缺哪颗就按分层兜底，
     * 所以没有 layout 的旧实例也能画出来。
     */
    function instanceDocument(detail: InstanceDetail): WorkflowDocument {
      const nodes = detail.graph.nodes.map(copyNodeDef)
      const edges = detail.graph.edges.map((edge) => ({ ...edge }))
      const ids = nodes.map((node) => node.node_id)
      let fallback: Record<string, Position> = {}
      if (ids.length > 0) {
        try {
          fallback = layerPositions(ids, edges)
        } catch {
          // 图有环也不该让整页崩：坐标退回原点，环路本身由后端在图校验里拦。
        }
      }
      const positions: Record<string, Position> = {}
      for (const nodeId of ids) {
        positions[nodeId] = detail.graph.positions[nodeId] ?? fallback[nodeId] ?? { x: 0, y: 0 }
      }
      const lineage = detail.lineage_id === '' ? detail.workflow_id : detail.lineage_id
      const current = documentStore.document
      const sameLineage = current !== null && current.lineage_id === lineage
      const known = documentStore.versions.find((row) => row.workflow_id === detail.workflow_id)?.name
      return {
        schema_version: DOCUMENT_SCHEMA_VERSION,
        lineage_id: lineage,
        name: known ?? (sameLineage ? current.name : '运行实例'),
        description: sameLineage ? current.description : '',
        nodes,
        edges,
        layout: { positions, viewport: null, notes: [] },
      }
    }

    /** 空图（旧后端没给 graph）时保持现有画布，不用一张空图把人正在看的东西换掉。 */
    function bindInstanceCanvas(detail: InstanceDetail): void {
      if (detail.graph.nodes.length === 0) return
      documentStore.bindInstanceDocument(instanceDocument(detail))
    }

    function applyInstanceSnapshot(instanceId: string, snapshot: InstanceDetail): boolean {
      if (instance.value?.instance_id !== instanceId) return false
      instance.value = snapshot
      // 实例运行中插节点后，画布上的图也要跟着长出来——否则右侧说"已插入"、画布纹丝不动。
      if (editorStore.mode.kind === 'instance' && editorStore.mode.instanceId === instanceId) {
        documentStore.syncInstanceDocument(instanceDocument(snapshot))
      }
      return true
    }

    function currentNodeOf(detail: InstanceDetail): string | null {
      const awaiting = detail.nodes.find((node) => node.state === 'awaiting_human')
      const failed = detail.nodes.find((node) => node.state === 'failed')
      return (awaiting ?? failed ?? detail.nodes[0])?.node_id ?? null
    }

    async function startInstance(payload: Record<string, JsonValue> = {}): Promise<boolean> {
      const workflowId = documentStore.publishedWorkflowId
      if (workflowId === null) {
        editorStore.setError('请先发布工作流版本再启动实例')
        return false
      }
      if (editorStore.mode.kind !== 'instance') modeBeforeInstance = editorStore.mode
      return editorStore.runAction(async () => {
        const token = ++viewToken
        const created = await gateway.startInstance({ workflow_id: workflowId, payload })
        await syncInstancesQuietly()
        if (token !== viewToken) return
        clearPreview()
        instance.value = created
        bindInstanceCanvas(created)
        editorStore.setMode({ kind: 'instance', instanceId: created.instance_id, readOnly: true })
        editorStore.select(currentNodeOf(created))
        startPolling()
      }, '启动实例失败')
    }

    async function focusInstance(instanceId: string): Promise<boolean> {
      stopPolling()
      if (editorStore.mode.kind !== 'instance') modeBeforeInstance = editorStore.mode
      return editorStore.runAction(async () => {
        const token = ++viewToken
        const detail = await gateway.instance(instanceId)
        if (token !== viewToken) return
        instance.value = detail
        bindInstanceCanvas(detail)
        editorStore.setMode({ kind: 'instance', instanceId, readOnly: true })
        editorStore.select(currentNodeOf(detail))
        startPolling()
      }, '读取实例失败')
    }

    function clearPreview(): void {
      previewToken += 1
      preview.value = null
      previewSignature = null
    }

    /**
     * 试运行：把**当前画布上的草稿**送给服务端跑一遍，包括还没保存的改动。
     *
     * 结果只进 `preview`，不碰实例账本、不轮询；外部动作由服务端换成回执。
     */
    async function startPreview(payload: Record<string, JsonValue> = {}): Promise<boolean> {
      const document = documentStore.document
      if (document === null) {
        editorStore.setError('画布尚未初始化')
        return false
      }
      if (editorStore.mode.kind === 'instance') {
        editorStore.setError('先返回草稿再试运行：试运行跑的是画布上的草稿')
        return false
      }
      if (editorStore.mode.kind !== 'draft') {
        editorStore.setError('当前是只读版本视图：先恢复为草稿，再试运行')
        return false
      }
      if (document.lineage_id === '') {
        editorStore.setError('空白画布还没有 lineage：先保存草稿或打开一份定义，再试运行')
        return false
      }
      return editorStore.runAction(async () => {
        const token = ++previewToken
        const result = await gateway.createPreview({ document: encodeDocument(document), payload })
        if (token !== previewToken) return
        preview.value = result
        previewSignature = canonicalDocumentJson(document)
        editorStore.setMode({ kind: 'draft' })
        editorStore.select(currentNodeOf(result))
      }, '试运行失败')
    }

    /** 草稿一改，旧试运行结果就不能再画在画布上：它说的是另一张图。 */
    watch(
      () => documentStore.document,
      (current) => {
        if (preview.value === null) return
        if (current === null || previewSignature === null || canonicalDocumentJson(current) !== previewSignature) {
          clearPreview()
        }
      },
    )

    /** 返回进入实例前的那一档：草稿从收起的那份原样放回，不打接口。 */
    async function exitInstance(): Promise<boolean> {
      stopPolling()
      const previous = modeBeforeInstance
      const lineage = instance.value?.lineage_id ?? instance.value?.workflow_id ?? ''
      if (previous?.kind === 'version') {
        if (!(await documentStore.openVersion(previous.workflowId))) return false
        leaveInstance(previous)
        return true
      }
      if (documentStore.restoreDraft()) {
        leaveInstance({ kind: 'draft' })
        return true
      }
      if (lineage !== '') {
        if (!(await documentStore.openLineage(lineage))) return false
      } else {
        documentStore.resetDocument()
      }
      leaveInstance({ kind: 'draft' })
      return true
    }

    function leaveInstance(mode: EditorMode): void {
      stopPolling()
      instance.value = null
      capture.value = null
      clearPreview()
      modeBeforeInstance = null
      editorStore.setMode(mode)
      editorStore.select(null)
    }

    /** 队列顺序与 RuntimePanel 一致（新到旧、等人签优先），打开的就是首行那张。 */
    async function openFirstWaiting(): Promise<boolean> {
      const first = [...instances.value].reverse().find((row) => row.status === 'waiting')
      if (first === undefined) return false
      return focusInstance(first.instance_id)
    }

    async function refreshInstance(): Promise<void> {
      const instanceId = instance.value?.instance_id
      if (instanceId === undefined) return
      const wasMutable = instanceMutable.value
      const token = viewToken
      try {
        const next = await gateway.instance(instanceId)
        // 迟到的轮询不许把面板翻回上一条：人可能已经改点另一张单。
        if (token !== viewToken || instance.value?.instance_id !== instanceId) return
        applyInstanceSnapshot(instanceId, next)
        if (wasMutable && isInstanceStatus(next.status) && isInstanceStateTerminal(next.status)) await loadInstances()
      } catch (caught) {
        editorStore.setError(describeFailure(caught))
      }
    }

    async function refreshAll(): Promise<boolean> {
      return editorStore.runAction(async () => {
        await Promise.all([refreshInstance(), loadInstances()])
      }, '刷新实例失败')
    }

    function startPolling(): void {
      if (timer !== null) return
      polling.value = true
      timer = setInterval(() => {
        if (!instanceMutable.value) {
          stopPolling()
          return
        }
        void refreshInstance()
      }, POLL_INTERVAL_MS)
    }

    function stopPolling(): void {
      if (timer !== null) clearInterval(timer)
      timer = null
      polling.value = false
    }

    function startQueuePolling(): void {
      if (queueTimer !== null) return
      queueTimer = setInterval(() => {
        if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return
        void syncInstancesQuietly()
      }, QUEUE_POLL_INTERVAL_MS)
    }

    function stopQueuePolling(): void {
      if (queueTimer !== null) clearInterval(queueTimer)
      queueTimer = null
    }

    async function submitDecision(
      nodeId: string,
      choice: string,
      comment = '',
      by = '',
      level: number | null = null,
    ): Promise<boolean> {
      const instanceId = instance.value?.instance_id
      if (instanceId === undefined) return false
      return editorStore.runAction(async () => {
        // 平台没有登录态：留空就不发 by，让服务端记 unknown，而不是替人签名。
        const payload: DecisionInput = by === '' ? { choice, comment } : { choice, by, comment }
        if (level !== null) payload.level = level
        const detail = await gateway.submitDecision(instanceId, nodeId, payload)
        capture.value = { instanceId, nodeId, ...describeCapture(detail.标注沉淀) }
        applyInstanceSnapshot(instanceId, detail)
        await syncInstancesQuietly()
      }, '提交人工核签失败')
    }

    function captureFor(nodeId: string): { text: string; landed: boolean } | null {
      const record = capture.value
      if (record === null || record.nodeId !== nodeId || record.instanceId !== instance.value?.instance_id) return null
      return { text: record.text, landed: record.landed }
    }

    async function patchRuntimeConfig(nodeId: string, config: Record<string, JsonValue>): Promise<boolean> {
      const instanceId = instance.value?.instance_id
      if (instanceId === undefined) return false
      return editorStore.runAction(async () => {
        const result = await gateway.patchNodeConfig(instanceId, nodeId, config)
        await refreshInstance()
        // 改参接口按键合并，清空表达不出"删除"——至少把没改成空的格子说明白。
        const kept = Object.keys(result.config).filter((key) => !(key in config))
        if (kept.length > 0) {
          editorStore.setError(
            `这 ${kept.length} 格没能改成空：${kept.join('、')}——改参接口按键合并，清空表达不出"删除"，服务端仍是原值；要改请填新值`,
          )
        }
      }, '运行中改参失败')
    }

    async function insertRuntimeNode(after: string, node: NodeDef): Promise<boolean> {
      const instanceId = instance.value?.instance_id
      if (instanceId === undefined) return false
      return editorStore.runAction(async () => {
        // 落点跟着锚点走：实例画布的坐标就在当前文档里，服务端不需要再猜一次。
        const anchor = documentStore.document?.layout.positions[after]
        const position = anchor === undefined ? undefined : { x: Math.round(anchor.x + 300), y: Math.round(anchor.y) }
        const payload: InsertNodeInput = {
          after,
          node: toNodeInput(node),
          ...(position === undefined ? {} : { position }),
        }
        await gateway.insertNode(instanceId, payload)
        applyInstanceSnapshot(instanceId, await gateway.instance(instanceId))
      }, '插入节点失败')
    }

    async function bypassNode(nodeId: string, reason = ''): Promise<boolean> {
      const instanceId = instance.value?.instance_id
      if (instanceId === undefined) return false
      return editorStore.runAction(async () => {
        applyInstanceSnapshot(instanceId, await gateway.bypassNode(instanceId, nodeId, reason))
        await syncInstancesQuietly()
      }, '绕过节点失败')
    }

    async function abortInstance(reason = ''): Promise<boolean> {
      const instanceId = instance.value?.instance_id
      if (instanceId === undefined) return false
      return editorStore.runAction(async () => {
        if (applyInstanceSnapshot(instanceId, await gateway.abortInstance(instanceId, reason))) stopPolling()
        await syncInstancesQuietly()
      }, '中止实例失败')
    }

    return {
      instances,
      instancesUpdatedAt,
      instance,
      preview,
      polling,
      capture,
      instanceStatus,
      previewStatus,
      canvasRuns,
      instanceMutable,
      awaitingNodes,
      nodeStateOf,
      nodeRun,
      canPatchRuntime,
      canBypassNode,
      canDecideNode,
      decisionOptions,
      loadInstances,
      startInstance,
      startPreview,
      clearPreview,
      focusInstance,
      exitInstance,
      openFirstWaiting,
      refreshInstance,
      refreshAll,
      startPolling,
      stopPolling,
      startQueuePolling,
      stopQueuePolling,
      submitDecision,
      captureFor,
      patchRuntimeConfig,
      insertRuntimeNode,
      bypassNode,
      abortInstance,
    }
  })
}

export const useRunStore = createRunStore()
export type RunStore = ReturnType<typeof useRunStore>
