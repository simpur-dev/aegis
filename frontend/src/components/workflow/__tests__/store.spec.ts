import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type {
  InstanceDetail,
  InstanceNodeRun,
  NodeTypesResponse,
  WorkflowClient,
} from '@/api/workflow'
import { createNodeDef } from '@/components/workflow/registry'
import { createWorkflowStore } from '@/stores/workflow'
import { defToGraph, type InstanceStatus, type NodeState } from '@/utils/graph'

function nodeRun(nodeId: string, state: NodeState, overrides: Partial<InstanceNodeRun> = {}): InstanceNodeRun {
  return {
    node_id: nodeId,
    type: 'data_fetch',
    state,
    attempts: 1,
    schedule_latency_ms: 3,
    duration_ms: 12,
    output: {},
    error: null,
    notes: [],
    ...overrides,
  }
}

function detail(status: InstanceStatus = 'running', runs: InstanceNodeRun[] = []): InstanceDetail {
  return {
    instance_id: 'wfi_0123456789ab',
    workflow_id: 'wf_0123456789ab',
    workflow_version: 2,
    trace_id: 'trace-1',
    status,
    error: null,
    nodes: runs,
  }
}

/** 每个用例一套独立桩：断言"派发出去的方法与载荷"，不发网络。 */
function fakeClient(responses: Partial<Record<keyof WorkflowClient, unknown>> = {}): WorkflowClient {
  const ok = { count: 0, items: [] }
  return {
    nodeTypes: vi.fn(async () => responses.nodeTypes ?? ok),
    definitions: vi.fn(async () => responses.definitions ?? ok),
    createDefinition: vi.fn(async () => responses.createDefinition ?? { workflow_id: 'wf_new', name: '链路', version: 1 }),
    reviseDefinition: vi.fn(async () => responses.reviseDefinition ?? { workflow_id: 'wf_new', name: '链路', version: 2 }),
    archiveDefinition: vi.fn(async () => ({ workflow_id: 'wf_1', status: 'archived' })),
    startInstance: vi.fn(async () => detail()),
    instances: vi.fn(async () => ok),
    instance: vi.fn(async () => responses.instance ?? detail()),
    submitDecision: vi.fn(async () => detail('running', [nodeRun('review_1', 'succeeded')])),
    patchNodeConfig: vi.fn(async () => ({ node_id: 'fetch_1', config: { limit: 50 } })),
    insertNode: vi.fn(async () => ({ instance_id: 'wfi_0123456789ab', node_id: 'notify_9', after: 'fetch_1' })),
    bypassNode: vi.fn(async () => detail('running', [nodeRun('fetch_1', 'bypassed')])),
    abortInstance: vi.fn(async () => detail('aborted', [nodeRun('fetch_1', 'cancelled')])),
  } as unknown as WorkflowClient
}

function firstCall(client: WorkflowClient, method: keyof WorkflowClient): unknown[] {
  const mock = client[method] as unknown as { mock: { calls: unknown[][] } }
  return mock.mock.calls[0] ?? []
}

describe('stores/workflow 定义编辑', () => {
  let client: WorkflowClient

  beforeEach(() => {
    setActivePinia(createPinia())
    client = fakeClient({
      nodeTypes: {
        count: 2,
        items: [
          { type: 'data_fetch', description: '数据接入：按区域/指标拉取最新读数', required_config: [], optional_config: ['limit'] },
          { type: 'join', description: '并行汇聚：合并多路上游结果', required_config: ['upstream'], optional_config: [] },
        ],
      } satisfies NodeTypesResponse,
    })
  })

  async function freshStore() {
    const store = createWorkflowStore(client)()
    store.resetDefinition('泥石流防控链路')
    return store
  }

  it('载入节点面板并按类型索引描述', async () => {
    const store = await freshStore()
    await store.loadNodeTypes()
    expect(store.nodeTypes).toHaveLength(2)
    expect(store.descriptionByType.data_fetch).toContain('数据接入')
  })

  it('空白画布即被本地校验拦住（至少一个节点）', async () => {
    const store = await freshStore()
    expect(store.validationErrors).toContain('工作流至少需要一个节点')
    expect(store.canSave).toBe(false)
    expect(await store.saveDefinition()).toBe(false)
    expect(client.createDefinition).not.toHaveBeenCalled()
  })

  it('选中节点后补丁只落到该节点', async () => {
    const store = await freshStore()
    store.addNode(createNodeDef('data_fetch', 'fetch_1'))
    store.addNode(createNodeDef('join', 'join_1'))
    store.select('join_1')
    expect(store.selectedNode?.type).toBe('join')
    store.patchSelected({ config: { upstream: ['fetch_1'] }, sla_ms: 9_000, timeout_ms: 9_000 })
    expect(store.current?.nodes.find((item) => item.node_id === 'fetch_1')?.config).toEqual(
      createNodeDef('data_fetch', 'x').config,
    )
    expect(store.current?.nodes.find((item) => item.node_id === 'join_1')?.sla_ms).toBe(9_000)
    expect(store.validationErrors).toEqual([])
  })

  it('首次保存走创建，二次保存走修订', async () => {
    const store = await freshStore()
    store.addNode(createNodeDef('data_fetch', 'fetch_1'))
    expect(await store.saveDefinition()).toBe(true)
    expect(client.createDefinition).toHaveBeenCalledTimes(1)
    expect(store.current?.workflow_id).toBe('wf_new')
    await store.saveDefinition()
    expect(client.reviseDefinition).toHaveBeenCalledWith('wf_new', expect.objectContaining({}))
    expect(store.current?.version).toBe(2)
  })

  it('保存载荷丢弃后端未开放的 retry 字段', async () => {
    const store = await freshStore()
    const created = createNodeDef('data_fetch', 'fetch_1')
    store.addNode({ ...created, retry: { max_attempts: 3, backoff_ms: 900 } })
    await store.saveDefinition()
    const [payload] = firstCall(client, 'createDefinition') as [{ nodes: Record<string, unknown>[] }]
    expect('retry' in (payload.nodes[0] ?? {})).toBe(false)
    expect(store.selectedNode?.retry.max_attempts).toBe(3)
  })

  it('传入画布视图时以视图为准（逆映射）', async () => {
    const store = await freshStore()
    store.addNode(createNodeDef('data_fetch', 'fetch_1'))
    store.addNode(createNodeDef('notify', 'notify_1'))
    const def = store.current
    if (def === null) throw new Error('画布未初始化')
    // 只提交视图里的第一个节点：验证保存路径确实以画布视图为准
    await store.saveDefinition({ nodes: defToGraph(def).nodes.slice(0, 1), edges: [] })
    const [payload] = firstCall(client, 'createDefinition') as [{ nodes: unknown[] }]
    expect(payload.nodes).toHaveLength(1)
  })

  it('环路在提交前被拦下并给出中文原因', async () => {
    const store = await freshStore()
    store.addNode(createNodeDef('data_fetch', 'fetch_1'))
    store.addNode(createNodeDef('notify', 'notify_1'))
    expect(store.connect('fetch_1', 'notify_1')).toBe(true)
    expect(store.connect('notify_1', 'fetch_1')).toBe(true)
    expect(store.canSave).toBe(false)
    expect(await store.saveDefinition()).toBe(false)
    expect(store.error ?? '').toContain('工作流图存在环')
    expect(client.createDefinition).not.toHaveBeenCalled()
  })

  it('自环与重复连线被画布直接丢弃', async () => {
    const store = await freshStore()
    store.addNode(createNodeDef('data_fetch', 'fetch_1'))
    store.addNode(createNodeDef('notify', 'notify_1'))
    expect(store.connect('fetch_1', 'fetch_1')).toBe(false)
    expect(store.connect('fetch_1', 'notify_1')).toBe(true)
    expect(store.connect('fetch_1', 'notify_1')).toBe(false)
    expect(store.current?.edges).toHaveLength(1)
  })

  it('join 的 upstream 可按连线回填，单值型上游取首条', async () => {
    const store = await freshStore()
    store.addNode(createNodeDef('data_fetch', 'fetch_1'))
    store.addNode(createNodeDef('join', 'join_1'))
    store.connect('fetch_1', 'join_1')
    store.select('join_1')
    store.syncUpstreamConfig('join_1')
    expect(store.selectedNode?.config.upstream).toEqual(['fetch_1'])
  })

  it('删除节点级联删除其连线', async () => {
    const store = await freshStore()
    store.addNode(createNodeDef('data_fetch', 'fetch_1'))
    store.addNode(createNodeDef('notify', 'notify_1'))
    store.connect('fetch_1', 'notify_1')
    store.select('fetch_1')
    store.removeSelected()
    expect(store.current?.nodes.map((item) => item.node_id)).toEqual(['notify_1'])
    expect(store.current?.edges).toEqual([])
  })
})

describe('stores/workflow 运行中操作', () => {
  let client: WorkflowClient

  beforeEach(() => {
    setActivePinia(createPinia())
  })

  async function storeWithInstance(runs: InstanceNodeRun[], status: InstanceStatus = 'running') {
    client = fakeClient({ instance: detail(status, runs) })
    const store = createWorkflowStore(client)()
    store.resetDefinition('链路')
    store.addNode(createNodeDef('data_fetch', 'fetch_1'))
    store.addNode(createNodeDef('human_review', 'review_1'))
    store.patchSelected({ config: { prompt: '请确认', options: ['approve', 'reject'] } })
    await store.focusInstance('wfi_0123456789ab')
    return store
  }

  it('未保存定义时不启动实例', async () => {
    const store = createWorkflowStore(fakeClient())()
    expect(await store.startInstance()).toBe(false)
    expect(store.error).toContain('请先保存')
  })

  it('启动实例后按已保存的 workflow_id 起轮询', async () => {
    vi.useFakeTimers()
    client = fakeClient({ instance: detail('running', [nodeRun('fetch_1', 'running')]) })
    const store = createWorkflowStore(client)()
    store.resetDefinition('链路')
    store.addNode(createNodeDef('data_fetch', 'fetch_1'))
    await store.saveDefinition()
    await store.startInstance()
    expect(firstCall(client, 'startInstance')[0]).toEqual({ workflow_id: 'wf_new', payload: {} })
    expect(store.polling).toBe(true)
    const callsAfterStart = vi.mocked(client.instance).mock.calls.length
    await vi.advanceTimersByTimeAsync(1_500)
    expect(vi.mocked(client.instance).mock.calls.length).toBeGreaterThan(callsAfterStart)
    store.stopPolling()
    expect(store.polling).toBe(false)
    vi.useRealTimers()
  })

  it('实例进入终态后自动停止轮询', async () => {
    vi.useFakeTimers()
    const store = createWorkflowStore(fakeClient({ instance: detail('succeeded', []) }))()
    store.resetDefinition('链路')
    await store.focusInstance('wfi_0123456789ab')
    expect(store.polling).toBe(true)
    expect(store.instanceMutable).toBe(false)
    await vi.advanceTimersByTimeAsync(1_500)
    expect(store.polling).toBe(false)
    vi.useRealTimers()
  })

  it('改参只在 pending/ready 节点上开放，且会重拉快照', async () => {
    const store = await storeWithInstance([nodeRun('fetch_1', 'pending'), nodeRun('review_1', 'awaiting_human')])
    expect(store.canPatchRuntime('fetch_1')).toBe(true)
    expect(await store.patchRuntimeConfig('fetch_1', { limit: 50 })).toBe(true)
    expect(firstCall(client, 'patchNodeConfig')).toEqual(['wfi_0123456789ab', 'fetch_1', { limit: 50 }])
    // 改参接口不返回实例快照，必须再拉一次
    expect(vi.mocked(client.instance).mock.calls.length).toBe(2)
  })

  it('已执行的节点不可改参（engine.py:539）', async () => {
    const store = await storeWithInstance([nodeRun('fetch_1', 'succeeded')])
    expect(store.canPatchRuntime('fetch_1')).toBe(false)
    expect(store.canBypassNode('fetch_1')).toBe(false)
  })

  it('旁路对 awaiting_human 开放，成功后状态刷新为 bypassed（engine.py:582）', async () => {
    const store = await storeWithInstance([nodeRun('review_1', 'awaiting_human')])
    store.select('review_1')
    expect(store.canBypassNode('review_1')).toBe(true)
    expect(store.canDecideNode('review_1')).toBe(true)
    expect(await store.bypassNode('review_1', '现场已处置')).toBe(true)
    expect(firstCall(client, 'bypassNode')).toEqual(['wfi_0123456789ab', 'review_1', '现场已处置'])
    expect(store.nodeStateOf('fetch_1')).toBe('bypassed')
  })

  it('核签候选来自定义的 options，提交后走 decision 接口', async () => {
    const store = await storeWithInstance([nodeRun('review_1', 'awaiting_human')])
    expect(store.decisionOptions('review_1')).toEqual(['approve', 'reject'])
    expect(await store.submitDecision('review_1', 'approve', '同意')).toBe(true)
    expect(firstCall(client, 'submitDecision')).toEqual([
      'wfi_0123456789ab',
      'review_1',
      { choice: 'approve', by: '值班指挥员', comment: '同意' },
    ])
    expect(store.nodeStateOf('review_1')).toBe('succeeded')
    expect(store.awaitingNodes).toEqual([])
  })

  it('非 awaiting_human 节点不可核签（engine.py:515）', async () => {
    const store = await storeWithInstance([nodeRun('review_1', 'pending')])
    expect(store.canDecideNode('review_1')).toBe(false)
    expect(store.awaitingNodes).toEqual([])
  })

  it('插入节点走 insert 接口并重拉实例快照（engine.py:569 不回快照）', async () => {
    const store = await storeWithInstance([nodeRun('fetch_1', 'succeeded')])
    await store.insertRuntimeNode('fetch_1', createNodeDef('notify', 'notify_9'))
    const [instanceId, payload] = firstCall(client, 'insertNode') as [string, { after: string; node: Record<string, unknown> }]
    expect(instanceId).toBe('wfi_0123456789ab')
    expect(payload.after).toBe('fetch_1')
    expect(payload.node.node_id).toBe('notify_9')
    expect('retry' in payload.node).toBe(false)
    expect(vi.mocked(client.instance).mock.calls.length).toBe(2)
  })

  it('中止走 abort 接口并停止轮询', async () => {
    vi.useFakeTimers()
    const store = await storeWithInstance([nodeRun('fetch_1', 'running')])
    expect(await store.abortInstance('上游数据中断')).toBe(true)
    expect(firstCall(client, 'abortInstance')).toEqual(['wfi_0123456789ab', '上游数据中断'])
    expect(store.polling).toBe(false)
    expect(store.instanceStatus).toBe('aborted')
    vi.useRealTimers()
  })

  it('400 错误还原成 detail 中文提示', async () => {
    const store = await storeWithInstance([nodeRun('review_1', 'awaiting_human')])
    vi.mocked(client.bypassNode).mockRejectedValueOnce(
      Object.assign(new Error('Request failed'), { status: 400, detail: { detail: '节点 review_1 状态 succeeded 不可旁路' } }),
    )
    expect(await store.bypassNode('review_1')).toBe(false)
    expect(store.error).toContain('状态 succeeded 不可旁路')
  })
})
