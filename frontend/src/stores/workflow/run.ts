/**
 * 运行层：实例队列、当前实例、轮询与人工核签。
 *
 * 与文档层的边界：这里只读文档（节点名、决策候选、已发布版本号），
 * 不修改草稿；focusInstance 把那一版文档作为只读快照放进画布。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import {
  toNodeInput,
  workflowApi,
  type DecisionInput,
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
  type InstanceStatus,
  type JsonValue,
  type NodeDef,
  type NodeState,
} from '@/utils/graph'

import { useDocumentStore } from './document'
import { useEditorStore } from './editor'
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
    const polling = ref(false)
    const capture = ref<{ instanceId: string; nodeId: string; text: string; landed: boolean } | null>(null)

    let timer: ReturnType<typeof setInterval> | null = null
    let queueTimer: ReturnType<typeof setInterval> | null = null
    let listToken = 0
    let viewToken = 0

    const runs = computed<Map<string, InstanceNodeRun>>(
      () => new Map((instance.value?.nodes ?? []).map((run) => [run.node_id, run])),
    )

    const instanceStatus = computed<InstanceStatus | null>(() => {
      const raw = instance.value?.status
      return raw !== undefined && isInstanceStatus(raw) ? raw : null
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

    function applyInstanceSnapshot(instanceId: string, snapshot: InstanceDetail): boolean {
      if (instance.value?.instance_id !== instanceId) return false
      instance.value = snapshot
      return true
    }

    async function startInstance(payload: Record<string, JsonValue> = {}): Promise<boolean> {
      const workflowId = documentStore.publishedWorkflowId
      if (workflowId === null) {
        editorStore.setError('请先发布工作流版本再启动实例')
        return false
      }
      return editorStore.runAction(async () => {
        const token = ++viewToken
        const created = await gateway.startInstance({ workflow_id: workflowId, payload })
        await syncInstancesQuietly()
        if (token !== viewToken) return
        instance.value = created
        startPolling()
      }, '启动实例失败')
    }

    async function focusInstance(instanceId: string): Promise<boolean> {
      stopPolling()
      return editorStore.runAction(async () => {
        const token = ++viewToken
        const detail = await gateway.instance(instanceId)
        if (token !== viewToken) return
        instance.value = detail
        const definition = await gateway.getVersionDocument(detail.workflow_id)
        if (token !== viewToken) return
        documentStore.bindInstanceDocument(definition)
        editorStore.setMode({ kind: 'instance', instanceId, readOnly: true })
        const awaiting = detail.nodes.find((node) => node.state === 'awaiting_human') ?? detail.nodes[0]
        editorStore.select(awaiting?.node_id ?? null)
        startPolling()
      }, '读取实例失败')
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
        instance.value = next
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
        await gateway.insertNode(instanceId, { after, node: toNodeInput(node) })
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
      polling,
      capture,
      instanceStatus,
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
      focusInstance,
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
