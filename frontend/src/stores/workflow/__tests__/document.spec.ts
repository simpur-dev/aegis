import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { WorkflowError, type DraftPutInput, type WorkflowGateway } from '@/api/workflow/contract'
import { edgeId } from '@/utils/graph'
import { emptyLayout, MAX_STICKY_NOTES, type WorkflowDocument, type WorkflowDraft } from '@/utils/graph/document'

import { createDocumentStore, type DocumentStore } from '../document'
import {
  LINEAGE,
  OTHER_WORKFLOW,
  PUBLISHED,
  UPDATED_AT,
  deferred,
  draftOf,
  gatewayStub,
  nodeDef,
  versionSummary,
  workflowDocument,
  type StubGateway,
} from './fixtures'

/**
 * 一台有状态的假服务端：草稿 revision 会随 PUT 递增，发布后版本列表仍回同一份。
 * 用例要量的是 store 的账（什么时候发、带哪个 revision、失败后停在哪），不是 HTTP。
 */
function fakeGateway(overrides: Partial<Record<keyof WorkflowGateway, unknown>> = {}): StubGateway {
  let serverRevision = 2
  let serverDocument = workflowDocument()
  return gatewayStub({
    createLineage: vi.fn(async (name: string) => {
      serverRevision = 0
      serverDocument = workflowDocument({ name, nodes: [], edges: [], layout: emptyLayout() })
      return draftOf(serverDocument, { revision: 0 })
    }),
    putDraft: vi.fn(async (lineage: string, payload: DraftPutInput) => {
      serverDocument = payload.document
      serverRevision = payload.base_revision + 1
      return draftOf(serverDocument, { lineage_id: lineage, revision: serverRevision })
    }),
    getDraft: vi.fn(async () => draftOf(serverDocument, { revision: serverRevision })),
    listVersions: vi.fn(async () => [versionSummary()]),
    getVersionDocument: vi.fn(async () => workflowDocument()),
    publish: vi.fn(async () => versionSummary({ name: serverDocument.name })),
    revertToDraft: vi.fn(async (lineage: string, workflowId: string) => {
      serverRevision += 1
      return draftOf(serverDocument, {
        lineage_id: lineage,
        base_workflow_id: workflowId,
        revision: serverRevision,
      })
    }),
    ...overrides,
  })
}

let gateway: StubGateway
let store: DocumentStore | undefined

function bootstrap(overrides: Partial<Record<keyof WorkflowGateway, unknown>> = {}): void {
  gateway = fakeGateway(overrides)
  store = createDocumentStore(gateway)()
}

function current(): DocumentStore {
  if (store === undefined) throw new Error('store 未创建：先调 bootstrap()')
  return store
}

beforeEach(() => {
  setActivePinia(createPinia())
  store = undefined
  vi.useRealTimers()
})

afterEach(() => {
  store?.dispose()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('文档层：编辑与本地体检', () => {
  it('空白画布不算脏；缺 lineage 不挡编辑，只报缺节点', () => {
    bootstrap()
    expect(current().isDirty).toBe(false)
    expect(current().lineageId).toBe('')
    expect(current().validationErrors).toEqual(['工作流至少需要一个节点'])
    expect(current().canSave).toBe(false)

    current().addNode(nodeDef('fetch_1'))
    expect(current().validationErrors).toEqual([])
    expect(current().canSave).toBe(true)
    expect(current().isDirty).toBe(true)
  })

  it('连线防自环与重复，删节点级联删边并剪掉坐标；改上游取连线', () => {
    bootstrap()
    current().addNode(nodeDef('fetch_1'))
    current().addNode(nodeDef('join_1', 'join', { upstream: [] }))

    expect(current().connect('fetch_1', 'fetch_1')).toBe(false)
    expect(current().connect('fetch_1', 'join_1')).toBe(true)
    expect(current().connect('fetch_1', 'join_1')).toBe(false)
    expect(current().document?.edges).toEqual([{ source: 'fetch_1', target: 'join_1', condition: '' }])

    current().syncUpstreamConfig('join_1')
    expect(current().definition?.nodes.find((node) => node.node_id === 'join_1')?.config.upstream).toEqual(['fetch_1'])

    const edge = current().document?.edges[0]
    if (edge === undefined) throw new Error('连线未建立')
    current().updateEdgeCondition(edgeId(0, edge), 'High ')
    expect(current().document?.edges[0]?.condition).toBe('high')
    current().removeNodes(['fetch_1'])
    expect(current().definition?.edges).toEqual([])
    expect(current().document?.layout.positions.fetch_1).toBeUndefined()
  })

  it('布局：拖动会落坐标，环图自动布局退回网格', () => {
    bootstrap()
    current().addNode(nodeDef('a'))
    current().addNode(nodeDef('b'))
    current().connect('a', 'b')
    current().connect('b', 'a')
    current().autoLayout()
    expect(current().document?.layout.positions).toEqual({ a: { x: 0, y: 0 }, b: { x: 280, y: 0 } })

    current().moveNode('a', { x: 12, y: 34 })
    expect(current().document?.layout.positions.a).toEqual({ x: 12, y: 34 })
  })

  it('备注只活在版式里：增删改、上限 32，且不进工作流定义', () => {
    bootstrap()
    current().addNode(nodeDef('fetch_1'))
    expect(current().addNote({ x: 12, y: 34 })).toBe('note_1')
    expect(current().document?.layout.notes).toEqual([
      { note_id: 'note_1', x: 12, y: 34, width: 220, height: 140, text: '', color: 'amber' },
    ])

    current().updateNote('note_1', { text: '复核口径', color: 'blue', x: 80 })
    expect(current().document?.layout.notes[0]).toMatchObject({ text: '复核口径', color: 'blue', x: 80, y: 34 })
    expect(current().definition?.nodes).toHaveLength(1)
    expect(current().definition).not.toHaveProperty('layout')
    expect(current().isDirty).toBe(true)

    current().removeNote('note_1')
    expect(current().document?.layout.notes).toEqual([])

    for (let index = 0; index < MAX_STICKY_NOTES; index += 1) {
      expect(current().addNote()).not.toBeNull()
    }
    expect(current().document?.layout.notes).toHaveLength(MAX_STICKY_NOTES)
    expect(current().addNote()).toBeNull()
    expect(current().document?.layout.notes).toHaveLength(MAX_STICKY_NOTES)
  })

  it('边中点插入：条件留在进新节点那段，下游 upstream 改指新节点并落坐标', () => {
    bootstrap()
    current().addNode(nodeDef('a'))
    current().addNode(nodeDef('join_1', 'join', { upstream: ['a'] }))
    current().connect('a', 'join_1')
    const original = current().document?.edges[0]
    if (original === undefined) throw new Error('连线未建立')
    current().updateEdgeCondition(edgeId(0, original), 'high')

    expect(
      current().insertNodeBetween({ source: 'a', target: 'join_1' }, nodeDef('mid_1', 'notify'), { x: 100, y: 20 }),
    ).toBe(true)
    expect(current().document?.edges).toEqual([
      { source: 'a', target: 'mid_1', condition: 'high' },
      { source: 'mid_1', target: 'join_1', condition: '' },
    ])
    expect(current().definition?.edges).toEqual(current().document?.edges)
    expect(current().definition?.nodes.find((node) => node.node_id === 'join_1')?.config.upstream).toEqual(['mid_1'])
    expect(current().document?.layout.positions.mid_1).toEqual({ x: 100, y: 20 })
    expect(current().insertNodeBetween({ source: 'a', target: 'missing' }, nodeDef('mid_2'), { x: 0, y: 0 })).toBe(
      false,
    )
  })

  it('插进去的那颗自己声明 upstream 时，取的是它的真上游（不是照抄原边）', () => {
    bootstrap()
    current().addNode(nodeDef('a'))
    current().addNode(nodeDef('b'))
    current().connect('a', 'b')
    expect(
      current().insertNodeBetween({ source: 'a', target: 'b' }, nodeDef('join_1', 'join', { upstream: [] }), {
        x: 0,
        y: 0,
      }),
    ).toBe(true)
    /* 数组型 upstream 会带上进入新节点的那一段的 source；标量型则整体赋值。两支都要各钉一次，
       否则以后有人把数组那支改坏，测试只会红在另一支上。 */
    expect(current().definition?.nodes.find((node) => node.node_id === 'join_1')?.config.upstream).toEqual(['a'])
    current().addNode(nodeDef('c'))
    current().connect('join_1', 'c')
    expect(
      current().insertNodeBetween({ source: 'join_1', target: 'c' }, nodeDef('gate', 'join', { upstream: 'x' }), {
        x: 0,
        y: 0,
      }),
    ).toBe(true)
    expect(current().definition?.nodes.find((node) => node.node_id === 'gate')?.config.upstream).toBe('join_1')
  })

  it('加载新文档才推 epoch：改内容不算换图', async () => {
    bootstrap()
    const epoch = current().documentEpoch
    current().addNode(nodeDef('fetch_1'))
    current().setMeta({ name: '改个名' })
    expect(current().documentEpoch).toBe(epoch)
    expect(await current().openLineage(LINEAGE)).toBe(true)
    expect(current().documentEpoch).toBe(epoch + 1)
    current().setMeta({ description: '改说明' })
    expect(current().documentEpoch).toBe(epoch + 1)
  })
})

describe('文档层：自动保存', () => {
  it('改动先标脏，800ms 防抖后才落盘；首次保存懒建 lineage', async () => {
    vi.useFakeTimers()
    bootstrap()
    current().addNode(nodeDef('fetch_1'))
    expect(current().saveState.kind).toBe('dirty')
    expect(current().statusText).toBe('有未发布的更改')

    await vi.advanceTimersByTimeAsync(799)
    expect(gateway.createLineage).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1)

    expect(gateway.createLineage).toHaveBeenCalledTimes(1)
    const put = gateway.putDraft.mock.calls[0]
    expect(put?.[0]).toBe(LINEAGE)
    expect(put?.[1].base_revision).toBe(0)
    expect(put?.[1].document.lineage_id).toBe(LINEAGE)
    expect(current().lineageId).toBe(LINEAGE)
    expect(current().revision).toBe(1)
    expect(current().isDirty).toBe(false)
    expect(current().saveState).toEqual({ kind: 'clean', savedAt: UPDATED_AT })
    expect(current().statusText).toContain('草稿已自动保存')
  })

  it('改回原样时不制造空保存，也不凭空建 lineage', async () => {
    vi.useFakeTimers()
    bootstrap()
    current().addNode(nodeDef('fetch_1'))
    current().removeNodes(['fetch_1'])
    expect(current().isDirty).toBe(false)

    expect(await current().saveNow('auto')).toBe(true)
    await vi.advanceTimersByTimeAsync(2_000)
    expect(gateway.createLineage).not.toHaveBeenCalled()
    expect(gateway.putDraft).not.toHaveBeenCalled()
  })

  it('手动 flush 立即落盘并取消防抖中的那一趟', async () => {
    vi.useFakeTimers()
    bootstrap()
    current().addNode(nodeDef('fetch_1'))
    expect(await current().flush()).toBe(true)
    expect(gateway.putDraft).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(2_000)
    expect(gateway.putDraft).toHaveBeenCalledTimes(1)
  })

  it('409 停在冲突态，不覆盖也不重试；后续编辑不再自动保存', async () => {
    vi.useFakeTimers()
    bootstrap({
      putDraft: vi.fn<WorkflowGateway['putDraft']>(async () => {
        throw new WorkflowError('revision_conflict', 409, '草稿已被其他会话修改（服务器 revision=3）', {
          server_revision: 3,
        })
      }),
    })
    current().addNode(nodeDef('fetch_1'))
    await vi.advanceTimersByTimeAsync(800)

    expect(current().saveState).toEqual({ kind: 'conflict', serverRevision: 3, localRevision: 0 })
    expect(current().statusText).toBe('草稿已被其他会话修改')
    expect(current().error).toContain('服务器 revision=3')

    current().addNode(nodeDef('notify_1'))
    await vi.advanceTimersByTimeAsync(5_000)
    expect(gateway.putDraft).toHaveBeenCalledTimes(1)
  })

  it('网络失败按 1s/2s/4s 退避，仍失败则停在可重试错误', async () => {
    vi.useFakeTimers()
    bootstrap({
      putDraft: vi.fn<WorkflowGateway['putDraft']>(async () => {
        throw new WorkflowError('network_error', 0, '接口调用失败（网络）：请求超时')
      }),
    })
    current().addNode(nodeDef('fetch_1'))
    const saving = current().flush()
    await vi.advanceTimersByTimeAsync(8_000)

    expect(await saving).toBe(false)
    expect(gateway.putDraft).toHaveBeenCalledTimes(4)
    expect(current().saveState).toMatchObject({ kind: 'error', code: 'network_error', retryable: true })
    expect(current().statusText).toBe('草稿保存失败')
    expect(current().error).toContain('草稿保存失败：接口调用失败（网络）：请求超时')
  })

  it('保存在路上时又改了：这一趟只清已落盘那一版，剩余改动继续排下一趟', async () => {
    vi.useFakeTimers()
    const first = deferred<WorkflowDraft>()
    let sentDocument: WorkflowDocument | null = null
    const putDraft = vi.fn<WorkflowGateway['putDraft']>()
    putDraft.mockImplementationOnce((_lineage, payload) => {
      sentDocument = payload.document
      return first.promise
    })
    putDraft.mockImplementation(async (lineage, payload) =>
      draftOf(payload.document, { lineage_id: lineage, revision: payload.base_revision + 1 }),
    )
    bootstrap({ putDraft })

    expect(await current().openLineage(LINEAGE)).toBe(true)
    current().addNode(nodeDef('notify_1'))
    const saving = current().flush()
    expect(current().saveState.kind).toBe('saving')
    expect(current().statusText).toBe('正在保存草稿…')
    if (sentDocument === null) throw new Error('第一趟保存没有发出去')

    current().setMeta({ name: '保存途中改名' })
    first.resolve(draftOf(sentDocument, { revision: 3 }))

    expect(await saving).toBe(true)
    expect(current().revision).toBe(3)
    expect(current().isDirty).toBe(true)
    expect(current().saveState.kind).toBe('dirty')

    await vi.advanceTimersByTimeAsync(800)
    expect(putDraft).toHaveBeenCalledTimes(2)
    expect(putDraft.mock.calls[1]?.[1].base_revision).toBe(3)
    expect(putDraft.mock.calls[1]?.[1].document.name).toBe('保存途中改名')
    expect(current().isDirty).toBe(false)
  })

  it('保存在路上时换图：迟到的回执不把新画布铺回去', async () => {
    vi.useFakeTimers()
    const gate = deferred<WorkflowDraft>()
    const putDraft = vi.fn<WorkflowGateway['putDraft']>(() => gate.promise)
    bootstrap({ putDraft })

    expect(await current().openLineage(LINEAGE)).toBe(true)
    current().addNode(nodeDef('notify_1'))
    const saving = current().flush()
    const sent = current().document
    if (sent === null) throw new Error('画布未初始化')
    current().resetDocument('新画布')
    gate.resolve(draftOf(sent, { revision: 3 }))

    expect(await saving).toBe(false)
    expect(current().document?.name).toBe('新画布')
    expect(current().document?.nodes).toHaveLength(0)
  })
})

describe('文档层：打开草稿与版本', () => {
  it('打开 lineage：草稿与版本列表一起取回，基线对齐后不脏', async () => {
    bootstrap()
    expect(await current().openLineage(LINEAGE)).toBe(true)

    expect(gateway.getDraft).toHaveBeenCalledWith(LINEAGE)
    expect(current().lineageId).toBe(LINEAGE)
    expect(current().revision).toBe(2)
    expect(current().definition?.name).toBe('已存链路')
    expect(current().isDirty).toBe(false)
    expect(current().saveState).toEqual({ kind: 'clean', savedAt: UPDATED_AT })
    expect(current().versions).toHaveLength(1)
    expect(current().publishedWorkflowId).toBe(PUBLISHED)
    expect(current().publishedVersion).toBe(1)
  })

  it('还没发布过的 lineage：listVersions 回 404 视为空版本，不是打开失败', async () => {
    bootstrap({
      listVersions: vi.fn<WorkflowGateway['listVersions']>(async () => {
        throw new WorkflowError('not_found', 404, '还没有发布版本')
      }),
    })
    expect(await current().openLineage(LINEAGE)).toBe(true)
    expect(current().versions).toEqual([])
    expect(current().publishedWorkflowId).toBeNull()
    expect(current().error).toBeNull()
  })

  it('打开失败保留原画布，并把后端原因写进错误', async () => {
    bootstrap({
      getDraft: vi.fn<WorkflowGateway['getDraft']>(async () => {
        throw new WorkflowError('not_found', 404, '目标不存在')
      }),
    })
    current().setMeta({ name: '本地草稿' })
    expect(await current().openLineage(LINEAGE)).toBe(false)
    expect(current().document?.name).toBe('本地草稿')
    expect(current().error).toBe('打开定义失败：目标不存在')
  })

  it('旧定义没有版式时按分层自动布局，且这份补全不被记成改动', async () => {
    bootstrap({
      getDraft: vi.fn<WorkflowGateway['getDraft']>(async () =>
        draftOf(workflowDocument({ layout: emptyLayout() }), { revision: 4 }),
      ),
    })
    expect(await current().openLineage(LINEAGE)).toBe(true)
    expect(Object.keys(current().document?.layout.positions ?? {}).sort()).toEqual(['fetch_1', 'review_1'])
    expect(current().isDirty).toBe(false)
    expect(current().revision).toBe(4)
  })

  it('打开版本是只读视图：能看不能存，发布也被挡住', async () => {
    vi.useFakeTimers()
    bootstrap()
    expect(await current().openVersion(PUBLISHED)).toBe(true)

    expect(gateway.getVersionDocument).toHaveBeenCalledWith(PUBLISHED)
    expect(current().autoSaveEnabled).toBe(false)
    expect(current().revision).toBe(0)
    expect(current().isDirty).toBe(false)
    expect(current().versions).toHaveLength(1)

    current().addNode(nodeDef('notify_1'))
    expect(current().isDirty).toBe(true)
    await vi.advanceTimersByTimeAsync(2_000)
    expect(gateway.putDraft).not.toHaveBeenCalled()
    expect(await current().flush()).toBe(false)
    expect(await current().publish()).toBe(false)
    expect(current().error).toContain('只读版本视图')
  })

  it('实例绑定的文档直接铺进画布，同样关掉自动保存', () => {
    bootstrap()
    current().bindInstanceDocument(workflowDocument({ name: '实例版' }))
    expect(current().document?.name).toBe('实例版')
    expect(current().autoSaveEnabled).toBe(false)
    expect(current().revision).toBe(0)
    expect(current().isDirty).toBe(false)
    expect(current().saveState.kind).toBe('clean')
  })

  it('先发后回的打开结果不许盖掉最后打开的那份', async () => {
    const slow = deferred<WorkflowDraft>()
    const SLOW = 'wf_aaaaaaaaaaaa'
    const FAST = 'wf_bbbbbbbbbbbb'
    bootstrap({
      getDraft: vi.fn((lineage: string) =>
        lineage === SLOW
          ? slow.promise
          : Promise.resolve(draftOf(workflowDocument({ name: '快的那条' }), { revision: 5 })),
      ),
      listVersions: vi.fn(async () => []),
    })

    const opening = current().openLineage(SLOW)
    await Promise.resolve()
    expect(await current().openLineage(FAST)).toBe(true)
    expect(current().document?.name).toBe('快的那条')

    slow.resolve(draftOf(workflowDocument({ name: '慢的那条' }), { revision: 9 }))
    expect(await opening).toBe(true)
    expect(current().document?.name).toBe('快的那条')
    expect(current().revision).toBe(5)
  })
})

describe('文档层：发布与版本恢复', () => {
  it('发布前先把手上的改动落盘，再把新 revision 交给 publish', async () => {
    vi.useFakeTimers()
    bootstrap()
    expect(await current().openLineage(LINEAGE)).toBe(true)
    current().setMeta({ name: '改过的链路' })

    expect(await current().publish()).toBe(true)
    expect(gateway.putDraft).toHaveBeenCalledTimes(1)
    expect(gateway.putDraft.mock.calls[0]?.[1].base_revision).toBe(2)
    expect(gateway.publish).toHaveBeenCalledWith(LINEAGE, 3)
    expect(gateway.getDraft).toHaveBeenCalledTimes(2)
    expect(current().revision).toBe(3)
    expect(current().document?.name).toBe('改过的链路')
    expect(current().saveState).toEqual({ kind: 'clean', savedAt: UPDATED_AT })
  })

  it('本来就干净时发布不多打一趟保存', async () => {
    bootstrap()
    expect(await current().openLineage(LINEAGE)).toBe(true)
    expect(await current().publish()).toBe(true)
    expect(gateway.putDraft).not.toHaveBeenCalled()
    expect(gateway.publish).toHaveBeenCalledWith(LINEAGE, 2)
  })

  it('本地校验不过就不发布，原因写在页面上', async () => {
    bootstrap()
    expect(await current().publish()).toBe(false)
    expect(current().error).toContain('工作流至少需要一个节点')
    expect(gateway.publish).not.toHaveBeenCalled()
  })

  it('恢复旧版本会生成新草稿并刷新版本列表', async () => {
    bootstrap()
    expect(await current().openLineage(LINEAGE)).toBe(true)
    expect(await current().restoreVersion(PUBLISHED)).toBe(true)

    expect(gateway.revertToDraft).toHaveBeenCalledWith(LINEAGE, PUBLISHED)
    expect(current().autoSaveEnabled).toBe(true)
    expect(current().revision).toBe(3)
    expect(current().baseWorkflowId).toBe(PUBLISHED)
    expect(current().isDirty).toBe(false)
    expect(current().error).toBeNull()
  })

  it('还没有 lineage 时恢复版本要明说，而不是报一个不存在的接口错误', async () => {
    bootstrap()
    expect(await current().restoreVersion(PUBLISHED)).toBe(false)
    expect(current().error).toContain('还没有 lineage')
    expect(gateway.revertToDraft).not.toHaveBeenCalled()
  })
})

describe('文档层：冲突处置', () => {
  it('以服务器为准重载：冲突标志消失，本地改动丢掉', async () => {
    bootstrap({
      putDraft: vi.fn<WorkflowGateway['putDraft']>(async () => {
        throw new WorkflowError('revision_conflict', 409, '草稿已被其他会话修改', { server_revision: 3 })
      }),
    })
    expect(await current().openLineage(LINEAGE)).toBe(true)
    current().setMeta({ name: '本地改的' })
    expect(await current().flush()).toBe(false)
    expect(current().saveState).toEqual({ kind: 'conflict', serverRevision: 3, localRevision: 2 })

    expect(await current().reloadDraft()).toBe(true)
    expect(current().saveState).toEqual({ kind: 'clean', savedAt: UPDATED_AT })
    expect(current().document?.name).toBe('已存链路')
    expect(current().error).toBeNull()
    expect(current().autoSaveEnabled).toBe(true)
  })

  it('拿服务器 revision 覆盖写回：本地内容保留', async () => {
    const putDraft = vi.fn<WorkflowGateway['putDraft']>(async () => {
      throw new WorkflowError('revision_conflict', 409, '冲突', { server_revision: 9 })
    })
    bootstrap({ putDraft })
    expect(await current().openLineage(LINEAGE)).toBe(true)
    current().setMeta({ name: '本地改的' })
    expect(await current().flush()).toBe(false)

    putDraft.mockImplementation(async (lineage, payload) =>
      draftOf(payload.document, { lineage_id: lineage, revision: payload.base_revision + 1 }),
    )
    expect(await current().overwriteDraft()).toBe(true)
    const last = putDraft.mock.calls[putDraft.mock.calls.length - 1]
    expect(last?.[1].base_revision).toBe(2)
    expect(last?.[1].document.name).toBe('本地改的')
    expect(current().document?.name).toBe('本地改的')
    expect(current().isDirty).toBe(false)
    expect(current().error).toBeNull()
  })

  it('非法时间戳的 savedAt 不崩，状态栏给——:--:--', async () => {
    bootstrap({
      getDraft: vi.fn<WorkflowGateway['getDraft']>(async () =>
        draftOf(workflowDocument(), { revision: 2, updated_at: 'not-a-date' }),
      ),
    })
    expect(await current().openLineage(LINEAGE)).toBe(true)
    expect(current().statusText).toBe('草稿已自动保存 --:--:--')
  })
})

describe('文档层：版式补全与空画布保护', () => {
  it('seedPositions 只补缺不覆盖；带落点新增节点时为老节点补分层坐标', () => {
    bootstrap()
    current().addNode(nodeDef('a'))
    current().addNode(nodeDef('b'))
    current().seedPositions({ a: { x: 10, y: 20 } })
    expect(current().document?.layout.positions).toEqual({ a: { x: 10, y: 20 } })

    current().seedPositions({ a: { x: 99, y: 99 }, b: { x: 30, y: 40 } })
    expect(current().document?.layout.positions).toEqual({ a: { x: 10, y: 20 }, b: { x: 30, y: 40 } })

    current().addNode(nodeDef('c'), { x: 500, y: 600 })
    expect(current().document?.layout.positions.c).toEqual({ x: 500, y: 600 })
  })

  it('第一张带落点的新节点落地前，先把已有节点按分层铺开', () => {
    bootstrap()
    current().addNode(nodeDef('a'))
    current().addNode(nodeDef('b'), { x: 500, y: 600 })
    expect(current().document?.layout.positions).toEqual({
      a: { x: 0, y: 0 },
      b: { x: 500, y: 600 },
    })
  })

  it('删边走 update 流程，画布与定义同步', () => {
    bootstrap()
    current().addNode(nodeDef('a'))
    current().addNode(nodeDef('b'))
    current().connect('a', 'b')
    const edge = current().document?.edges[0]
    if (edge === undefined) throw new Error('连线未建立')
    current().removeEdge(edgeId(0, edge))
    expect(current().document?.edges).toEqual([])
  })

  it('画布未初始化时，所有写操作安静退出而不是抛异常', async () => {
    bootstrap()
    current().document = null

    current().seedPositions({ a: { x: 0, y: 0 } })
    current().addNode(nodeDef('a'))
    current().moveNode('a', { x: 1, y: 1 })
    current().autoLayout()
    expect(current().connect('a', 'b')).toBe(false)
    current().patchNode('a', { name: 'x' })
    current().updateEdgeCondition('e', 'high')
    current().removeEdge('e')
    current().removeNodes(['a'])
    current().syncUpstreamConfig('a')
    current().setMeta({ name: 'x' })

    expect(current().definition).toBeNull()
    expect(current().lineageId).toBe('')
    expect(current().validationErrors).toEqual(['画布尚未初始化'])
    expect(current().canSave).toBe(false)
    expect(current().canAutosave).toBe(false)
    expect(await current().saveNow()).toBe(false)
    expect(await current().flush()).toBe(false)
    expect(await current().publish()).toBe(false)
    expect(current().error).toContain('只读版本视图')

    expect(await current().restoreVersion(PUBLISHED)).toBe(false)
    expect(await current().reloadDraft()).toBe(false)
    expect(await current().overwriteDraft()).toBe(false)
    await current().refreshVersions()
    expect(current().versions).toEqual([])
  })

  it('published 回退顺序：is_current → 首行 → 打开版本时固定的那一版', async () => {
    bootstrap()
    expect(await current().openVersion(PUBLISHED)).toBe(true)
    expect(current().publishedWorkflowId).toBe(PUBLISHED)

    current().versions = [versionSummary({ workflow_id: OTHER_WORKFLOW, version: 7, is_current: false })]
    expect(current().publishedWorkflowId).toBe(OTHER_WORKFLOW)
    expect(current().publishedVersion).toBe(7)

    current().versions = []
    expect(current().publishedWorkflowId).toBe(PUBLISHED)
    expect(current().publishedVersion).toBeNull()
  })

  it('版本列表刷新：没有 lineage 不发请求；404 视为空；其它错误照实抛出', async () => {
    bootstrap()
    await current().refreshVersions()
    expect(gateway.listVersions).not.toHaveBeenCalled()

    expect(await current().openLineage(LINEAGE)).toBe(true)
    gateway.listVersions.mockRejectedValueOnce(new WorkflowError('not_found', 404, '还没有发布版本'))
    await current().refreshVersions()
    expect(current().versions).toEqual([])

    gateway.listVersions.mockRejectedValueOnce(new WorkflowError('internal', 500, '列表挂了'))
    await expect(current().refreshVersions()).rejects.toThrow('列表挂了')
  })

  it('打开时版本列表的异常不是 404：整趟打开失败，画布不动', async () => {
    bootstrap({
      listVersions: vi.fn<WorkflowGateway['listVersions']>(async () => {
        throw new WorkflowError('internal', 500, '列表挂了')
      }),
    })
    current().setMeta({ name: '本地草稿' })
    expect(await current().openLineage(LINEAGE)).toBe(false)
    expect(current().document?.name).toBe('本地草稿')
    expect(current().error).toContain('列表挂了')
  })
})

describe('文档层：保存的边界与并发', () => {
  it('改回原样后 flush 只把状态收回 clean，不再打一趟空保存', async () => {
    bootstrap()
    expect(await current().openLineage(LINEAGE)).toBe(true)
    current().setMeta({ name: '临时改名' })
    current().setMeta({ name: '已存链路' })
    expect(current().isDirty).toBe(false)
    expect(current().saveState.kind).toBe('dirty')

    expect(await current().flush()).toBe(true)
    expect(gateway.putDraft).not.toHaveBeenCalled()
    expect(current().saveState.kind).toBe('clean')
  })

  it('结构合法但名称只有空白时不自动保存', async () => {
    bootstrap()
    current().addNode(nodeDef('fetch_1'))
    current().setMeta({ name: '   ' })
    expect(current().isDirty).toBe(true)
    expect(current().canAutosave).toBe(false)
    expect(await current().saveNow()).toBe(false)
    expect(gateway.putDraft).not.toHaveBeenCalled()
  })

  it('保存进行中再次 saveNow：后一趟排队，等前一趟落地后不再重复写', async () => {
    const gate = deferred<WorkflowDraft>()
    const putDraft = vi.fn<WorkflowGateway['putDraft']>(() => gate.promise)
    bootstrap({ putDraft })
    expect(await current().openLineage(LINEAGE)).toBe(true)
    current().setMeta({ name: '改过' })

    const first = current().saveNow('manual')
    const second = current().saveNow('manual')
    const third = current().saveNow('manual')
    gate.resolve(draftOf(current().document ?? workflowDocument(), { revision: 3 }))

    expect(await first).toBe(true)
    expect(await second).toBe(true)
    expect(await third).toBe(true)
    expect(putDraft).toHaveBeenCalledTimes(1)
    expect(current().isDirty).toBe(false)
  })

  it('请求被中止时不算失败，也不写错误横幅', async () => {
    const putDraft = vi.fn<WorkflowGateway['putDraft']>(
      (_lineage, _payload, signal) =>
        new Promise((_resolve, reject) => {
          signal?.addEventListener('abort', () => {
            reject(new WorkflowError('aborted', 0, '请求已取消'))
          })
        }),
    )
    bootstrap({ putDraft })
    expect(await current().openLineage(LINEAGE)).toBe(true)
    current().setMeta({ name: '改过' })

    const saving = current().saveNow('manual')
    current().dispose()
    expect(await saving).toBe(false)
    expect(current().error).toBeNull()
  })

  it('首次保存建 lineage 期间换图：迟到的草稿不落回新画布', async () => {
    const gate = deferred<WorkflowDraft>()
    bootstrap({
      createLineage: vi.fn<WorkflowGateway['createLineage']>(() => gate.promise),
    })
    current().addNode(nodeDef('fetch_1'))
    const saving = current().saveNow('manual')
    current().resetDocument('新画布')
    gate.resolve(draftOf(workflowDocument(), { revision: 1 }))

    expect(await saving).toBe(false)
    expect(current().document?.name).toBe('新画布')
    expect(current().document?.nodes).toHaveLength(0)
  })

  it('非 WorkflowError 的保存失败按 internal 记账，不谎称可重试', async () => {
    bootstrap({
      putDraft: vi.fn<WorkflowGateway['putDraft']>(async () => {
        throw new Error('boom')
      }),
    })
    current().addNode(nodeDef('fetch_1'))
    expect(await current().saveNow('manual')).toBe(false)
    expect(current().saveState).toEqual({ kind: 'error', code: 'internal', retryable: false })
    expect(current().error).toContain('boom')
  })

  it('冲突回执没带 server_revision 时退化为本地 revision', async () => {
    bootstrap({
      putDraft: vi.fn<WorkflowGateway['putDraft']>(async () => {
        throw new WorkflowError('revision_conflict', 409, '冲突')
      }),
    })
    expect(await current().openLineage(LINEAGE)).toBe(true)
    current().setMeta({ name: '改过' })
    expect(await current().flush()).toBe(false)
    expect(current().saveState).toEqual({ kind: 'conflict', serverRevision: 2, localRevision: 2 })
  })
})

describe('文档层：发布与覆盖失败的出口', () => {
  it('保存失败时发布会停在保存这一步，不再调用 publish', async () => {
    bootstrap({
      putDraft: vi.fn<WorkflowGateway['putDraft']>(async () => {
        throw new WorkflowError('revision_conflict', 409, '冲突', { server_revision: 9 })
      }),
    })
    expect(await current().openLineage(LINEAGE)).toBe(true)
    current().setMeta({ name: '改过' })
    expect(await current().publish()).toBe(false)
    expect(gateway.publish).not.toHaveBeenCalled()
    expect(current().saveState.kind).toBe('conflict')
  })

  it('publish 接口失败时把原因写进页面，loading 回落', async () => {
    bootstrap({
      publish: vi.fn<WorkflowGateway['publish']>(async () => {
        throw new WorkflowError('archived', 409, '流程已归档')
      }),
    })
    expect(await current().openLineage(LINEAGE)).toBe(true)
    expect(await current().publish()).toBe(false)
    expect(current().error).toContain('发布失败')
    expect(current().error).toContain('流程已归档')
    expect(current().loading).toBe(false)
  })

  it('覆盖写回失败与没有 lineage 时不制造假成功', async () => {
    bootstrap()
    expect(await current().reloadDraft()).toBe(false)
    expect(await current().overwriteDraft()).toBe(false)

    expect(await current().openLineage(LINEAGE)).toBe(true)
    current().setMeta({ name: '本地改的' })
    gateway.getDraft.mockRejectedValueOnce(new WorkflowError('internal', 500, '草稿读不出来'))
    expect(await current().overwriteDraft()).toBe(false)
    expect(current().error).toContain('覆盖草稿失败')
    expect(current().error).toContain('草稿读不出来')
  })
})
