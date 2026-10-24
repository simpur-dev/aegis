import { reactive } from 'vue'
import { describe, expect, it } from 'vitest'

import {
  decodeDecisionResult,
  decodeDefinitionsResponse,
  decodeInstancesResponse,
  decodeNodeTypesResponse,
  decodeRenameLineageResult,
  decodeVersionList,
  decodeWorkflowDef,
  decodeWorkflowDocument,
  decodeWorkflowDraft,
  decodeWorkflowError,
  toNodeInput,
  toNodeInputs,
} from '@/api/workflow/codec'
import { WorkflowError } from '@/api/workflow/contract'
import type { NodeDef } from '@/utils/graph'
import {
  MAX_STICKY_NOTES,
  autoLayoutDocument,
  canonicalDocumentJson,
  documentFromDefinition,
  documentsEqual,
  ensureDocumentLayout,
  validateDocument,
  type WorkflowDocument,
} from '@/utils/graph/document'

const WF = 'wf_0123456789ab'
const WF2 = 'wf_0123456789cd'

function node(overrides: Partial<NodeDef> = {}): NodeDef {
  return {
    node_id: 'fetch_1',
    type: 'data_fetch',
    name: '数据接入',
    config: { region_code: 'RG-01', limit: 10 },
    sla_ms: 5_000,
    timeout_ms: 4_000,
    on_failure: 'retry',
    retry: { max_attempts: 2, backoff_ms: 300 },
    ...overrides,
  }
}

function wireNode(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return { ...node(), ...overrides }
}

function wireDefinition(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    workflow_id: WF,
    lineage_id: WF,
    schema_version: 1,
    name: '链路',
    description: '',
    version: 1,
    nodes: [wireNode()],
    edges: [],
    created_by: 'platform.workflow',
    created_at: '2026-10-22T00:00:00Z',
    status: 'active',
    content_hash: 'sha256:abc',
    ...overrides,
  }
}

function wireDocument(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    schema_version: 1,
    lineage_id: WF,
    name: '链路',
    description: '',
    nodes: [
      wireNode({ node_id: 'start', type: 'start', config: { variables: [] } }),
      wireNode({ node_id: 'end', type: 'end', config: {} }),
    ],
    edges: [{ source: 'start', target: 'end', condition: '' }],
    layout: {
      positions: { start: { x: 0, y: 0 }, end: { x: 280, y: 0 }, ghost: { x: 1, y: 1 } },
      viewport: { x: 0, y: 0, zoom: 1 },
      notes: [],
    },
    ...overrides,
  }
}

/** 坏响应必须是不带路径猜谜的 invalid_response，且路径点名到出问题的那一格。 */
function expectRejected(run: () => unknown, path: string): void {
  expect(run).toThrowError(
    expect.objectContaining({
      code: 'invalid_response',
      message: expect.stringContaining(path),
    }),
  )
}

describe('codec：响应逐字段解析', () => {
  it('定义：保留 lineage/时间/哈希，未知节点类型按原串降级', () => {
    const def = decodeWorkflowDef(wireDefinition({ nodes: [wireNode({ type: 'future_node' })] }))
    expect(def.workflow_id).toBe(WF)
    expect(def.lineage_id).toBe(WF)
    expect(def.schema_version).toBe(1)
    expect(def.nodes[0]?.type).toBe('future_node')
  })

  it('定义：缺 retry 时点名到 nodes[0].retry', () => {
    const payload = wireDefinition()
    const nodes = payload.nodes as Record<string, unknown>[]
    delete nodes[0]?.retry
    let caught: unknown
    try {
      decodeWorkflowDef(payload)
    } catch (error) {
      caught = error
    }
    expect(caught).toBeInstanceOf(WorkflowError)
    expect((caught as WorkflowError).message).toContain('nodes[0].retry')
    expect((caught as WorkflowError).code).toBe('invalid_response')
  })

  it('文档：剪掉已删除节点的坐标，保留真实坐标', () => {
    const document = decodeWorkflowDocument(wireDocument())
    expect(Object.keys(document.layout.positions).sort()).toEqual(['end', 'start'])
  })

  it('文档：schema_version 不受支持时给专项错误码', () => {
    expect(() => decodeWorkflowDocument(wireDocument({ schema_version: 2 }))).toThrowError(
      expect.objectContaining({ code: 'invalid_schema_version' }),
    )
  })

  it('文档：布局缩放越界带路径', () => {
    const payload = wireDocument()
    const layout = payload.layout as Record<string, unknown>
    layout.viewport = { x: 0, y: 0, zoom: 9 }
    expect(() => decodeWorkflowDocument(payload)).toThrowError(
      expect.objectContaining({ message: expect.stringContaining('layout.viewport.zoom') }),
    )
  })

  it('文档：备注超过上限被拒且路径点名', () => {
    const note = { note_id: 'note_1', x: 0, y: 0, width: 220, height: 140, text: '', color: 'amber' }
    const payload = wireDocument()
    const layout = payload.layout as Record<string, unknown>
    layout.notes = Array.from({ length: MAX_STICKY_NOTES + 1 }, (_, index) => ({
      ...note,
      note_id: `note_${index + 1}`,
    }))
    expect(() => decodeWorkflowDocument(payload)).toThrowError(
      expect.objectContaining({ message: expect.stringContaining('layout.notes') }),
    )
  })

  it('草稿：document.lineage_id 与草稿不一致时报错', () => {
    const draft = {
      lineage_id: WF,
      base_workflow_id: null,
      revision: 1,
      document: wireDocument({ lineage_id: WF2 }),
      updated_at: '2026-10-22T00:00:00Z',
    }
    expect(() => decodeWorkflowDraft(draft)).toThrowError(
      expect.objectContaining({ message: expect.stringContaining('document.lineage_id') }),
    )
  })

  it('版本列表：count 与 items 不一致视为坏响应', () => {
    const row = {
      workflow_id: WF,
      lineage_id: WF,
      version: 1,
      name: '链路',
      description: '',
      status: 'active',
      node_count: 2,
      edge_count: 1,
      created_at: '2026-10-22T00:00:00Z',
      content_hash: 'sha256:abc',
      is_current: true,
    }
    expect(() => decodeVersionList({ count: 2, items: [row] })).toThrowError(
      expect.objectContaining({ message: expect.stringContaining('versions.count') }),
    )
    expect(decodeVersionList({ count: 1, items: [row] })[0]?.is_current).toBe(true)
  })

  it('定义列表：带上 lineage 与哈希', () => {
    const decoded = decodeDefinitionsResponse({
      count: 1,
      items: [
        {
          workflow_id: WF,
          lineage_id: WF2,
          name: '链路',
          version: 3,
          status: 'archived',
          description: '说明',
          node_count: 2,
          edge_count: 1,
          created_at: '2026-10-22T00:00:00Z',
          content_hash: 'sha256:abc',
        },
      ],
    })
    expect(decoded.items[0]?.lineage_id).toBe(WF2)
    expect(decoded.items[0]?.status).toBe('archived')
  })

  it('节点面板：count 与 items 对不上时失败', () => {
    expect(() =>
      decodeNodeTypesResponse({
        count: 2,
        items: [
          {
            type: 'notify',
            description: '',
            category: 'io',
            required_config: [],
            optional_config: [],
            min_config: {},
            outputs: ['notified'],
          },
        ],
      }),
    ).toThrowError(expect.objectContaining({ message: expect.stringContaining('node-types.count') }))
  })

  it('节点面板：类别/下限/产出键逐格收严，坏字段带路径', () => {
    const row = {
      type: 'degrade_to_rule',
      description: '规则降级',
      category: 'intelligence',
      required_config: [],
      optional_config: ['risk_level'],
      min_config: { risk_level: 1 },
      outputs: ['risk_level', 'degraded'],
    }
    const decoded = decodeNodeTypesResponse({ count: 1, items: [row] })
    expect(decoded.items[0]?.category).toBe('intelligence')
    expect(decoded.items[0]?.min_config).toEqual({ risk_level: 1 })
    expect(decoded.items[0]?.outputs).toEqual(['risk_level', 'degraded'])
    expectRejected(
      () => decodeNodeTypesResponse({ count: 1, items: [{ ...row, category: 'misc' }] }),
      'node-types.items[0].category',
    )
    expectRejected(
      () => decodeNodeTypesResponse({ count: 1, items: [{ ...row, min_config: { risk_level: '1' } }] }),
      'node-types.items[0].min_config.risk_level',
    )
    expectRejected(
      () => decodeNodeTypesResponse({ count: 1, items: [{ ...row, outputs: [1] }] }),
      'node-types.items[0].outputs[0]',
    )
  })

  it('实例：payload 与节点运行态都按 JSON 解析', () => {
    const detail = decodeDecisionResult({
      instance_id: 'wfi_1',
      workflow_id: WF,
      workflow_version: 1,
      trace_id: 'trc_1',
      status: 'waiting',
      error: null,
      payload: { region_code: 'RG-01', rows: [{ metric: 'rain', value: 12.5 }] },
      nodes: [
        {
          node_id: 'review_1',
          type: 'human_review',
          state: 'awaiting_human',
          attempts: 1,
          schedule_latency_ms: 0,
          duration_ms: null,
          output: { ok: true },
          error: null,
          notes: ['等待签字'],
        },
      ],
      标注沉淀: {
        status: '已沉淀',
        case_id: 'case_1',
        真值: '报=1 等级=2',
        样本: { observed_at: '2026-10-22T00:00:00Z' },
      },
    })
    expect(detail.payload.region_code).toBe('RG-01')
    expect(detail.nodes[0]?.state).toBe('awaiting_human')
    expect(detail.标注沉淀?.case_id).toBe('case_1')
  })

  it('实例：未知状态被拒', () => {
    expect(() =>
      decodeDecisionResult({
        instance_id: 'wfi_1',
        workflow_id: WF,
        workflow_version: 1,
        trace_id: 'trc_1',
        status: 'exploded',
        error: null,
        nodes: [],
      }),
    ).toThrowError(expect.objectContaining({ message: expect.stringContaining('instance.status') }))
  })

  it('请求体：节点名裁掉首尾空白', () => {
    expect(toNodeInput(node({ name: ' 数据接入 ' })).name).toBe('数据接入')
  })
})

describe('codec：错误体归一化', () => {
  it('新统一错误体保留码与 details', () => {
    const decoded = decodeWorkflowError(
      { detail: { code: 'revision_conflict', message: '草稿已被其他会话修改', details: { server_revision: 4 } } },
      409,
    )
    expect(decoded.code).toBe('revision_conflict')
    expect(decoded.message).toBe('草稿已被其他会话修改')
    expect(decoded.details.server_revision).toBe(4)
  })

  it('新 422 的字段错误并进 message', () => {
    const decoded = decodeWorkflowError(
      {
        detail: {
          code: 'validation_failed',
          message: '请求校验失败',
          details: {
            errors: [
              {
                loc: ['body', 'document', 'name'],
                msg: 'String should have at least 1 character',
                type: 'string_too_short',
              },
            ],
          },
        },
      },
      422,
    )
    expect(decoded.message).toContain('请求校验失败')
    expect(decoded.message).toContain('document.name')
  })

  it('旧端点的字符串 detail 按状态给码', () => {
    const decoded = decodeWorkflowError({ detail: '工作流图存在环' }, 400)
    expect(decoded.code).toBe('validation_failed')
    expect(decoded.message).toBe('工作流图存在环')
  })

  it('旧端点的 422 数组保留定位', () => {
    const decoded = decodeWorkflowError(
      {
        detail: [{ loc: ['body', 'name'], msg: 'String should have at least 2 characters', type: 'string_too_short' }],
      },
      422,
    )
    expect(decoded.code).toBe('validation_failed')
    expect(decoded.message).toContain('流程名称')
  })
})

describe('document：本地体检与版式', () => {
  function doc(overrides: Record<string, unknown> = {}): WorkflowDocument {
    return decodeWorkflowDocument(wireDocument({ ...overrides }))
  }

  it('合法文档无错误；Start 有入边 / 不可达节点会点名', () => {
    expect(validateDocument(doc())).toEqual([])
    const broken = doc({
      edges: [
        { source: 'start', target: 'end', condition: '' },
        { source: 'end', target: 'start', condition: '' },
      ],
    })
    const errors = validateDocument(broken)
    expect(errors.some((error) => error.includes('start 节点 start 不允许有入边'))).toBe(true)
  })

  it('没有 layout 的旧定义自动布局', () => {
    const definition = decodeWorkflowDef(
      wireDefinition({
        nodes: [wireNode({ node_id: 'a' }), wireNode({ node_id: 'b', type: 'notify' })],
        edges: [{ source: 'a', target: 'b', condition: '' }],
      }),
    )
    const document = documentFromDefinition(definition)
    expect(document.layout.positions).toEqual({})
    const laidOut = ensureDocumentLayout(document)
    expect(laidOut.layout.positions.a).toEqual({ x: 0, y: 0 })
    expect(laidOut.layout.positions.b).toEqual({ x: 280, y: 0 })
  })

  it('残留坐标和未知节点键被剪掉', () => {
    const laid = ensureDocumentLayout(
      doc({
        layout: {
          positions: { start: { x: 5, y: 6 }, ghost: { x: 1, y: 1 } },
          viewport: null,
          notes: [],
        },
      }),
    )
    expect(laid.layout.positions).toEqual({ start: { x: 5, y: 6 } })
  })

  it('有环时自动布局退回网格而不是抛错', () => {
    const cyclic = doc({
      nodes: [wireNode({ node_id: 'a' }), wireNode({ node_id: 'b', type: 'notify' })],
      edges: [
        { source: 'a', target: 'b', condition: '' },
        { source: 'b', target: 'a', condition: '' },
      ],
      layout: { positions: {}, viewport: null, notes: [] },
    })
    expect(autoLayoutDocument(cyclic).layout.positions).toEqual({
      a: { x: 0, y: 0 },
      b: { x: 280, y: 0 },
    })
  })

  it('脏比较忽略对象键序', () => {
    const left = doc()
    const right = decodeWorkflowDocument({
      layout: left.layout,
      edges: left.edges,
      nodes: left.nodes,
      description: left.description,
      name: left.name,
      lineage_id: left.lineage_id,
      schema_version: left.schema_version,
    })
    expect(canonicalDocumentJson(left)).toBe(canonicalDocumentJson(right))
    expect(documentsEqual(left, right)).toBe(true)
    expect(documentsEqual(left, null)).toBe(false)
  })
})

describe('codec：每个坏格子都要点名', () => {
  const badDoc = (overrides: Record<string, unknown>): Record<string, unknown> => wireDocument(overrides)

  it('数组形状、数量上限与空节点', () => {
    expectRejected(() => decodeWorkflowDocument(badDoc({ nodes: 'nope' })), 'document.nodes')
    expectRejected(() => decodeWorkflowDocument(badDoc({ edges: 'nope' })), 'document.edges')
    expectRejected(() => decodeWorkflowDocument(badDoc({ nodes: [] })), 'document.nodes')
  })

  it('字符串的最小/最大长度逐格检查', () => {
    expectRejected(() => decodeWorkflowDocument(badDoc({ name: '' })), 'document.name')
    expectRejected(() => decodeWorkflowDocument(badDoc({ description: 'x'.repeat(513) })), 'document.description')
    expectRejected(() => decodeWorkflowDocument(badDoc({ lineage_id: 'wf_short' })), 'document.lineage_id')
    expectRejected(
      () => decodeWorkflowDocument(badDoc({ nodes: [wireNode({ name: 'x'.repeat(65) })] })),
      'nodes[0].name',
    )
    expectRejected(
      () => decodeWorkflowDocument(badDoc({ edges: [{ source: '', target: 'end', condition: '' }] })),
      'edges[0].source',
    )
  })

  it('节点类型、节点 ID、失败策略、整数范围与 timeout 关系', () => {
    expectRejected(() => decodeWorkflowDocument(badDoc({ nodes: [wireNode({ type: 'Bad-Type' })] })), 'nodes[0].type')
    expectRejected(
      () => decodeWorkflowDocument(badDoc({ nodes: [wireNode({ node_id: 'Bad-ID' })] })),
      'nodes[0].node_id',
    )
    expectRejected(
      () => decodeWorkflowDocument(badDoc({ nodes: [wireNode({ on_failure: 'explode' })] })),
      'nodes[0].on_failure',
    )
    expectRejected(
      () => decodeWorkflowDocument(badDoc({ nodes: [wireNode({ retry: { max_attempts: 6, backoff_ms: 0 } })] })),
      'nodes[0].retry.max_attempts',
    )
    expectRejected(
      () => decodeWorkflowDocument(badDoc({ nodes: [wireNode({ sla_ms: 500, timeout_ms: 5_000 })] })),
      'nodes[0].timeout_ms',
    )
  })

  it('config 里非 JSON 值、NaN 与原型污染键都拒收', () => {
    expectRejected(
      () => decodeWorkflowDocument(badDoc({ nodes: [wireNode({ config: { value: undefined } })] })),
      'nodes[0].config.value',
    )
    expectRejected(
      () => decodeWorkflowDocument(badDoc({ nodes: [wireNode({ config: { value: Number.NaN } })] })),
      'nodes[0].config.value',
    )
    const polluted = JSON.parse('{"__proto__":{"polluted":true}}') as Record<string, unknown>
    expectRejected(
      () => decodeWorkflowDocument(badDoc({ nodes: [wireNode({ config: polluted })] })),
      'nodes[0].config.__proto__',
    )
  })

  it('版式：坐标键长度、重复备注、备注字段与未知颜色', () => {
    expectRejected(
      () =>
        decodeWorkflowDocument(badDoc({ layout: { positions: { '': { x: 0, y: 0 } }, viewport: null, notes: [] } })),
      'layout.positions',
    )
    expectRejected(
      () =>
        decodeWorkflowDocument(
          badDoc({
            layout: { positions: { [`${'x'.repeat(33)}`]: { x: 0, y: 0 } }, viewport: null, notes: [] },
          }),
        ),
      'layout.positions',
    )
    const note = { note_id: 'note_1', x: 0, y: 0, width: 220, height: 140, text: '', color: 'amber' }
    expectRejected(
      () => decodeWorkflowDocument(badDoc({ layout: { positions: {}, viewport: null, notes: [note, note] } })),
      'layout.notes',
    )
    expectRejected(
      () =>
        decodeWorkflowDocument(
          badDoc({ layout: { positions: {}, viewport: null, notes: [{ ...note, note_id: 'Bad' }] } }),
        ),
      'layout.notes[0].note_id',
    )
    expectRejected(
      () =>
        decodeWorkflowDocument(badDoc({ layout: { positions: {}, viewport: null, notes: [{ ...note, width: 10 }] } })),
      'layout.notes[0].width',
    )
    expectRejected(
      () =>
        decodeWorkflowDocument(
          badDoc({ layout: { positions: {}, viewport: null, notes: [{ ...note, color: 'red' }] } }),
        ),
      'layout.notes[0].color',
    )
    expectRejected(
      () =>
        decodeWorkflowDocument(
          badDoc({ layout: { positions: {}, viewport: { x: 0, y: 0, zoom: Number.NaN }, notes: [] } }),
        ),
      'layout.viewport.zoom',
    )
  })

  it('定义：lineage 缺省取 workflow_id，schema_version 缺省取 1，非法身份字段照常点名', () => {
    const base = wireDefinition()
    delete base.schema_version
    delete base.lineage_id
    const decoded = decodeWorkflowDef(base)
    expect(decoded.schema_version).toBe(1)
    expect(decoded.lineage_id).toBe(WF)

    expectRejected(() => decodeWorkflowDef(wireDefinition({ workflow_id: 'wf_bad' })), 'workflow_id')
    expectRejected(() => decodeWorkflowDef(wireDefinition({ lineage_id: 'wf_bad' })), 'lineage_id')
    expectRejected(() => decodeWorkflowDef(wireDefinition({ status: 'draft' })), 'status')
    expectRejected(() => decodeWorkflowDef(wireDefinition({ version: 0 })), 'version')
    expectRejected(() => decodeWorkflowDef(wireDefinition({ nodes: 'nope' })), 'nodes')
  })

  it('文档必填 schema_version；草稿的 base_workflow_id 可空但不可坏', () => {
    const missingSchema = wireDocument()
    delete missingSchema.schema_version
    expectRejected(() => decodeWorkflowDocument(missingSchema), 'schema_version')
    expectRejected(
      () =>
        decodeWorkflowDraft({
          lineage_id: WF,
          base_workflow_id: 'wf_bad',
          revision: 1,
          document: wireDocument(),
          updated_at: '2026-10-22T00:00:00Z',
        }),
      'base_workflow_id',
    )
    expectRejected(
      () =>
        decodeWorkflowDraft({
          lineage_id: WF,
          base_workflow_id: null,
          revision: 0,
          document: wireDocument(),
          updated_at: '2026-10-22T00:00:00Z',
        }),
      'revision',
    )
  })

  it('版本行与实例行的每一格身份都要过校验', () => {
    const row = {
      workflow_id: WF,
      lineage_id: WF,
      version: 1,
      name: '链路',
      description: '',
      status: 'active',
      node_count: 2,
      edge_count: 1,
      created_at: '2026-10-22T00:00:00Z',
      content_hash: 'sha256:abc',
      is_current: true,
    }
    expectRejected(() => decodeVersionList({ count: 1, items: [{ ...row, workflow_id: 'wf_bad' }] }), 'workflow_id')
    expectRejected(() => decodeVersionList({ count: 1, items: [{ ...row, lineage_id: 'wf_bad' }] }), 'lineage_id')
    expectRejected(() => decodeVersionList({ count: 1, items: [{ ...row, status: 'draft' }] }), 'status')
    expectRejected(() => decodeVersionList({ count: 1, items: [{ ...row, is_current: 'yes' }] }), 'is_current')

    const detail = {
      instance_id: 'wfi_1',
      workflow_id: WF,
      workflow_version: 1,
      trace_id: 'trc_1',
      status: 'waiting',
      error: null,
      nodes: [],
    }
    expect(decodeDecisionResult(detail).payload).toEqual({})
    const withError = decodeDecisionResult({
      ...detail,
      error: '上游失败',
      payload: {},
      nodes: [
        {
          node_id: 'n_1',
          type: 'notify',
          state: 'failed',
          attempts: 1,
          schedule_latency_ms: null,
          duration_ms: 10,
          output: {},
          error: '上游失败',
          notes: [],
        },
      ],
    })
    expect(withError.error).toBe('上游失败')
    expect(withError.nodes[0]?.duration_ms).toBe(10)
    expect(withError.nodes[0]?.schedule_latency_ms).toBeNull()
  })

  it('实例列表的 count 与 items 对不上时报错', () => {
    const detail = {
      instance_id: 'wfi_1',
      workflow_id: WF,
      workflow_version: 1,
      trace_id: 'trc_1',
      status: 'waiting',
      error: null,
      nodes: [],
    }
    expect(decodeInstancesResponse({ count: 1, items: [detail] }).items).toHaveLength(1)
    expectRejected(() => decodeInstancesResponse({ count: 2, items: [detail] }), 'instances.count')
  })

  it('改名回执：draft 可空，也可带完整草稿', () => {
    const base = { workflow_id: WF, lineage_id: WF, name: '新名', version: 2 }
    expect(decodeRenameLineageResult({ ...base, draft: null }).draft).toBeNull()
    const withDraft = decodeRenameLineageResult({
      ...base,
      draft: {
        lineage_id: WF,
        base_workflow_id: null,
        revision: 1,
        document: wireDocument(),
        updated_at: '2026-10-22T00:00:00Z',
      },
    })
    expect(withDraft.draft?.revision).toBe(1)
  })
})

describe('codec：错误体兜底与请求体', () => {
  it('404/网络空体也要有人能读的一句话，不能是空串', () => {
    expect(decodeWorkflowError(undefined, 404)).toMatchObject({ code: 'not_found' })
    expect(decodeWorkflowError(undefined, 404).message.length).toBeGreaterThan(0)
    expect(decodeWorkflowError(undefined, 500)).toMatchObject({ code: 'internal' })
    expect(decodeWorkflowError(undefined, 500).message.length).toBeGreaterThan(0)
    expect(decodeWorkflowError({ detail: null }, 500).message.length).toBeGreaterThan(0)
  })

  it('新错误体里 code/message/details 各自坏掉时逐格兜底', () => {
    const noCode = decodeWorkflowError({ detail: { message: '被拒了' } }, 409)
    expect(noCode.code).toBe('name_conflict')
    expect(noCode.message).toBe('被拒了')

    const unknownCode = decodeWorkflowError({ detail: { code: 'mystery', message: '被拒了' } }, 400)
    expect(unknownCode.code).toBe('validation_failed')

    const blankMessage = decodeWorkflowError({ detail: { code: 'archived', message: '   ' } }, 409)
    expect(blankMessage.message.length).toBeGreaterThan(0)

    const badDetails = decodeWorkflowError({ detail: { code: 'archived', message: '已归档', details: 'nope' } }, 409)
    expect(badDetails.details).toEqual({})

    const emptyFieldErrors = decodeWorkflowError(
      { detail: { code: 'validation_failed', message: '校验失败', details: { errors: [] } } },
      422,
    )
    expect(emptyFieldErrors.message).toBe('校验失败')

    const unknownShape = decodeWorkflowError({ mystery: 1 }, 418)
    expect(unknownShape.code).toBe('internal')
    expect(unknownShape.message).toContain('mystery=1')
  })

  it('toNodeInput 是深拷贝，响应式代理也能安全发送', () => {
    const source = reactive(node({ config: { nested: { value: 1 }, tags: ['a'] } }))
    const input = toNodeInput(source)
    expect(input.config).toEqual({ nested: { value: 1 }, tags: ['a'] })
    expect(input.config).not.toBe(source.config)
    const nested = input.config.nested
    if (nested !== null && typeof nested === 'object' && !Array.isArray(nested)) nested.value = 2
    expect(source.config.nested).toEqual({ value: 1 })

    expect(toNodeInputs([node({ node_id: 'a' }), node({ node_id: 'b' })])).toHaveLength(2)
  })
})
