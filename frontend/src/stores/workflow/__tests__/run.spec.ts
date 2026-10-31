import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'

import { WorkflowError, type InstancesResponse, type WorkflowGateway } from '@/api/workflow/contract'

import { createDocumentStore, type DocumentStore } from '../document'
import { useEditorStore, type EditorStore } from '../editor'
import { createRunStore, type RunStore } from '../run'
import {
  INSTANCE,
  LINEAGE,
  OTHER_INSTANCE,
  PUBLISHED,
  deferred,
  gatewayStub,
  instanceDetail,
  instanceGraph,
  nodeDef,
  nodeRun,
  setVisibility,
  versionSummary,
  workflowDocument,
  type StubGateway,
} from './fixtures'

function runtimeGateway(overrides: Partial<Record<keyof WorkflowGateway, unknown>> = {}): StubGateway {
  return gatewayStub({
    instances: vi.fn(async () => ({ count: 0, items: [] })),
    instance: vi.fn(async () =>
      instanceDetail({ nodes: [nodeRun('review_1', 'awaiting_human', { type: 'human_review' })] }),
    ),
    startInstance: vi.fn(async () => instanceDetail({ nodes: [nodeRun('fetch_1', 'running')] })),
    createPreview: vi.fn(async () =>
      instanceDetail({
        instance_id: 'pvp_000000000001',
        preview: true,
        preview_id: 'pvp_000000000001',
        status: 'succeeded',
        nodes: [
          nodeRun('fetch_1', 'succeeded', { duration_ms: 41 }),
          nodeRun('review_1', 'succeeded', { type: 'human_review' }),
        ],
        results: { fetch_1: { level: 4 }, review_1: { decision: 'approve' } },
      }),
    ),
    submitDecision: vi.fn(async () => ({
      ...instanceDetail({ nodes: [nodeRun('review_1', 'succeeded')] }),
      标注沉淀: { status: '已沉淀', case_id: 'evt_9c1', 真值: '报=True 等级=5' },
    })),
    patchNodeConfig: vi.fn(async () => ({ node_id: 'fetch_1', config: { limit: 50 } })),
    insertNode: vi.fn(async () => ({ instance_id: INSTANCE, node_id: 'notify_9', after: 'fetch_1' })),
    bypassNode: vi.fn(async () => instanceDetail({ nodes: [nodeRun('review_1', 'bypassed')] })),
    abortInstance: vi.fn(async () => instanceDetail({ status: 'aborted', nodes: [nodeRun('fetch_1', 'cancelled')] })),
    ...overrides,
  })
}

let gateway: StubGateway
let documents: DocumentStore
let editor: EditorStore
let run: RunStore | undefined

function bootstrap(overrides: Partial<Record<keyof WorkflowGateway, unknown>> = {}): void {
  gateway = runtimeGateway(overrides)
  documents = createDocumentStore(gateway)()
  editor = useEditorStore()
  run = createRunStore(gateway)()
}

function runtime(): RunStore {
  if (run === undefined) throw new Error('run store 未创建：先调 bootstrap()')
  return run
}

function rows(count: number, status = 'waiting'): InstancesResponse {
  return {
    count,
    items: Array.from({ length: count }, (_, index) =>
      instanceDetail({ instance_id: `wfi_0000000000${index}`, status }),
    ),
  }
}

beforeEach(() => {
  setActivePinia(createPinia())
  run = undefined
  vi.useRealTimers()
})

afterEach(() => {
  run?.stopPolling()
  run?.stopQueuePolling()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('运行层：启动实例', () => {
  it('没有已发布版本时不启动，直接说清缺什么', async () => {
    bootstrap()
    expect(await runtime().startInstance()).toBe(false)
    expect(editor.error).toBe('请先发布工作流版本再启动实例')
    expect(gateway.startInstance).not.toHaveBeenCalled()
  })

  it('启动用已发布 workflow_id，并立刻按 1.5s 轮询', async () => {
    vi.useFakeTimers()
    bootstrap()
    documents.versions = [versionSummary()]

    expect(await runtime().startInstance({ alert: '沟道泥位抬升' })).toBe(true)
    expect(gateway.startInstance).toHaveBeenCalledWith({
      workflow_id: PUBLISHED,
      payload: { alert: '沟道泥位抬升' },
    })
    expect(runtime().instance?.instance_id).toBe(INSTANCE)
    expect(runtime().polling).toBe(true)
    expect(gateway.instances).toHaveBeenCalledTimes(1)

    const before = gateway.instance.mock.calls.length
    await vi.advanceTimersByTimeAsync(1_500)
    expect(gateway.instance.mock.calls.length).toBe(before + 1)
    runtime().stopPolling()
    expect(runtime().polling).toBe(false)
  })
})

describe('运行层：聚焦实例与画布', () => {
  it('聚焦实例把实例快照的图画进画布，并选中待核签节点', async () => {
    vi.useFakeTimers()
    bootstrap()
    documents.resetDocument('草稿')

    expect(await runtime().focusInstance(INSTANCE)).toBe(true)
    expect(gateway.instance).toHaveBeenCalledWith(INSTANCE)
    // 图来自实例详情自身，不再额外拉一趟版本文档。
    expect(gateway.getVersionDocument).not.toHaveBeenCalled()
    expect(documents.document?.name).toBe('运行实例')
    expect(documents.document?.nodes.map((node) => node.node_id)).toEqual(['fetch_1', 'review_1'])
    expect(documents.isDirty).toBe(false)
    expect(documents.autoSaveEnabled).toBe(false)
    expect(Object.keys(documents.document?.layout.positions ?? {}).sort()).toEqual(['fetch_1', 'review_1'])
    expect(editor.mode).toEqual({ kind: 'instance', instanceId: INSTANCE, readOnly: true })
    expect(editor.selectedNodeId).toBe('review_1')
    expect(runtime().nodeStateOf('review_1')).toBe('awaiting_human')
    expect(runtime().canDecideNode('review_1')).toBe(true)
    expect(runtime().decisionOptions('review_1')).toEqual(['approve', 'reject'])
    expect(runtime().polling).toBe(true)
  })

  it('没有待核签节点时选第一个，不留空选择', async () => {
    vi.useFakeTimers()
    bootstrap({
      instance: vi.fn(async () => instanceDetail({ status: 'running', nodes: [nodeRun('fetch_1', 'running')] })),
    })
    documents.resetDocument('草稿')
    expect(await runtime().focusInstance(INSTANCE)).toBe(true)
    expect(editor.selectedNodeId).toBe('fetch_1')
  })

  it('取不回实例时报后端原因，画布保持原样且不轮询', async () => {
    bootstrap({
      instance: vi.fn<WorkflowGateway['instance']>(async () => {
        throw new WorkflowError('not_found', 404, '目标不存在')
      }),
    })
    documents.resetDocument('草稿')
    expect(await runtime().focusInstance(INSTANCE)).toBe(false)
    expect(editor.error).toBe('读取实例失败：目标不存在')
    expect(documents.document?.name).toBe('草稿')
    expect(runtime().polling).toBe(false)
  })

  it('旧后端没给图时保持原画布，不用一张空图换掉正在看的东西', async () => {
    bootstrap({
      instance: vi.fn(async () =>
        instanceDetail({ nodes: [nodeRun('review_1', 'awaiting_human')], graph: instanceGraph([], []) }),
      ),
    })
    documents.resetDocument('草稿')
    expect(await runtime().focusInstance(INSTANCE)).toBe(true)
    expect(documents.document?.name).toBe('草稿')
    expect(editor.mode).toEqual({ kind: 'instance', instanceId: INSTANCE, readOnly: true })
    expect(runtime().polling).toBe(true)
  })

  it('实例图带着运行中插入的节点，画布不靠版本文档去猜', async () => {
    bootstrap({
      instance: vi.fn(async () =>
        instanceDetail({
          nodes: [nodeRun('fetch_1', 'succeeded'), nodeRun('notify_9', 'pending')],
          graph: instanceGraph(
            [nodeDef('fetch_1'), nodeDef('notify_9', 'notify')],
            [{ source: 'fetch_1', target: 'notify_9', condition: '' }],
            { fetch_1: { x: 0, y: 0 }, notify_9: { x: 300, y: 0 } },
          ),
        }),
      ),
    })
    documents.resetDocument('草稿')
    expect(await runtime().focusInstance(INSTANCE)).toBe(true)
    expect(documents.document?.nodes.map((node) => node.node_id)).toEqual(['fetch_1', 'notify_9'])
    expect(documents.document?.edges).toHaveLength(1)
    expect(documents.document?.layout.positions.notify_9).toEqual({ x: 300, y: 0 })
  })

  it('连点两条实例：慢的那条后到，面板、画布与选中都留在最后点的那条', async () => {
    vi.useFakeTimers()
    const slow = deferred<ReturnType<typeof instanceDetail>>()
    const SLOW = 'wf_aaaaaaaaaaaa'
    const FAST = 'wf_bbbbbbbbbbbb'
    bootstrap({
      instance: vi.fn((instanceId: string) =>
        instanceId === 'wfi_slow'
          ? slow.promise
          : Promise.resolve(
              instanceDetail({
                instance_id: OTHER_INSTANCE,
                workflow_id: FAST,
                status: 'waiting',
                nodes: [nodeRun('review_1', 'awaiting_human')],
                graph: instanceGraph([nodeDef('fetch_1'), nodeDef('review_1')], [], {
                  fetch_1: { x: 10, y: 10 },
                  review_1: { x: 290, y: 10 },
                }),
              }),
            ),
      ),
    })
    documents.resetDocument('草稿')

    const opening = runtime().focusInstance('wfi_slow')
    await Promise.resolve()
    expect(await runtime().focusInstance(OTHER_INSTANCE)).toBe(true)
    slow.resolve(
      instanceDetail({
        instance_id: 'wfi_slow',
        workflow_id: SLOW,
        status: 'running',
        nodes: [nodeRun('a', 'awaiting_human')],
        graph: instanceGraph([nodeDef('a')], [], { a: { x: 999, y: 999 } }),
      }),
    )
    expect(await opening).toBe(true)
    expect(runtime().instance?.instance_id).toBe(OTHER_INSTANCE)
    expect(documents.document?.nodes.map((node) => node.node_id)).toEqual(['fetch_1', 'review_1'])
    expect(editor.selectedNodeId).toBe('review_1')
  })

  it('迟到的轮询不把面板翻回上一条，轮询继续替新选的那条跑', async () => {
    vi.useFakeTimers()
    const held = deferred<ReturnType<typeof instanceDetail>>()
    let slowCalls = 0
    bootstrap({
      instance: vi.fn((instanceId: string) => {
        if (instanceId === 'wfi_slow') {
          slowCalls += 1
          if (slowCalls >= 2) return held.promise
          return Promise.resolve(
            instanceDetail({ instance_id: 'wfi_slow', status: 'waiting', nodes: [nodeRun('a', 'awaiting_human')] }),
          )
        }
        return Promise.resolve(
          instanceDetail({
            instance_id: OTHER_INSTANCE,
            status: 'waiting',
            nodes: [nodeRun('review_1', 'awaiting_human')],
          }),
        )
      }),
    })
    documents.resetDocument('草稿')
    await runtime().focusInstance('wfi_slow')
    await vi.advanceTimersByTimeAsync(1_500)
    expect(slowCalls).toBe(2)

    await runtime().focusInstance(OTHER_INSTANCE)
    held.resolve(instanceDetail({ instance_id: 'wfi_slow', status: 'succeeded' }))
    await vi.advanceTimersByTimeAsync(0)
    expect(runtime().instance?.instance_id).toBe(OTHER_INSTANCE)
    expect(runtime().polling).toBe(true)
  })
})

describe('运行层：试运行', () => {
  function draftWithLineage(): void {
    documents.document = workflowDocument({ lineage_id: LINEAGE, name: '未保存草稿' })
  }

  it('送的是画布上的草稿本身，结果只进 preview，不碰实例账本', async () => {
    bootstrap()
    draftWithLineage()

    expect(await runtime().startPreview({ alert: '沟道泥位抬升' })).toBe(true)
    const sent = gateway.createPreview.mock.calls[0]?.[0]
    expect(sent?.payload).toEqual({ alert: '沟道泥位抬升' })
    expect(sent?.document?.nodes.map((node) => node.node_id)).toEqual(['fetch_1', 'review_1'])
    expect(sent?.lineage_id).toBeUndefined()
    expect(runtime().preview?.preview_id).toBe('pvp_000000000001')
    expect(runtime().previewStatus).toBe('succeeded')
    expect(runtime().instance).toBeNull()
    expect(gateway.startInstance).not.toHaveBeenCalled()
    expect(gateway.instances).not.toHaveBeenCalled()
    expect(runtime().polling).toBe(false)
    expect(runtime().canvasRuns.map((row) => row.node_id)).toEqual(['fetch_1', 'review_1'])
    expect(editor.mode).toEqual({ kind: 'draft' })
    expect(editor.selectedNodeId).toBe('fetch_1')
  })

  it('草稿一改，旧试运行结果就不再画在画布上', async () => {
    bootstrap()
    draftWithLineage()
    await runtime().startPreview()
    expect(runtime().preview).not.toBeNull()

    documents.setMeta({ name: '改了名字' })
    await nextTick()
    expect(runtime().preview).toBeNull()
    expect(runtime().canvasRuns).toEqual([])
  })

  it('清掉试运行后，晚到的响应不许再贴回画布', async () => {
    const gate = deferred<ReturnType<typeof instanceDetail>>()
    bootstrap({ createPreview: vi.fn(() => gate.promise) })
    draftWithLineage()

    const pending = runtime().startPreview()
    runtime().clearPreview()
    gate.resolve(instanceDetail({ instance_id: 'pvp_x', preview: true, preview_id: 'pvp_x', status: 'succeeded' }))

    expect(await pending).toBe(true)
    expect(runtime().preview).toBeNull()
  })

  it('空白画布没有 lineage 就不发试运行，先把缺什么说清', async () => {
    bootstrap()
    documents.resetDocument('空白')
    expect(await runtime().startPreview()).toBe(false)
    expect(editor.error).toContain('lineage')
    expect(gateway.createPreview).not.toHaveBeenCalled()
  })

  it('只读版本视图不试运行：那一版不是画布上的草稿', async () => {
    bootstrap()
    documents.document = workflowDocument()
    editor.setMode({ kind: 'version', workflowId: PUBLISHED })
    expect(await runtime().startPreview()).toBe(false)
    expect(editor.error).toContain('只读版本')
    expect(gateway.createPreview).not.toHaveBeenCalled()
    expect(editor.mode).toEqual({ kind: 'version', workflowId: PUBLISHED })
  })

  it('只读版本模式不叠任何运行态', async () => {
    bootstrap()
    draftWithLineage()
    await runtime().startPreview()
    expect(runtime().canvasRuns.length).toBe(2)

    editor.setMode({ kind: 'version', workflowId: PUBLISHED })
    expect(runtime().canvasRuns).toEqual([])
  })
})

describe('运行层：实例模式与草稿', () => {
  it('进实例收起草稿，返回时原样放回，不重新拉服务器草稿', async () => {
    vi.useFakeTimers()
    bootstrap()
    // 草稿只画了一颗节点，实例图有两颗：换没换画布一眼就能看出来。
    documents.document = workflowDocument({ name: '未保存草稿', nodes: [nodeDef('fetch_1')], edges: [] })
    documents.setMeta({ name: '草稿改名' })
    expect(documents.isDirty).toBe(true)

    expect(await runtime().focusInstance(INSTANCE)).toBe(true)
    expect(documents.document?.nodes.map((node) => node.node_id)).toEqual(['fetch_1', 'review_1'])
    expect(gateway.getDraft).not.toHaveBeenCalled()

    expect(await runtime().exitInstance()).toBe(true)
    expect(documents.document?.nodes.map((node) => node.node_id)).toEqual(['fetch_1'])
    expect(documents.document?.name).toBe('草稿改名')
    expect(documents.isDirty).toBe(true)
    expect(documents.autoSaveEnabled).toBe(true)
    expect(editor.mode).toEqual({ kind: 'draft' })
    expect(runtime().instance).toBeNull()
    expect(runtime().polling).toBe(false)
    expect(gateway.getDraft).not.toHaveBeenCalled()
  })

  it('正式启动也收起草稿；返回后未发布的改动还在', async () => {
    vi.useFakeTimers()
    bootstrap()
    documents.versions = [versionSummary()]
    documents.document = workflowDocument()
    documents.setMeta({ name: '未发布改动' })

    expect(await runtime().startInstance()).toBe(true)
    expect(editor.mode.kind).toBe('instance')
    expect(gateway.getDraft).not.toHaveBeenCalled()

    expect(await runtime().exitInstance()).toBe(true)
    expect(documents.document?.name).toBe('未发布改动')
  })

  it('正式启动清掉试运行结果，画布改认实例状态', async () => {
    vi.useFakeTimers()
    bootstrap()
    documents.versions = [versionSummary()]
    documents.document = workflowDocument({ lineage_id: LINEAGE })

    await runtime().startPreview()
    expect(runtime().canvasRuns.map((row) => row.node_id)).toEqual(['fetch_1', 'review_1'])
    expect(await runtime().startInstance()).toBe(true)

    expect(runtime().preview).toBeNull()
    expect(editor.mode.kind).toBe('instance')
    expect(runtime().canvasRuns.map((row) => row.node_id)).toEqual(['fetch_1'])
  })

  it('从只读版本进实例，返回仍回到那一档只读版本', async () => {
    vi.useFakeTimers()
    bootstrap({
      getVersionDocument: vi.fn(async () => workflowDocument()),
      listVersions: vi.fn(async () => []),
    })
    documents.resetDocument('草稿')
    editor.setMode({ kind: 'version', workflowId: PUBLISHED })

    expect(await runtime().focusInstance(INSTANCE)).toBe(true)
    expect(await runtime().exitInstance()).toBe(true)
    expect(editor.mode).toEqual({ kind: 'version', workflowId: PUBLISHED })
    expect(gateway.getVersionDocument).toHaveBeenCalledWith(PUBLISHED)
  })

  it('轮询带回的新图（别的会话插入的节点）也长到实例画布上', async () => {
    vi.useFakeTimers()
    bootstrap({
      instance: vi.fn(async () => instanceDetail({ status: 'running', nodes: [nodeRun('fetch_1', 'running')] })),
    })
    documents.resetDocument('草稿')
    await runtime().focusInstance(INSTANCE)

    gateway.instance.mockResolvedValue(
      instanceDetail({
        status: 'running',
        nodes: [nodeRun('fetch_1', 'succeeded'), nodeRun('notify_9', 'pending')],
        graph: instanceGraph(
          [nodeDef('fetch_1'), nodeDef('notify_9', 'notify')],
          [{ source: 'fetch_1', target: 'notify_9', condition: '' }],
          { fetch_1: { x: 0, y: 0 }, notify_9: { x: 300, y: 0 } },
        ),
      }),
    )
    await vi.advanceTimersByTimeAsync(1_500)
    expect(documents.document?.nodes.map((node) => node.node_id)).toEqual(['fetch_1', 'notify_9'])
  })
})

describe('运行层：人工核签与数据飞轮', () => {
  async function focusedWaiting(): Promise<void> {
    documents.resetDocument('草稿')
    expect(await runtime().focusInstance(INSTANCE)).toBe(true)
  }

  it('核签发 choice/by/comment/level，回执替换实例快照', async () => {
    vi.useFakeTimers()
    bootstrap()
    await focusedWaiting()
    expect(await runtime().submitDecision('review_1', 'adjust', '改判红色', '扎西', 1)).toBe(true)

    expect(gateway.submitDecision).toHaveBeenCalledWith(INSTANCE, 'review_1', {
      choice: 'adjust',
      by: '扎西',
      comment: '改判红色',
      level: 1,
    })
    expect(runtime().nodeStateOf('review_1')).toBe('succeeded')
    expect(runtime().awaitingNodes).toEqual([])
    expect(runtime().captureFor('review_1')).toEqual({
      text: '这一票已进真值表：报=True 等级=5（事件 evt_9c1）',
      landed: true,
    })
  })

  it('没填签字人就不发 by，没选等级就不发 level', async () => {
    vi.useFakeTimers()
    bootstrap({
      submitDecision: vi.fn(async () => instanceDetail({ nodes: [nodeRun('review_1', 'succeeded')] })),
    })
    await focusedWaiting()
    expect(await runtime().submitDecision('review_1', 'reject', '现场已核实无险情')).toBe(true)

    const payload = gateway.submitDecision.mock.calls[0]?.[2]
    expect(payload).toEqual({ choice: 'reject', comment: '现场已核实无险情' })
    expect(payload).not.toHaveProperty('by')
    expect(payload).not.toHaveProperty('level')
  })

  it('后端没给沉淀回执时，说清这一单本来就不进标注池', async () => {
    vi.useFakeTimers()
    bootstrap({
      submitDecision: vi.fn(async () => instanceDetail({ nodes: [nodeRun('review_1', 'succeeded')] })),
    })
    await focusedWaiting()
    await runtime().submitDecision('review_1', 'approve', '同意')
    const note = runtime().captureFor('review_1')
    expect(note?.landed).toBe(false)
    expect(note?.text).toContain('没有"结论裁决"标记')
  })

  it('后端说没进真值表时，原因原样带出来', async () => {
    vi.useFakeTimers()
    bootstrap({
      submitDecision: vi.fn(async () => ({
        ...instanceDetail({ nodes: [nodeRun('review_1', 'succeeded')] }),
        标注沉淀: { status: '未沉淀', 原因: '当前存储视图没有标注面（store_backend=memory）' },
      })),
    })
    await focusedWaiting()
    await runtime().submitDecision('review_1', 'approve', '同意')
    const note = runtime().captureFor('review_1')
    expect(note?.landed).toBe(false)
    expect(note?.text).toContain('没进真值表')
    expect(note?.text).toContain('store_backend=memory')
  })

  it('换到别的实例，上一张单的沉淀回执不再挂过来', async () => {
    vi.useFakeTimers()
    bootstrap({
      instance: vi.fn(async (instanceId: string) =>
        instanceDetail({
          instance_id: instanceId,
          status: 'waiting',
          nodes: [nodeRun('review_1', 'awaiting_human')],
        }),
      ),
    })
    await focusedWaiting()
    await runtime().submitDecision('review_1', 'approve', '同意')
    expect(runtime().captureFor('review_1')).not.toBeNull()

    await runtime().focusInstance(OTHER_INSTANCE)
    expect(runtime().captureFor('review_1')).toBeNull()
  })

  it('列表没刷回来不能报成核签失败', async () => {
    vi.useFakeTimers()
    bootstrap({
      instances: vi.fn<WorkflowGateway['instances']>(async () => {
        throw new WorkflowError('network_error', 0, '实例列表不可用')
      }),
    })
    await focusedWaiting()
    expect(await runtime().submitDecision('review_1', 'approve', '同意')).toBe(true)
    expect(runtime().instance?.status).toBe('running')
    expect(editor.error).toContain('实例列表没刷新')
    expect(editor.error).not.toContain('核签失败')
  })

  it('签掉一张等人签的工单后列表跟着对账', async () => {
    vi.useFakeTimers()
    bootstrap({
      instances: vi.fn(async () => rows(1)),
    })
    await focusedWaiting()
    await runtime().loadInstances()
    expect(runtime().instances.filter((row) => row.status === 'waiting')).toHaveLength(1)
    gateway.instances.mockResolvedValue(rows(0))

    expect(await runtime().submitDecision('review_1', 'approve', '同意')).toBe(true)
    expect(runtime().instances.filter((row) => row.status === 'waiting')).toHaveLength(0)
  })
})

describe('运行层：运行中改参与插节点', () => {
  it('改参发 PATCH 并重拉实例快照', async () => {
    vi.useFakeTimers()
    bootstrap({
      instance: vi.fn(async () => instanceDetail({ status: 'running', nodes: [nodeRun('fetch_1', 'pending')] })),
    })
    documents.resetDocument('草稿')
    await runtime().focusInstance(INSTANCE)
    expect(runtime().canPatchRuntime('fetch_1')).toBe(true)

    expect(await runtime().patchRuntimeConfig('fetch_1', { limit: 50 })).toBe(true)
    expect(gateway.patchNodeConfig).toHaveBeenCalledWith(INSTANCE, 'fetch_1', { limit: 50 })
    expect(gateway.instance).toHaveBeenCalledTimes(2)
    expect(editor.error).toBeNull()
  })

  it('清空一格不会真的删掉旧值，页面必须说出来', async () => {
    vi.useFakeTimers()
    bootstrap({
      instance: vi.fn(async () => instanceDetail({ status: 'running', nodes: [nodeRun('fetch_1', 'pending')] })),
    })
    documents.resetDocument('草稿')
    await runtime().focusInstance(INSTANCE)

    expect(await runtime().patchRuntimeConfig('fetch_1', {})).toBe(true)
    expect(editor.error).toContain('limit')
    expect(editor.error).toContain('服务端仍是原值')
  })

  it('已执行的节点不可改参也不可旁路', async () => {
    vi.useFakeTimers()
    bootstrap({
      instance: vi.fn(async () => instanceDetail({ status: 'running', nodes: [nodeRun('fetch_1', 'succeeded')] })),
    })
    documents.resetDocument('草稿')
    await runtime().focusInstance(INSTANCE)
    expect(runtime().canPatchRuntime('fetch_1')).toBe(false)
    expect(runtime().canBypassNode('fetch_1')).toBe(false)
  })

  it('插入节点走 insert 接口，落点在锚点右侧，画布跟着长出新节点', async () => {
    vi.useFakeTimers()
    const instance = vi.fn<WorkflowGateway['instance']>()
    instance
      .mockResolvedValueOnce(instanceDetail({ status: 'running', nodes: [nodeRun('fetch_1', 'succeeded')] }))
      .mockResolvedValueOnce(
        instanceDetail({
          status: 'running',
          nodes: [nodeRun('fetch_1', 'succeeded'), nodeRun('notify_9', 'pending')],
          graph: instanceGraph(
            [nodeDef('fetch_1'), nodeDef('notify_9', 'notify')],
            [{ source: 'fetch_1', target: 'notify_9', condition: '' }],
            { fetch_1: { x: 0, y: 0 }, notify_9: { x: 300, y: 0 } },
          ),
        }),
      )
    bootstrap({ instance })
    documents.resetDocument('草稿')
    await runtime().focusInstance(INSTANCE)
    const epoch = documents.documentEpoch

    expect(await runtime().insertRuntimeNode('fetch_1', nodeDef('notify_9', 'notify', { text: '注意' }))).toBe(true)
    const call = gateway.insertNode.mock.calls[0]
    expect(call?.[0]).toBe(INSTANCE)
    expect(call?.[1].after).toBe('fetch_1')
    expect(call?.[1].node.node_id).toBe('notify_9')
    expect(call?.[1].node.retry).toEqual({ max_attempts: 1, backoff_ms: 200 })
    expect(call?.[1].position).toEqual({ x: 300, y: 0 })
    expect(gateway.instance).toHaveBeenCalledTimes(2)

    // 插入只该多出一颗节点，不该把视口重收到别处。
    expect(documents.document?.nodes.map((node) => node.node_id)).toEqual(['fetch_1', 'notify_9'])
    expect(documents.document?.layout.positions.notify_9).toEqual({ x: 300, y: 0 })
    expect(documents.documentEpoch).toBe(epoch)
  })
})

describe('运行层：旁路、中止与实例队列', () => {
  it('旁路等人签节点后刷新快照与列表', async () => {
    vi.useFakeTimers()
    bootstrap()
    documents.resetDocument('草稿')
    await runtime().focusInstance(INSTANCE)
    expect(runtime().canBypassNode('review_1')).toBe(true)

    expect(await runtime().bypassNode('review_1', '现场已处置')).toBe(true)
    expect(gateway.bypassNode).toHaveBeenCalledWith(INSTANCE, 'review_1', '现场已处置')
    expect(runtime().nodeStateOf('review_1')).toBe('bypassed')
    expect(gateway.instances).toHaveBeenCalled()
  })

  it('中止后进入终态并停掉轮询', async () => {
    vi.useFakeTimers()
    bootstrap({
      instance: vi.fn(async () => instanceDetail({ status: 'running', nodes: [nodeRun('fetch_1', 'running')] })),
    })
    documents.resetDocument('草稿')
    await runtime().focusInstance(INSTANCE)
    expect(runtime().polling).toBe(true)

    expect(await runtime().abortInstance('上游数据中断')).toBe(true)
    expect(gateway.abortInstance).toHaveBeenCalledWith(INSTANCE, '上游数据中断')
    expect(runtime().polling).toBe(false)
    expect(runtime().instanceStatus).toBe('aborted')
  })

  it('实例进入终态后自动停止轮询', async () => {
    vi.useFakeTimers()
    bootstrap({
      instance: vi.fn(async () => instanceDetail({ status: 'succeeded' })),
    })
    documents.resetDocument('草稿')
    await runtime().focusInstance(INSTANCE)
    expect(runtime().polling).toBe(true)
    expect(runtime().instanceMutable).toBe(false)
    await vi.advanceTimersByTimeAsync(1_500)
    expect(runtime().polling).toBe(false)
  })

  it('轮询发现进入终态才重拉列表，人还等着签时不白打接口', async () => {
    vi.useFakeTimers()
    bootstrap()
    documents.resetDocument('草稿')
    await runtime().focusInstance(INSTANCE)
    await vi.advanceTimersByTimeAsync(1_500)
    expect(gateway.instances).not.toHaveBeenCalled()

    gateway.instance.mockResolvedValue(instanceDetail({ status: 'succeeded' }))
    await vi.advanceTimersByTimeAsync(1_500)
    expect(gateway.instances).toHaveBeenCalledTimes(1)
  })

  it('后端给了没映射的状态时不崩，也不当成终态去重拉列表', async () => {
    vi.useFakeTimers()
    bootstrap()
    documents.resetDocument('草稿')
    await runtime().focusInstance(INSTANCE)

    gateway.instance.mockResolvedValue(instanceDetail({ status: 'paused_by_operator' }))
    await vi.advanceTimersByTimeAsync(1_500)
    expect(runtime().instanceStatus).toBeNull()
    expect(editor.error).toBeNull()
    expect(gateway.instances).not.toHaveBeenCalled()
  })

  it('「刷新实例」把详情和列表一起对新', async () => {
    vi.useFakeTimers()
    bootstrap({ instances: vi.fn(async () => rows(1)) })
    documents.resetDocument('草稿')
    await runtime().focusInstance(INSTANCE)
    const detailCalls = gateway.instance.mock.calls.length

    expect(await runtime().refreshAll()).toBe(true)
    expect(gateway.instance.mock.calls.length).toBe(detailCalls + 1)
    expect(gateway.instances).toHaveBeenCalledTimes(1)
  })

  it('打开第一张等人签的工单：列表新到旧，取最新的那一个', async () => {
    vi.useFakeTimers()
    bootstrap({
      instances: vi.fn(async () => ({
        count: 3,
        items: [
          instanceDetail({ instance_id: 'wfi_old_waiting', status: 'waiting' }),
          instanceDetail({ instance_id: 'wfi_done', status: 'succeeded' }),
          instanceDetail({ instance_id: 'wfi_new_waiting', status: 'waiting' }),
        ],
      })),
    })
    documents.resetDocument('草稿')
    await runtime().loadInstances()

    expect(await runtime().openFirstWaiting()).toBe(true)
    const calls = gateway.instance.mock.calls
    expect(calls[calls.length - 1]?.[0]).toBe('wfi_new_waiting')
  })

  it('没有人在等签时待办条无事可做，不去打扰接口', async () => {
    bootstrap({ instances: vi.fn(async () => rows(0)) })
    await runtime().loadInstances()
    expect(await runtime().openFirstWaiting()).toBe(false)
    expect(gateway.instance).not.toHaveBeenCalled()
  })

  it('先发后回的旧队列快照不许把数字往回带', async () => {
    const slow = deferred<InstancesResponse>()
    const fast = deferred<InstancesResponse>()
    const instances = vi.fn<WorkflowGateway['instances']>()
    instances.mockReturnValueOnce(slow.promise).mockReturnValueOnce(fast.promise)
    bootstrap({ instances })

    const first = runtime().loadInstances()
    await Promise.resolve()
    const second = runtime().loadInstances()
    fast.resolve(rows(3))
    await second
    expect(runtime().instances).toHaveLength(3)

    slow.resolve(rows(1))
    await first
    expect(runtime().instances).toHaveLength(3)
    expect(runtime().instancesUpdatedAt).not.toBeNull()
  })

  it('页签在后台时队列自查不打接口', async () => {
    vi.useFakeTimers()
    const restore = setVisibility('hidden')
    try {
      bootstrap({ instances: vi.fn(async () => rows(0)) })
      await runtime().loadInstances()
      const before = gateway.instances.mock.calls.length
      runtime().startQueuePolling()
      await vi.advanceTimersByTimeAsync(45_000)
      expect(gateway.instances.mock.calls.length).toBe(before)
      runtime().stopQueuePolling()
    } finally {
      restore()
    }
  })

  it('回到前台后队列按 15s 自查，把别人开的新工单带进来', async () => {
    vi.useFakeTimers()
    bootstrap({ instances: vi.fn(async () => rows(2)) })
    await runtime().loadInstances()
    gateway.instances.mockResolvedValue(rows(3))
    const before = gateway.instances.mock.calls.length
    runtime().startQueuePolling()
    await vi.advanceTimersByTimeAsync(15_000)
    expect(gateway.instances.mock.calls.length).toBe(before + 1)
    expect(runtime().instances).toHaveLength(3)
    runtime().stopQueuePolling()
  })
})

describe('运行层：空态、候选与迟到回执', () => {
  it('没有当前实例时读操作给空值，写操作直接回 false 且不打接口', async () => {
    bootstrap()
    expect(runtime().nodeRun('review_1')).toBeNull()
    expect(runtime().nodeStateOf('review_1')).toBeNull()
    expect(runtime().awaitingNodes).toEqual([])
    expect(runtime().decisionOptions('review_1')).toEqual([])

    documents.document = null
    expect(runtime().decisionOptions('review_1')).toEqual([])

    expect(await runtime().submitDecision('review_1', 'approve')).toBe(false)
    expect(await runtime().patchRuntimeConfig('fetch_1', { limit: 1 })).toBe(false)
    expect(await runtime().insertRuntimeNode('fetch_1', nodeDef('notify_9', 'notify'))).toBe(false)
    expect(await runtime().bypassNode('review_1', '原因')).toBe(false)
    expect(await runtime().abortInstance('原因')).toBe(false)
    await runtime().refreshInstance()
    expect(gateway.submitDecision).not.toHaveBeenCalled()
    expect(gateway.patchNodeConfig).not.toHaveBeenCalled()
    expect(gateway.insertNode).not.toHaveBeenCalled()
    expect(gateway.bypassNode).not.toHaveBeenCalled()
    expect(gateway.abortInstance).not.toHaveBeenCalled()
    expect(gateway.instance).not.toHaveBeenCalled()
  })

  it('决策候选取自节点 config.options；非数组与杂项都过滤掉', () => {
    bootstrap()
    documents.document = workflowDocument({
      nodes: [
        nodeDef('review_1', 'human_review', { options: 'nope' }),
        nodeDef('review_2', 'human_review', { options: [1, 'approve', null] }),
        nodeDef('fetch_1'),
      ],
    })
    expect(runtime().decisionOptions('review_1')).toEqual([])
    expect(runtime().decisionOptions('review_2')).toEqual(['approve'])
    expect(runtime().decisionOptions('fetch_1')).toEqual([])
    expect(runtime().decisionOptions('missing')).toEqual([])
  })

  it('轮询与队列轮询都只开一个计时器', async () => {
    vi.useFakeTimers()
    bootstrap()
    documents.resetDocument('草稿')
    await runtime().focusInstance(INSTANCE)
    const detailCalls = gateway.instance.mock.calls.length
    runtime().startPolling()
    await vi.advanceTimersByTimeAsync(1_500)
    expect(gateway.instance.mock.calls.length).toBe(detailCalls + 1)

    const listCalls = gateway.instances.mock.calls.length
    runtime().startQueuePolling()
    runtime().startQueuePolling()
    await vi.advanceTimersByTimeAsync(15_000)
    expect(gateway.instances.mock.calls.length).toBe(listCalls + 1)
    runtime().stopQueuePolling()
  })

  it('刷新失败把原因写在页面上，旧快照不丢', async () => {
    bootstrap()
    documents.resetDocument('草稿')
    await runtime().focusInstance(INSTANCE)
    gateway.instance.mockRejectedValueOnce(new WorkflowError('network_error', 0, '网关断开'))
    await runtime().refreshInstance()
    expect(editor.error).toContain('网关断开')
    expect(runtime().instance?.instance_id).toBe(INSTANCE)
  })

  it('核签回执晚到、人已切单：不覆盖当前面板', async () => {
    const gate = deferred<ReturnType<typeof instanceDetail>>()
    bootstrap({
      submitDecision: vi.fn<WorkflowGateway['submitDecision']>(() => gate.promise),
    })
    documents.resetDocument('草稿')
    await runtime().focusInstance(INSTANCE)
    const submitting = runtime().submitDecision('review_1', 'approve', '同意')
    runtime().instance = instanceDetail({ instance_id: OTHER_INSTANCE, status: 'waiting' })
    gate.resolve(instanceDetail({ instance_id: INSTANCE, nodes: [nodeRun('review_1', 'succeeded')] }))

    expect(await submitting).toBe(true)
    expect(runtime().instance?.instance_id).toBe(OTHER_INSTANCE)
    expect(runtime().captureFor('review_1')).toBeNull()
  })

  it('启动回执晚到、人已聚焦另一条：不把新面板换回旧实例', async () => {
    const gate = deferred<ReturnType<typeof instanceDetail>>()
    bootstrap({
      startInstance: vi.fn<WorkflowGateway['startInstance']>(() => gate.promise),
      instance: vi.fn(async (instanceId: string) =>
        instanceDetail({
          instance_id: instanceId === 'other' ? OTHER_INSTANCE : INSTANCE,
          status: 'waiting',
          nodes: [nodeRun('review_1', 'awaiting_human')],
        }),
      ),
    })
    documents.versions = [versionSummary()]
    const starting = runtime().startInstance()
    await Promise.resolve()
    expect(await runtime().focusInstance('other')).toBe(true)
    gate.resolve(instanceDetail({ instance_id: INSTANCE, status: 'running' }))

    expect(await starting).toBe(true)
    expect(runtime().instance?.instance_id).toBe(OTHER_INSTANCE)
  })

  it('聚焦时实例详情晚到、人已切到另一条：旧图不铺回画布', async () => {
    const gate = deferred<ReturnType<typeof instanceDetail>>()
    bootstrap({
      instance: vi.fn((instanceId: string) =>
        instanceId === 'slow'
          ? gate.promise
          : Promise.resolve(
              instanceDetail({
                instance_id: 'fast',
                status: 'waiting',
                nodes: [nodeRun('review_1', 'awaiting_human')],
              }),
            ),
      ),
    })
    documents.resetDocument('草稿')
    const opening = runtime().focusInstance('slow')
    await Promise.resolve()
    expect(await runtime().focusInstance('fast')).toBe(true)
    gate.resolve(
      instanceDetail({
        instance_id: 'slow',
        status: 'running',
        nodes: [nodeRun('a', 'running')],
        graph: instanceGraph([nodeDef('a')], [], { a: { x: 0, y: 0 } }),
      }),
    )

    expect(await opening).toBe(true)
    expect(documents.document?.nodes.map((node) => node.node_id)).toEqual(['fetch_1', 'review_1'])
    expect(editor.selectedNodeId).toBe('review_1')
  })

  it('沉淀回执缺真值/事件号时用占位说明，不拼出 undefined', async () => {
    bootstrap({
      submitDecision: vi.fn(async () => ({
        ...instanceDetail({ nodes: [nodeRun('review_1', 'succeeded')] }),
        标注沉淀: { status: '已沉淀' },
      })),
    })
    documents.resetDocument('草稿')
    await runtime().focusInstance(INSTANCE)
    await runtime().submitDecision('review_1', 'approve', '同意')
    const note = runtime().captureFor('review_1')
    expect(note?.text).toContain('这一票已进真值表：')
    expect(note?.text).toContain('事件 未知')
    expect(note?.text).not.toContain('undefined')
  })

  it('未沉淀但后端没说原因时，不编造原因', async () => {
    bootstrap({
      submitDecision: vi.fn(async () => ({
        ...instanceDetail({ nodes: [nodeRun('review_1', 'succeeded')] }),
        标注沉淀: { status: '未沉淀' },
      })),
    })
    documents.resetDocument('草稿')
    await runtime().focusInstance(INSTANCE)
    await runtime().submitDecision('review_1', 'approve', '同意')
    expect(runtime().captureFor('review_1')?.text).toContain('后端没说明原因')
  })
})
