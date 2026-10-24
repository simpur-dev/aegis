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

import { describeFailure, nowIso } from './shared'

/** 与 document 的 15 秒自查同口径：定义列表不是实时流，但也不该是一张开页快照。 */
const CATALOG_POLL_INTERVAL_MS = 15_000

export function createCatalogStore(gateway: WorkflowGateway = workflowApi) {
  return defineStore('workflow.catalog', () => {
    const nodeTypes = ref<NodeTypeEntry[]>([])
    const definitions = ref<DefinitionSummary[]>([])
    const definitionsUpdatedAt = ref<string | null>(null)
    const error = ref<string | null>(null)
    const loading = ref(false)
    let definitionToken = 0
    let pollTimer: ReturnType<typeof setInterval> | null = null

    const descriptionByType = computed<Record<string, string>>(() =>
      Object.fromEntries(nodeTypes.value.map((entry) => [entry.type, entry.description])),
    )

    async function loadNodeTypes(): Promise<void> {
      nodeTypes.value = (await gateway.nodeTypes()).items
    }

    async function loadDefinitions(): Promise<void> {
      const token = ++definitionToken
      const next = (await gateway.definitions()).items
      // 自查与保存/归档后的对账会撞在一起：后发的那一趟赢，旧快照不许把列表往回带。
      if (token !== definitionToken) return
      definitions.value = next
      definitionsUpdatedAt.value = nowIso()
    }

    async function syncDefinitionsQuietly(): Promise<void> {
      try {
        await loadDefinitions()
      } catch (caught) {
        error.value = `定义列表没刷新：${describeFailure(caught)}`
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
      error,
      loading,
      descriptionByType,
      loadNodeTypes,
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
