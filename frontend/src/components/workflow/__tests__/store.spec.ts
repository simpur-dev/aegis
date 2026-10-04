import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { WorkflowApiError, type InstanceDetail, type InstanceNodeRun, type NodeTypesResponse, type WorkflowClient } from '@/api/workflow'
import { createNodeDef } from '@/components/workflow/registry'
import { createWorkflowStore } from '@/stores/workflow'
import { defToGraph, type InstanceStatus, type NodeState, type WorkflowDef } from '@/utils/graph'

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

/** 实例所绑定定义的前端视图：focusInstance 要把它取回画布，桩必须给得出。 */
function workflowDef(): WorkflowDef {
  return {
    workflow_id: 'wf_0123456789ab',
    name: '已存链路',
    description: '',
    version: 2,
    nodes: [createNodeDef('data_fetch', 'fetch_1'), createNodeDef('human_review', 'review_1')],
    edges: [{ source: 'fetch_1', target: 'review_1', condition: 'always' }],
    created_by: 'operator',
    status: 'active',
  }
}

/**
 * 每个用例一套独立桩：断言"派发出去的方法与载荷"，不发网络。
 *
 * 桩的键集用 `Record<keyof WorkflowClient, unknown>` 声明，客户端加方法而这里没补桩
 * 就是编译错误。此前 `definition` 漏过一次：focusInstance 取定义时 TypeError，
 * 被 runAction 咽成一条 error 文案，页面只表现为"画布空白、核签点不开"。
 */
function fakeClient(responses: Partial<Record<keyof WorkflowClient, unknown>> = {}): WorkflowClient {
  const ok = { count: 0, items: [] }
  const defaults: Record<keyof WorkflowClient, unknown> = {
    nodeTypes: ok,
    definitions: ok,
    definition: workflowDef(),
    createDefinition: { workflow_id: 'wf_new', name: '链路', version: 1 },
    reviseDefinition: { workflow_id: 'wf_new', name: '链路', version: 2 },
    archiveDefinition: { workflow_id: 'wf_1', status: 'archived' },
    restoreDefinition: { workflow_id: 'wf_1', status: 'active' },
    startInstance: detail(),
    instances: ok,
    instance: detail(),
    submitDecision: detail('running', [nodeRun('review_1', 'succeeded')]),
    patchNodeConfig: { node_id: 'fetch_1', config: { limit: 50 } },
    insertNode: { instance_id: 'wfi_0123456789ab', node_id: 'notify_9', after: 'fetch_1' },
    bypassNode: detail('running', [nodeRun('fetch_1', 'bypassed')]),
    abortInstance: detail('aborted', [nodeRun('fetch_1', 'cancelled')]),
  }
  const stubs = {} as Record<keyof WorkflowClient, unknown>
  for (const method of Object.keys(defaults) as (keyof WorkflowClient)[]) {
    stubs[method] = vi.fn(async () => responses[method] ?? defaults[method])
  }
  return stubs as unknown as WorkflowClient
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

  it('保存载荷带上 retry：打开→原样保存不会把策略抹回默认值', async () => {
    const store = await freshStore()
    const created = createNodeDef('data_fetch', 'fetch_1')
    store.addNode({ ...created, retry: { max_attempts: 3, backoff_ms: 900 } })
    await store.saveDefinition()
    const [payload] = firstCall(client, 'createDefinition') as [{ nodes: Record<string, unknown>[] }]
    expect((payload.nodes[0] ?? {}).retry).toEqual({ max_attempts: 3, backoff_ms: 900 })
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

  /**
   * 列表接口只给计数（workflow_api.py:113-130），画布能存能归档却打不开任何一张图，
   * 这一条把"打开"钉住：取回定义、立起标题、给每个节点落位、选中首个节点。
   */
  it('打开已存定义：取回全文并铺成可编辑的画布', async () => {
    const store = await freshStore()
    expect(await store.openDefinition('wf_0123456789ab')).toBe(true)
    expect(firstCall(client, 'definition')).toEqual(['wf_0123456789ab'])
    expect(store.current?.name).toBe('已存链路')
    expect(store.current?.workflow_id).toBe('wf_0123456789ab')
    expect(Object.keys(store.positions).sort()).toEqual(['fetch_1', 'review_1'])
    expect(store.selectedNodeId).toBe('fetch_1')
    expect(store.error).toBeNull()
  })

  it('打开之后仍可修订：workflow_id 已就位，保存走 revise 而不是 create', async () => {
    const store = await freshStore()
    await store.openDefinition('wf_0123456789ab')
    store.addNode(createNodeDef('notify', 'notify_9'))
    expect(await store.saveDefinition()).toBe(true)
    expect(client.createDefinition).not.toHaveBeenCalled()
    expect(firstCall(client, 'reviseDefinition')[0]).toBe('wf_0123456789ab')
  })

  it('打开不存在的定义：报后端原因，画布保持原样', async () => {
    const store = await freshStore()
    vi.mocked(client.definition).mockRejectedValueOnce(new Error('HTTP 404 未找到该工作流定义'))
    expect(await store.openDefinition('wf_missing')).toBe(false)
    expect(store.current?.name).toBe('泥石流防控链路')
    expect(store.error).toContain('404')
  })

  it('打开定义时停掉上一张图的轮询', async () => {
    vi.useFakeTimers()
    const running = createWorkflowStore(fakeClient({ instance: detail('running', [nodeRun('fetch_1', 'running')]) }))()
    running.resetDefinition('链路')
    await running.focusInstance('wfi_0123456789ab')
    expect(running.polling).toBe(true)
    expect(await running.openDefinition('wf_0123456789ab')).toBe(true)
    expect(running.polling).toBe(false)
    await vi.advanceTimersByTimeAsync(3_000)
    expect(running.polling).toBe(false)
    vi.useRealTimers()
  })

  it('422 的校验数组也要读得懂，不许漏出 [object Object]', () => {
    const store = createWorkflowStore(fakeClient())()
    const error = new WorkflowApiError(422, 'Request failed with status code 422', {
      detail: [{ loc: ['body', 'name'], msg: 'String should have at least 1 character', type: 'string_too_short' }],
    })
    expect(store.describeFailure(error)).toBe('HTTP 422：流程名称（name）：String should have at least 1 character')
  })

  /**
   * 超时、连不上这类错误没有响应体，客户端构造时已经把 HTTP 码写进 message 了。
   * store 再拼一次 `HTTP ${status}：` 就是"HTTP 0：接口调用失败（HTTP 0）：…"——
   * 一句错误里两个码，读的人会以为是两次故障。
   */
  it('detail 缺失时直接用客户端那句，不再自己拼一遍 HTTP 码', () => {
    const store = createWorkflowStore(fakeClient())()
    const error = new WorkflowApiError(0, '接口调用失败（HTTP 0）：timeout of 20000ms exceeded', undefined)
    expect(store.describeFailure(error)).toBe('接口调用失败（HTTP 0）：timeout of 20000ms exceeded')
  })

  it('有 detail 时码与原因分开写，原因取后端原文', () => {
    const store = createWorkflowStore(fakeClient())()
    const error = new WorkflowApiError(400, 'Request failed with status code 400', { detail: '工作流图存在环' })
    expect(store.describeFailure(error)).toBe('HTTP 400：工作流图存在环')
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
    // 画布上的两个节点来自服务端定义（focusInstance 取回），本地草稿只负责把标题立起来
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

  /**
   * 聚焦实例必须把"这条实例绑定的定义"一起载进画布。
   *
   * 真机复现的缺陷：运行区已显示"待人工核签：review"，画布仍是 0 节点、
   * 标题"未命名防控链路"——选不到节点就打不开决策面板，值班员签不了这张工单。
   */
  it('聚焦实例时按 workflow_id 取回定义并铺到画布上', async () => {
    client = fakeClient({
      instance: detail('waiting', [nodeRun('fetch_1', 'succeeded'), nodeRun('review_1', 'awaiting_human')]),
    })
    const store = createWorkflowStore(client)()
    store.resetDefinition('草稿')
    expect(await store.focusInstance('wfi_0123456789ab')).toBe(true)
    expect(firstCall(client, 'definition')).toEqual(['wf_0123456789ab'])
    expect(store.current?.name).toBe('已存链路')
    expect(store.current?.nodes.map((node) => node.node_id)).toEqual(['fetch_1', 'review_1'])
    expect(Object.keys(store.positions).sort()).toEqual(['fetch_1', 'review_1'])
    expect(store.nodeStateOf('review_1')).toBe('awaiting_human')
    expect(store.error).toBeNull()
  })

  it('聚焦实例时自动选中待核签节点，决策面板随之可用', async () => {
    client = fakeClient({
      instance: detail('waiting', [nodeRun('fetch_1', 'succeeded'), nodeRun('review_1', 'awaiting_human')]),
    })
    const store = createWorkflowStore(client)()
    store.resetDefinition('草稿')
    await store.focusInstance('wfi_0123456789ab')
    expect(store.selectedNodeId).toBe('review_1')
    expect(store.canDecideNode('review_1')).toBe(true)
    expect(store.decisionOptions('review_1')).toEqual(['approve', 'reject'])
    expect(await store.submitDecision('review_1', 'approve', '同意')).toBe(true)
  })

  it('无待核签节点时选中第一个，不至于选空', async () => {
    client = fakeClient({ instance: detail('running', [nodeRun('fetch_1', 'running')]) })
    const store = createWorkflowStore(client)()
    store.resetDefinition('草稿')
    await store.focusInstance('wfi_0123456789ab')
    expect(store.selectedNodeId).toBe('fetch_1')
  })

  it('取不回定义时报错而不是留下空白画布', async () => {
    client = fakeClient({ instance: detail('waiting', [nodeRun('review_1', 'awaiting_human')]) })
    vi.mocked(client.definition).mockRejectedValueOnce(new Error('HTTP 404 no such definition'))
    const store = createWorkflowStore(client)()
    store.resetDefinition('草稿')
    expect(await store.focusInstance('wfi_0123456789ab')).toBe(false)
    expect(store.current?.name).toBe('草稿')
    expect(store.error).toContain('404')
    expect(store.polling).toBe(false)
  })

  it('改参只在 pending/ready 节点上开放，且会重拉快照', async () => {
    const store = await storeWithInstance([nodeRun('fetch_1', 'pending'), nodeRun('review_1', 'awaiting_human')])
    expect(store.canPatchRuntime('fetch_1')).toBe(true)
    expect(await store.patchRuntimeConfig('fetch_1', { limit: 50 })).toBe(true)
    expect(firstCall(client, 'patchNodeConfig')).toEqual(['wfi_0123456789ab', 'fetch_1', { limit: 50 }])
    // 改参接口不返回实例快照，必须再拉一次
    expect(vi.mocked(client.instance).mock.calls.length).toBe(2)
  })

  /**
   * 界面上"清空那一格"等于这个键不再进载荷，而引擎是按键合并的：旧值原地留着、
   * HTTP 200、画布显示空——三件事凑一起就是"看着改成功了"。
   * 不说出来，值班员以为通知正文清掉了，它照旧发出去。
   */
  it('清空一格不会真的删掉旧值，页面必须说出来', async () => {
    const store = await storeWithInstance([nodeRun('fetch_1', 'pending')])
    expect(await store.patchRuntimeConfig('fetch_1', {})).toBe(true)
    expect(store.error, '服务端回的是 {limit:50}，而这次没发 limit —— 旧值被合并保留了').toContain('limit')
    expect(store.error).toContain('服务端仍是原值')
  })

  it('要改的键都发全时不多嘴', async () => {
    const store = await storeWithInstance([nodeRun('fetch_1', 'pending')])
    expect(await store.patchRuntimeConfig('fetch_1', { limit: 50 })).toBe(true)
    expect(store.error).toBeNull()
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
    expect(await store.submitDecision('review_1', 'approve', '同意', '巡护员扎西')).toBe(true)
    expect(firstCall(client, 'submitDecision')).toEqual([
      'wfi_0123456789ab',
      'review_1',
      { choice: 'approve', by: '巡护员扎西', comment: '同意' },
    ])
    expect(store.nodeStateOf('review_1')).toBe('succeeded')
    expect(store.awaitingNodes).toEqual([])
  })

  /**
   * 平台没有登录态：签字人只能由签字的人自己写。
   * 此前前端把 `by` 固定写成"值班指挥员"——等于代码替所有人签名，台账上那句"谁签的"从来不是事实。
   * 不填就**不发这个键**，让后端按它的默认记成 unknown。
   */
  it('没填签字人时不发 by 键，由后端记成 unknown', async () => {
    const store = await storeWithInstance([nodeRun('review_1', 'awaiting_human')])
    expect(await store.submitDecision('review_1', 'reject', '现场已核实无险情')).toBe(true)
    expect(firstCall(client, 'submitDecision')).toEqual([
      'wfi_0123456789ab',
      'review_1',
      { choice: 'reject', comment: '现场已核实无险情' },
    ])
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
    expect(payload.node.retry).toBeDefined()
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

/**
 * 面板小标题"等人签 N 张"数的是实例列表（RuntimePanel 的 waitingRows），
 * 而运行中操作此前只更新当前那一条的详情——列表没人管。
 *
 * 真机量到的一轮：新实例接口上 `waiting` 为 1，面板写"等人签 1 张"；点「核签通过」后
 * 2.5 秒、再过 6 秒，接口已经是 0，面板还写"等人签 1 张"；连顶栏「刷新实例」点下去
 * 都没变（那颗按钮只重拉详情）。数错张数不是难看而已——值班员会按这个数判断还剩几张。
 */
describe('stores/workflow 实例列表与「等人签」计数对账', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  function rows(count: number) {
    return {
      count,
      items: Array.from({ length: count }, (_, index) => ({
        ...detail('waiting', [nodeRun('review_1', 'awaiting_human')]),
        instance_id: `wfi_0000000000${index}`,
      })),
    }
  }

  /** 先把列表种成"有 1 张等人签"，再把服务端换成"签完了"：只有真的重拉才看得到差别。 */
  async function storeShowingOneTicket(client: WorkflowClient) {
    vi.mocked(client.instances).mockResolvedValue(rows(1))
    const store = createWorkflowStore(client)()
    store.resetDefinition('草稿')
    await store.focusInstance('wfi_0123456789ab')
    await store.loadInstances()
    expect(store.instances.filter((row) => row.status === 'waiting')).toHaveLength(1)
    vi.mocked(client.instances).mockResolvedValue(rows(0))
    return store
  }

  it('签掉一张工单后把列表拉一遍，小标题的张数才会降下来', async () => {
    const client = fakeClient({
      instance: detail('waiting', [nodeRun('review_1', 'awaiting_human')]),
      submitDecision: detail('running', [nodeRun('review_1', 'succeeded')]),
    })
    const store = await storeShowingOneTicket(client)
    expect(await store.submitDecision('review_1', 'approve', '同意', '值班指挥员')).toBe(true)
    expect(store.instances.filter((row) => row.status === 'waiting'), '签完还留着那张，面板就一直写「等人签 1 张」').toHaveLength(0)
  })

  it('绕过之后重拉列表', async () => {
    const bypassStore = await storeShowingOneTicket(
      fakeClient({ instance: detail('waiting', [nodeRun('review_1', 'awaiting_human')]) }),
    )
    expect(await bypassStore.bypassNode('review_1', '现场已处置')).toBe(true)
    expect(bypassStore.instances.filter((row) => row.status === 'waiting')).toHaveLength(0)
  })

  it('中止之后重拉列表', async () => {
    const abortStore = await storeShowingOneTicket(
      fakeClient({ instance: detail('waiting', [nodeRun('review_1', 'awaiting_human')]) }),
    )
    expect(await abortStore.abortInstance('上游数据中断')).toBe(true)
    expect(abortStore.instances.filter((row) => row.status === 'waiting')).toHaveLength(0)
  })

  it('顶栏「刷新实例」把这一页的实例信息都对新：详情和列表一起拉', async () => {
    const client = fakeClient({ instance: detail('waiting', [nodeRun('review_1', 'awaiting_human')]) })
    const store = await storeShowingOneTicket(client)
    const detailCalls = vi.mocked(client.instance).mock.calls.length
    expect(await store.refreshAll()).toBe(true)
    expect(vi.mocked(client.instance).mock.calls.length).toBe(detailCalls + 1)
    expect(store.instances.filter((row) => row.status === 'waiting'), '按钮点下去张数还不变，就等于这个按钮没用').toHaveLength(0)
  })

  it('轮询发现这条进了终态才重拉列表，还挂在人工这步时不多打一次接口', async () => {
    vi.useFakeTimers()
    const client = fakeClient({ instance: detail('waiting', [nodeRun('review_1', 'awaiting_human')]) })
    const store = createWorkflowStore(client)()
    store.resetDefinition('草稿')
    await store.focusInstance('wfi_0123456789ab')
    await vi.advanceTimersByTimeAsync(1_500)
    expect(vi.mocked(client.instances).mock.calls.length, '实例还等着人签，列表没有变化，不该白打一次').toBe(0)
    vi.mocked(client.instance).mockResolvedValue(detail('succeeded', []))
    await vi.advanceTimersByTimeAsync(1_500)
    expect(vi.mocked(client.instances).mock.calls.length).toBe(1)
    vi.useRealTimers()
  })

  it('后端给了前端没映射的状态时不崩，也不当成终态去重拉', async () => {
    vi.useFakeTimers()
    const client = fakeClient({ instance: detail('waiting', [nodeRun('review_1', 'awaiting_human')]) })
    const store = createWorkflowStore(client)()
    store.resetDefinition('草稿')
    await store.focusInstance('wfi_0123456789ab')
    vi.mocked(client.instance).mockResolvedValue({ ...detail('waiting', []), status: 'paused_by_operator' })
    await vi.advanceTimersByTimeAsync(1_500)
    expect(store.instanceStatus).toBeNull()
    expect(store.error).toBeNull()
    expect(vi.mocked(client.instances).mock.calls.length).toBe(0)
    vi.useRealTimers()
  })

  it('列表没拉回来不能报成「核签失败」：单子已经签出去了，说反了会让人重签一次', async () => {
    const client = fakeClient({
      instance: detail('waiting', [nodeRun('review_1', 'awaiting_human')]),
      submitDecision: detail('running', [nodeRun('review_1', 'succeeded')]),
    })
    vi.mocked(client.instances).mockRejectedValue(new Error('接口调用失败（HTTP 503）：upstream unavailable'))
    const store = createWorkflowStore(client)()
    store.resetDefinition('草稿')
    await store.focusInstance('wfi_0123456789ab')
    expect(await store.submitDecision('review_1', 'approve', '同意', '值班指挥员')).toBe(true)
    expect(store.instance?.status).toBe('running')
    expect(store.error).toContain('实例列表没刷新')
    expect(store.error, '把列表的失败说成核签的失败，是凭空多出来的一句谎').not.toContain('核签失败')
  })

  function gate<T>() {
    let resolve!: (value: T) => void
    const promise = new Promise<T>((done) => {
      resolve = done
    })
    return { promise, resolve }
  }

  it('页面开着不动，队列自查也要把别人开出来的新工单带进来', async () => {
    vi.useFakeTimers()
    try {
      const client = fakeClient()
      vi.mocked(client.instances).mockResolvedValue(rows(0))
      const store = createWorkflowStore(client)()
      store.resetDefinition('草稿')
      await store.loadInstances()
      expect(store.instances).toHaveLength(0)

      // 另一条路（人工上报、助手）在这期间开出了两张
      vi.mocked(client.instances).mockResolvedValue(rows(2))
      store.startQueuePolling()
      const callsBefore = vi.mocked(client.instances).mock.calls.length
      await vi.advanceTimersByTimeAsync(15_000)

      expect(vi.mocked(client.instances).mock.calls.length, '定时器没重拉列表，这一页就停在开页那一刻').toBe(callsBefore + 1)
      expect(store.instances).toHaveLength(2)
      expect(store.instancesUpdatedAt, '自己刷了却没写时间，读的人仍不知道这是几点的账').not.toBeNull()
      store.stopQueuePolling()
    } finally {
      vi.useRealTimers()
    }
  })

  it('页签在后台时队列自查不打接口', async () => {
    vi.useFakeTimers()
    const descriptor = Object.getOwnPropertyDescriptor(document, 'visibilityState')
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' })
    try {
      const client = fakeClient()
      const store = createWorkflowStore(client)()
      store.resetDefinition('草稿')
      await store.loadInstances()
      const calls = vi.mocked(client.instances).mock.calls.length
      store.startQueuePolling()
      await vi.advanceTimersByTimeAsync(45_000)
      expect(vi.mocked(client.instances).mock.calls.length, '切走的页签不该继续打接口').toBe(calls)
      store.stopQueuePolling()
    } finally {
      if (descriptor === undefined) delete (document as { visibilityState?: unknown }).visibilityState
      else Object.defineProperty(document, 'visibilityState', descriptor)
      vi.useRealTimers()
    }
  })

  it('先发后回的旧快照不许把队列数字往回带', async () => {
    const client = fakeClient()
    const slow = gate<ReturnType<typeof rows>>()
    const fast = gate<ReturnType<typeof rows>>()
    vi.mocked(client.instances).mockReturnValueOnce(slow.promise).mockReturnValueOnce(fast.promise)
    const store = createWorkflowStore(client)()
    store.resetDefinition('草稿')

    const first = store.loadInstances()
    await Promise.resolve()
    const second = store.loadInstances()
    fast.resolve(rows(3))
    await second
    expect(store.instances).toHaveLength(3)

    slow.resolve(rows(1))
    await first
    expect(store.instances, '后到的那份更旧，队列会凭空少两张').toHaveLength(3)
    expect(store.instancesUpdatedAt).not.toBeNull()
  })
})

/**
 * "有没有未保存改动"必须是个算得出来的事实。
 *
 * 真机上"新建画布"和"打开已存定义"都是直接覆盖画布：摆了五个节点还没保存，
 * 一误触就全没了，页面只轻描淡写说一句"已新建空白画布"。要拦住这种丢法，
 * 前提是 store 能回答"现在这份跟服务端一致吗"。
 */
describe('stores/workflow 未保存改动', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('空白画布不算未保存：它本来就什么都没有', () => {
    const store = createWorkflowStore(fakeClient())()
    store.resetDefinition('链路')
    expect(store.isDirty).toBe(false)
  })

  it('加一个节点就算脏，保存之后回到干净', async () => {
    const store = createWorkflowStore(fakeClient())()
    store.resetDefinition('链路')
    store.addNode(createNodeDef('data_fetch', 'fetch_1'))
    expect(store.isDirty).toBe(true)
    expect(await store.saveDefinition()).toBe(true)
    expect(store.isDirty).toBe(false)
    store.addNode(createNodeDef('notify', 'notify_1'))
    expect(store.isDirty, '存完又改了，还得算脏').toBe(true)
  })

  it('拖动节点改坐标也算脏：摆好的位置同样是劳动', () => {
    const store = createWorkflowStore(fakeClient())()
    store.resetDefinition('链路')
    store.addNode(createNodeDef('data_fetch', 'fetch_1'))
    store.saveDefinition()
    store.moveNode('fetch_1', { x: 900, y: 40 })
    expect(store.isDirty).toBe(true)
  })

  it('打开已存定义与聚焦实例都把画布换成服务端内容，因而不算脏', async () => {
    const store = createWorkflowStore(fakeClient())()
    store.resetDefinition('链路')
    expect(await store.openDefinition('wf_0123456789ab')).toBe(true)
    expect(store.isDirty).toBe(false)
    store.patchSelected({ name: '改个名' })
    expect(store.isDirty).toBe(true)
    expect(await store.focusInstance('wfi_0123456789ab')).toBe(true)
    expect(store.isDirty, '聚焦实例载入的也是服务端那一份').toBe(false)
  })

  it('保存失败不算"已同步"：脏状态必须留着提醒', async () => {
    const client = fakeClient()
    vi.mocked(client.createDefinition).mockRejectedValueOnce(new Error('HTTP 500 存不下'))
    const store = createWorkflowStore(client)()
    store.resetDefinition('链路')
    store.addNode(createNodeDef('data_fetch', 'fetch_1'))
    expect(await store.saveDefinition()).toBe(false)
    expect(store.isDirty).toBe(true)
  })

  /**
   * 归档/取消归档改的是服务端那份定义的 status，画布上正开着同一条时要跟着改，
   * 否则标题旁继续写"启用中"、列表里却已经是归档的那一版。
   * 状态不是值班员改的，不能顺手打上"有未保存改动"（那会弹刷新拦截）。
   */
  it('归档把画布上那条的状态改过来，但不把它算成未保存改动', async () => {
    const store = createWorkflowStore(fakeClient())()
    expect(await store.openDefinition('wf_0123456789ab')).toBe(true)
    expect(store.current?.status).toBe('active')

    expect(await store.archiveDefinition('wf_0123456789ab')).toBe(true)
    expect(store.current?.status).toBe('archived')
    expect(store.isDirty, '状态是服务端改的，不该记在画布账上').toBe(false)

    expect(await store.restoreDefinition('wf_0123456789ab')).toBe(true)
    expect(store.current?.status).toBe('active')
    expect(store.isDirty).toBe(false)
  })

  it('画布本来就没存过改动时，归档不能把这份脏抹掉', async () => {
    const store = createWorkflowStore(fakeClient())()
    expect(await store.openDefinition('wf_0123456789ab')).toBe(true)
    store.patchSelected({ name: '改个名' })
    expect(store.isDirty).toBe(true)

    expect(await store.archiveDefinition('wf_0123456789ab')).toBe(true)
    expect(store.current?.status, '状态照样要跟过来').toBe('archived')
    expect(store.isDirty, '刚改的名字服务端还不知道，重打基线等于抹掉提醒').toBe(true)
  })

  it('归档别的定义时不动当前画布的状态', async () => {
    const store = createWorkflowStore(fakeClient())()
    expect(await store.openDefinition('wf_0123456789ab')).toBe(true)
    expect(await store.archiveDefinition('wf_另一条')).toBe(true)
    expect(store.current?.status).toBe('active')
  })
})

/**
 * 迟到的回写不许盖掉人正在看的那一条。
 *
 * 真机量到两条动线（`.tmp-verify/stale-write-live.mjs`，用 `page.route` 把某一趟响应人为拖 2 秒）：
 * ① 连开两条定义，先点的那条后到——画布标题停在「雪崩气象型预警与交通管控流程」、11 个节点，
 *    而人最后打开的是 4 节点那一条；
 * ② 等签实例的一次轮询慢了 2 秒，人在这 2 秒里改点另一条——面板实例号是 `wfi_ed2670e23f3a`，
 *    画布画的却是 `wfi_ac42219df5d8` 那一张，轮询还继续替不被看着的那条跑。
 * 第二种最要命：这时在画布上点一个节点提交决策，发出去的是"这条实例 + 那张图的节点号"。
 */
describe('stores/workflow 迟到的响应不许盖掉当前视图', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  function deferred<T>() {
    let resolve!: (value: T) => void
    const promise = new Promise<T>((r) => {
      resolve = r
    })
    return { promise, resolve }
  }

  function defNamed(name: string, workflowId: string): WorkflowDef {
    return {
      ...workflowDef(),
      workflow_id: workflowId,
      name,
      nodes: [createNodeDef('data_fetch', workflowId + '_a')],
      edges: [],
    }
  }

  function instanceNamed(instanceId: string, workflowId: string, status: InstanceStatus, runs: InstanceNodeRun[]): InstanceDetail {
    return { ...detail(status, runs), instance_id: instanceId, workflow_id: workflowId }
  }

  function snapshotOf(instanceId: string, status: InstanceStatus = 'waiting'): InstanceDetail {
    const workflowId = instanceId === 'wfi_a' ? 'wf_slow' : 'wf_fast'
    return instanceNamed(instanceId, workflowId, status, [nodeRun(workflowId + '_a', 'awaiting_human')])
  }

  /** 按 id 分派的桩：wfi_a 是"人先看的那条"，wfi_b 是"后来改点的那条"。 */
  function twoInstanceClient(): WorkflowClient {
    const client = fakeClient()
    vi.mocked(client.instance).mockImplementation((instanceId: string) => Promise.resolve(snapshotOf(instanceId)))
    vi.mocked(client.definition).mockImplementation((workflowId: string) =>
      Promise.resolve(defNamed(workflowId === 'wf_slow' ? '慢的那条' : '快的那条', workflowId)),
    )
    return client
  }

  it('连开两条定义：先点的那条响应后到，也不能把画布翻回它', async () => {
    const client = fakeClient()
    const gate = deferred<WorkflowDef>()
    vi.mocked(client.definition).mockImplementation((workflowId: string) =>
      workflowId === 'wf_slow' ? gate.promise : Promise.resolve(defNamed('快的那条', 'wf_fast')),
    )
    const store = createWorkflowStore(client)()
    store.resetDefinition('草稿')
    const slow = store.openDefinition('wf_slow')
    await Promise.resolve()
    expect(await store.openDefinition('wf_fast')).toBe(true)
    expect(store.current?.name).toBe('快的那条')
    gate.resolve(defNamed('慢的那条', 'wf_slow'))
    expect(await slow).toBe(true)
    expect(store.current?.name, '旧答案后到就丢掉：画布要留在人最后打开的那条').toBe('快的那条')
    expect(Object.keys(store.positions)).toEqual(['wf_fast_a'])
  })

  it('人已经新建空白画布：迟到的定义响应不许把图铺回去', async () => {
    const client = fakeClient()
    const gate = deferred<WorkflowDef>()
    vi.mocked(client.definition).mockImplementation(() => gate.promise)
    const store = createWorkflowStore(client)()
    store.resetDefinition('草稿')
    const opening = store.openDefinition('wf_slow')
    await Promise.resolve()
    store.resetDefinition('新草稿')
    gate.resolve(defNamed('慢的那条', 'wf_slow'))
    expect(await opening).toBe(true)
    expect(store.current?.name).toBe('新草稿')
    expect(store.current?.nodes).toHaveLength(0)
  })

  it('连点两条实例：慢的那条后到，面板号、画布标题、选中节点都留在最后点的那条', async () => {
    const held: Array<(value: InstanceDetail) => void> = []
    const client = twoInstanceClient()
    vi.mocked(client.instance).mockImplementation((instanceId: string) =>
      instanceId === 'wfi_a'
        ? new Promise<InstanceDetail>((resolve) => {
            held.push(resolve)
          })
        : Promise.resolve(snapshotOf('wfi_b')),
    )
    const store = createWorkflowStore(client)()
    store.resetDefinition('草稿')
    const slow = store.focusInstance('wfi_a')
    await Promise.resolve()
    expect(await store.focusInstance('wfi_b')).toBe(true)
    held.forEach((resolve) => resolve(snapshotOf('wfi_a', 'running')))
    expect(await slow).toBe(true)
    expect(store.instance?.instance_id, '面板不能翻回没在看的那条').toBe('wfi_b')
    expect(store.current?.name).toBe('快的那条')
    expect(store.selectedNodeId).toBe('wf_fast_a')
  })

  it('轮询在路上迟到了：不许把面板翻回上一条，轮询要跟着人新点的那条走', async () => {
    vi.useFakeTimers()
    const gate = deferred<InstanceDetail>()
    // 用对象装着这次挂住的轮询：TS 不会把闭包里写进去的属性窄化成 undefined，
    // 换成 let + null 就得靠一次 as 才能过编译。
    const held: { ref?: typeof gate } = {}
    let aCalls = 0
    const client = twoInstanceClient()
    vi.mocked(client.instance).mockImplementation((instanceId: string) => {
      // 第一次取 wfi_a 是"点开它"，要正常回；第二次起才是轮询，把那一次挂住。
      if (instanceId === 'wfi_a') {
        aCalls += 1
        if (aCalls >= 2 && held.ref === undefined) {
          held.ref = gate
          return gate.promise
        }
      }
      return Promise.resolve(snapshotOf(instanceId))
    })
    const store = createWorkflowStore(client)()
    store.resetDefinition('草稿')
    await store.focusInstance('wfi_a')
    expect(store.instance?.instance_id).toBe('wfi_a')
    expect(store.polling).toBe(true)
    await vi.advanceTimersByTimeAsync(1_500)
    expect(held.ref, '这一趟轮询该正卡在路上').toBeDefined()
    await store.focusInstance('wfi_b')
    held.ref?.resolve(snapshotOf('wfi_a', 'succeeded'))
    await vi.advanceTimersByTimeAsync(1)
    expect(store.instance?.instance_id, '迟到的轮询不许把面板翻回 wfi_a').toBe('wfi_b')
    await vi.advanceTimersByTimeAsync(1_500)
    expect(vi.mocked(client.instance).mock.calls.at(-1)?.[0], '轮询要替人正在看的这条跑').toBe('wfi_b')
    vi.useRealTimers()
  })

  it('提交核签的响应回来时人已切到别的实例：那份快照不盖到页面上', async () => {
    const client = twoInstanceClient()
    const gate = deferred<InstanceDetail>()
    vi.mocked(client.submitDecision).mockReturnValue(gate.promise)
    const store = createWorkflowStore(client)()
    store.resetDefinition('草稿')
    await store.focusInstance('wfi_a')
    const signing = store.submitDecision('wf_slow_a', 'approve', '同意', '值班指挥员')
    await Promise.resolve()
    await store.focusInstance('wfi_b')
    gate.resolve(snapshotOf('wfi_a', 'running'))
    expect(await signing).toBe(true)
    expect(store.instance?.instance_id, 'A 的快照盖到 B 的页面上，节点号就对不上那张图').toBe('wfi_b')
  })

  it('中止的是已经不在看的那条：不许把现在这条的轮询停掉', async () => {
    const client = twoInstanceClient()
    vi.mocked(client.instance).mockImplementation((instanceId: string) =>
      Promise.resolve(snapshotOf(instanceId, instanceId === 'wfi_a' ? 'running' : 'waiting')),
    )
    const gate = deferred<InstanceDetail>()
    vi.mocked(client.abortInstance).mockReturnValue(gate.promise)
    const store = createWorkflowStore(client)()
    store.resetDefinition('草稿')
    await store.focusInstance('wfi_a')
    const aborting = store.abortInstance('上游数据中断')
    await Promise.resolve()
    await store.focusInstance('wfi_b')
    gate.resolve(snapshotOf('wfi_a', 'aborted'))
    expect(await aborting).toBe(true)
    expect(store.instance?.instance_id).toBe('wfi_b')
    expect(store.polling, 'B 还等着人签，轮询不能因为 A 的回执迟到就停').toBe(true)
  })

  it('存定义的路上被人切走了：刚存的那份不覆盖眼前的新画布', async () => {
    const client = twoInstanceClient()
    const gate = deferred<{ workflow_id: string; name: string; version: number }>()
    vi.mocked(client.createDefinition).mockReturnValue(gate.promise)
    const store = createWorkflowStore(client)()
    store.resetDefinition('要存的草稿')
    store.addNode(createNodeDef('data_fetch', 'fetch_1'))
    store.addNode(createNodeDef('notify', 'notify_1'))
    const saving = store.saveDefinition()
    await Promise.resolve()
    store.resetDefinition('新画布')
    gate.resolve({ workflow_id: 'wf_new', name: '要存的草稿', version: 1 })
    expect(await saving).toBe(true)
    expect(store.current?.name, '切走之后迟到的保存结果不该把图铺回来').toBe('新画布')
    expect(store.current?.nodes).toHaveLength(0)
  })
})
