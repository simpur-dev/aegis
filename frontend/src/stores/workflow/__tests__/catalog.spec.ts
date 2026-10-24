import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  WorkflowError,
  type DefinitionSummary,
  type DefinitionsResponse,
  type NodeTypesResponse,
  type WorkflowGateway,
} from '@/api/workflow/contract'

import { createCatalogStore, type CatalogStore } from '../catalog'
import { UPDATED_AT, deferred, gatewayStub, setVisibility, type StubGateway } from './fixtures'

function summary(workflowId: string, status: 'active' | 'archived' = 'active'): DefinitionSummary {
  return {
    workflow_id: workflowId,
    lineage_id: workflowId,
    name: workflowId,
    version: 1,
    status,
    description: '',
    node_count: 2,
    edge_count: 1,
    created_at: UPDATED_AT,
    content_hash: 'sha256:abc',
  }
}

function definitionsOf(items: DefinitionSummary[]): DefinitionsResponse {
  return { count: items.length, items }
}

const WF = 'wf_0123456789ab'

let gateway: StubGateway
let store: CatalogStore | undefined

function bootstrap(overrides: Partial<Record<keyof WorkflowGateway, unknown>> = {}): void {
  gateway = gatewayStub(overrides)
  store = createCatalogStore(gateway)()
}

function catalog(): CatalogStore {
  if (store === undefined) throw new Error('store 未创建：先调 bootstrap()')
  return store
}

beforeEach(() => {
  setActivePinia(createPinia())
  store = undefined
  vi.useRealTimers()
})

afterEach(() => {
  store?.stopDefinitionPolling()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('目录层：节点面板与定义列表', () => {
  it('节点面板按类型索引描述', async () => {
    const nodeTypes: NodeTypesResponse = {
      count: 2,
      items: [
        {
          type: 'data_fetch',
          description: '数据接入：按区域/指标拉取最新读数',
          required_config: [],
          optional_config: [],
        },
        { type: 'join', description: '并行汇聚：合并多路上游结果', required_config: ['upstream'], optional_config: [] },
      ],
    }
    bootstrap({ nodeTypes: vi.fn(async () => nodeTypes) })
    await catalog().loadNodeTypes()
    expect(catalog().nodeTypes).toHaveLength(2)
    expect(catalog().descriptionByType.data_fetch).toContain('数据接入')
    expect(catalog().descriptionByType.join).toContain('并行汇聚')
  })

  it('定义列表取回后写上取数时刻', async () => {
    bootstrap({ definitions: vi.fn(async () => definitionsOf([summary(WF)])) })
    expect(catalog().definitionsUpdatedAt).toBeNull()
    await catalog().loadDefinitions()
    expect(catalog().definitions).toHaveLength(1)
    expect(catalog().definitionsUpdatedAt).not.toBeNull()
  })

  it('先发后回的旧快照不许把列表往回带', async () => {
    const slow = deferred<DefinitionsResponse>()
    const fast = deferred<DefinitionsResponse>()
    const definitions = vi.fn<WorkflowGateway['definitions']>()
    definitions.mockReturnValueOnce(slow.promise).mockReturnValueOnce(fast.promise)
    bootstrap({ definitions })

    const first = catalog().loadDefinitions()
    await Promise.resolve()
    const second = catalog().loadDefinitions()
    fast.resolve(definitionsOf([summary('wf_aaaaaaaaaaaa'), summary('wf_bbbbbbbbbbbb'), summary('wf_cccccccccccc')]))
    await second
    expect(catalog().definitions).toHaveLength(3)

    slow.resolve(definitionsOf([summary(WF)]))
    await first
    expect(catalog().definitions).toHaveLength(3)
  })

  it('后台自查失败只记一句，不把页面炸掉', async () => {
    bootstrap({
      definitions: vi.fn<WorkflowGateway['definitions']>(async () => {
        throw new WorkflowError('network_error', 0, '接口调用失败（网络）：断网')
      }),
    })
    await catalog().syncDefinitionsQuietly()
    expect(catalog().error).toContain('定义列表没刷新')
    expect(catalog().error).toContain('断网')
  })

  it('归档后重拉列表；取消归档同理', async () => {
    const definitions = vi.fn<WorkflowGateway['definitions']>()
    definitions
      .mockResolvedValueOnce(definitionsOf([summary(WF)]))
      .mockResolvedValueOnce(definitionsOf([summary(WF, 'archived')]))
      .mockResolvedValueOnce(definitionsOf([summary(WF)]))
    bootstrap({
      definitions,
      archiveDefinition: vi.fn(async () => ({ workflow_id: WF, status: 'archived' })),
      restoreDefinition: vi.fn(async () => ({ workflow_id: WF, status: 'active' })),
    })
    await catalog().loadDefinitions()
    expect(catalog().definitions[0]?.status).toBe('active')

    expect(await catalog().archiveDefinition(WF)).toBe(true)
    expect(gateway.archiveDefinition).toHaveBeenCalledWith(WF)
    expect(catalog().definitions[0]?.status).toBe('archived')

    expect(await catalog().restoreDefinition(WF)).toBe(true)
    expect(gateway.restoreDefinition).toHaveBeenCalledWith(WF)
    expect(catalog().definitions[0]?.status).toBe('active')
    expect(catalog().loading).toBe(false)
  })

  it('归档失败时报后端原因，列表保持原样', async () => {
    bootstrap({
      definitions: vi.fn(async () => definitionsOf([summary(WF)])),
      archiveDefinition: vi.fn<WorkflowGateway['archiveDefinition']>(async () => {
        throw new WorkflowError('archived', 409, '该工作流已归档')
      }),
    })
    await catalog().loadDefinitions()
    expect(await catalog().archiveDefinition(WF)).toBe(false)
    expect(catalog().error).toBe('归档定义失败：该工作流已归档')
    expect(catalog().definitions).toHaveLength(1)
    expect(catalog().loading).toBe(false)
  })
})

describe('目录层：定义列表自查', () => {
  it('页签在后台时定义自查不打接口', async () => {
    vi.useFakeTimers()
    const restore = setVisibility('hidden')
    try {
      bootstrap({ definitions: vi.fn(async () => definitionsOf([])) })
      await catalog().loadDefinitions()
      const before = gateway.definitions.mock.calls.length
      catalog().startDefinitionPolling()
      await vi.advanceTimersByTimeAsync(45_000)
      expect(gateway.definitions.mock.calls.length).toBe(before)
      catalog().stopDefinitionPolling()
    } finally {
      restore()
    }
  })

  it('别人新存的那一版 15 秒内进列表', async () => {
    vi.useFakeTimers()
    bootstrap({ definitions: vi.fn(async () => definitionsOf([])) })
    await catalog().loadDefinitions()
    gateway.definitions.mockResolvedValue(definitionsOf([summary('wf_aaaaaaaaaaaa'), summary('wf_bbbbbbbbbbbb')]))

    const before = gateway.definitions.mock.calls.length
    catalog().startDefinitionPolling()
    await vi.advanceTimersByTimeAsync(15_000)
    expect(gateway.definitions.mock.calls.length).toBe(before + 1)
    expect(catalog().definitions).toHaveLength(2)
    catalog().stopDefinitionPolling()
  })
})
