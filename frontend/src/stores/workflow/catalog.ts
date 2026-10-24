/**
 * 目录层的服务端快照：节点类型与已发布定义列表。
 *
 * 与文档层分开的理由：这两列是"别人的数据"，会自己长出来（助手/同事新存一版），
 * 刷新它们不该碰画布上的草稿；画布上的保存也不该被列表轮询打断。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import type { DefinitionSummary, NodeTypeEntry, WorkflowGateway } from '@/api/workflow/contract'
import { workflowApi } from '@/api/workflow'
import { NODE_SPECS, PANEL_NODE_TYPES, nodeSpec, type NodeType } from '@/utils/graph'

import { describeFailure, nowIso } from './shared'

/** 与 document 的 15 秒自查同口径：定义列表不是实时流，但也不该是一张开页快照。 */
const CATALOG_POLL_INTERVAL_MS = 15_000

/** 服务端目录与本地镜像的差异。差异本身不阻断编辑，只把"两端说的不是同一句话"摆到台面上。 */
export interface CatalogDrift {
  /** 服务端已注册、本地镜像还没有：旧前端遇到新后端。 */
  readonly unknown: string[]
  /** 本地面板有、服务端没给：前端比后端新，或注册表被裁剪。 */
  readonly missing: string[]
  /** 两边都有，但类别或产出键对不上。 */
  readonly mismatched: string[]
}

const EMPTY_DRIFT: CatalogDrift = { unknown: [], missing: [], mismatched: [] }

function sameOutputs(left: readonly string[], right: readonly string[]): boolean {
  return JSON.stringify([...left].sort()) === JSON.stringify([...right].sort())
}

export function createCatalogStore(gateway: WorkflowGateway = workflowApi) {
  return defineStore('workflow.catalog', () => {
    const nodeTypes = ref<NodeTypeEntry[]>([])
    const definitions = ref<DefinitionSummary[]>([])
    const definitionsUpdatedAt = ref<string | null>(null)
    const nodeTypesUpdatedAt = ref<string | null>(null)
    const nodeTypesLoading = ref(false)
    const nodeTypesError = ref<string | null>(null)
    const error = ref<string | null>(null)
    const loading = ref(false)
    let definitionToken = 0
    let pollTimer: ReturnType<typeof setInterval> | null = null

    const descriptionByType = computed<Record<string, string>>(() =>
      Object.fromEntries(nodeTypes.value.map((entry) => [entry.type, entry.description])),
    )

    /**
     * 服务端目录 vs 本地镜像。`defToGraph` / 校验 / 面板全走本地镜像，
     * 所以差异不会让画布报错；但它是"升级只做了一半"的唯一可见证据。
     * 没有服务端目录（还没加载成功）时不下结论：空集合不等于"全都缺"。
     */
    const drift = computed<CatalogDrift>(() => {
      if (nodeTypes.value.length === 0) return EMPTY_DRIFT
      const seen = new Map(nodeTypes.value.map((entry) => [entry.type, entry]))
      const unknown = [...seen.keys()].filter((type) => nodeSpec(type) === null).sort()
      const missing = PANEL_NODE_TYPES.filter((type) => !seen.has(type))
      const mismatched = [...seen.entries()]
        .filter(([type, entry]) => {
          const local = NODE_SPECS[type as NodeType]
          return (
            local !== undefined && (local.category !== entry.category || !sameOutputs(local.outputs, entry.outputs))
          )
        })
        .map(([type]) => type)
        .sort()
      return { unknown, missing, mismatched }
    })
    const hasDrift = computed(
      () => drift.value.unknown.length + drift.value.missing.length + drift.value.mismatched.length > 0,
    )
    const driftSummary = computed(() => {
      const parts: string[] = []
      if (drift.value.unknown.length > 0) parts.push(`服务端新增类型 ${drift.value.unknown.join('、')}`)
      if (drift.value.missing.length > 0) parts.push(`本地多出类型 ${drift.value.missing.join('、')}`)
      if (drift.value.mismatched.length > 0) parts.push(`类别或产出键不一致 ${drift.value.mismatched.join('、')}`)
      return parts.join('；')
    })

    /**
     * 节点目录失败**不抛**：面板靠本地镜像照常可用，只是描述退回本地兜底文案。
     * 返回值给引导流程决定要不要提示，错误留在 `nodeTypesError` 供横幅与重试使用。
     */
    async function loadNodeTypes(): Promise<boolean> {
      nodeTypesLoading.value = true
      nodeTypesError.value = null
      try {
        nodeTypes.value = (await gateway.nodeTypes()).items
        nodeTypesUpdatedAt.value = nowIso()
        return true
      } catch (caught) {
        nodeTypesError.value = `节点目录没加载：${describeFailure(caught)}`
        return false
      } finally {
        nodeTypesLoading.value = false
      }
    }

    /** 重试与首次加载同一条路径：失败态只有一份，不会出现"重试成功了但错误还在"。 */
    const retryNodeTypes = loadNodeTypes

    async function loadDefinitions(): Promise<void> {
      const token = ++definitionToken
      const next = (await gateway.definitions()).items
      // 自查与保存/归档后的对账会撞在一起：后发的那一趟赢，旧快照不许把列表往回带。
      if (token !== definitionToken) return
      definitions.value = next
      definitionsUpdatedAt.value = nowIso()
    }

    async function syncDefinitionsQuietly(failureLabel = '定义列表没刷新'): Promise<void> {
      try {
        await loadDefinitions()
      } catch (caught) {
        error.value = `${failureLabel}：${describeFailure(caught)}`
      }
    }

    async function runCatalog(action: () => Promise<void>, label: string): Promise<boolean> {
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

    async function archiveDefinition(workflowId: string): Promise<boolean> {
      return runCatalog(async () => {
        await gateway.archiveDefinition(workflowId)
        await loadDefinitions()
      }, '归档定义失败')
    }

    async function restoreDefinition(workflowId: string): Promise<boolean> {
      return runCatalog(async () => {
        await gateway.restoreDefinition(workflowId)
        await loadDefinitions()
      }, '取消归档失败')
    }

    function startDefinitionPolling(): void {
      if (pollTimer !== null) return
      pollTimer = setInterval(() => {
        if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return
        void syncDefinitionsQuietly()
      }, CATALOG_POLL_INTERVAL_MS)
    }

    function stopDefinitionPolling(): void {
      if (pollTimer !== null) clearInterval(pollTimer)
      pollTimer = null
    }

    return {
      nodeTypes,
      definitions,
      definitionsUpdatedAt,
      nodeTypesUpdatedAt,
      nodeTypesLoading,
      nodeTypesError,
      error,
      loading,
      descriptionByType,
      drift,
      hasDrift,
      driftSummary,
      loadNodeTypes,
      retryNodeTypes,
      loadDefinitions,
      syncDefinitionsQuietly,
      archiveDefinition,
      restoreDefinition,
      startDefinitionPolling,
      stopDefinitionPolling,
    }
  })
}

export const useCatalogStore = createCatalogStore()
export type CatalogStore = ReturnType<typeof useCatalogStore>
