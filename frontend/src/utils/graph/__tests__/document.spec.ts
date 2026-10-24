import { describe, expect, it } from 'vitest'

import type { EdgeDef, JsonValue, NodeDef, WorkflowDef } from '@/utils/graph'
import {
  MAX_DOCUMENT_EDGES,
  MAX_DOCUMENT_NODES,
  MAX_NODE_CONFIG_BYTES,
  MAX_START_VARIABLES,
  autoLayoutDocument,
  canonicalDocumentJson,
  copyEdgeDef,
  copyLayout,
  copyNodeDef,
  definitionFromDocument,
  documentByteLength,
  documentFromDefinition,
  documentsEqual,
  emptyLayout,
  ensureDocumentLayout,
  prunePositions,
  validateDocument,
  type StickyNote,
  type WorkflowDocument,
} from '@/utils/graph/document'

const WF = 'wf_0123456789ab'
const WF2 = 'wf_0123456789cd'

function node(nodeId: string, overrides: Partial<NodeDef> = {}): NodeDef {
  return {
    node_id: nodeId,
    type: 'data_fetch',
    name: nodeId,
    config: {},
    sla_ms: 5_000,
    timeout_ms: 4_000,
    on_failure: 'retry',
    retry: { max_attempts: 1, backoff_ms: 100 },
    ...overrides,
  }
}

function doc(overrides: Partial<WorkflowDocument> = {}): WorkflowDocument {
  return {
    schema_version: 1,
    lineage_id: WF,
    name: '链路',
    description: '',
    nodes: [node('fetch_1')],
    edges: [],
    layout: emptyLayout(),
    ...overrides,
  }
}

function sticky(overrides: Partial<StickyNote> = {}): StickyNote {
  return { note_id: 'note_1', x: 0, y: 0, width: 220, height: 140, text: '', color: 'amber', ...overrides }
}

function definition(overrides: Partial<WorkflowDef> = {}): WorkflowDef {
  return {
    workflow_id: WF,
    name: '旧定义',
    description: '说明',
    version: 3,
    nodes: [node('a', { config: { nested: { values: [1, 'x'] } } })],
    edges: [{ source: 'a', target: 'a', condition: '' }],
    created_by: 'platform.workflow',
    status: 'active',
    ...overrides,
  }
}

function startDoc(variables: JsonValue, extraNodes: NodeDef[] = []): WorkflowDocument {
  return doc({
    nodes: [node('start', { type: 'start', config: { variables } }), ...extraNodes],
  })
}

describe('文档投影：定义与文档互转时深拷贝', () => {
  it('旧定义没有 lineage 时按 workflow_id 自成一条', () => {
    const document = documentFromDefinition(definition())
    expect(document.lineage_id).toBe(WF)
    expect(document.nodes).toHaveLength(1)
  })

  it('已有 lineage 时保留；节点 config 与版式都是手抄的，改副本不回写原对象', () => {
    const layout = {
      positions: { a: { x: 1, y: 2 } },
      viewport: { x: 0, y: 0, zoom: 1.5 },
      notes: [sticky()],
    }
    const source = definition({ lineage_id: WF2 })
    const document = documentFromDefinition(source, layout)
    expect(document.lineage_id).toBe(WF2)

    const nested = document.nodes[0]?.config.nested
    if (nested === null || typeof nested !== 'object' || Array.isArray(nested)) throw new Error('嵌套配置丢了')
    const values = nested.values
    if (!Array.isArray(values)) throw new Error('嵌套值丢了')
    values.push(2)
    expect(source.nodes[0]?.config.nested).toEqual({ values: [1, 'x'] })

    const viewport = document.layout.viewport
    if (viewport === null) throw new Error('viewport 丢了')
    viewport.zoom = 2
    expect(layout.viewport.zoom).toBe(1.5)
    const note = document.layout.notes[0]
    if (note !== undefined) note.text = '改过'
    expect(layout.notes[0]?.text).toBe('')
  })

  it('定义缺身份字段时取平台默认值，有身份字段时原样保留', () => {
    const source = documentFromDefinition(definition())
    const defaults = definitionFromDocument(source)
    expect(defaults.workflow_id).toBe(WF)
    expect(defaults.version).toBe(1)
    expect(defaults.created_by).toBe('platform.web')
    expect(defaults.status).toBe('active')
    expect(defaults.created_at).toBeUndefined()
    expect(defaults.content_hash).toBeUndefined()

    const kept = definitionFromDocument(source, {
      workflow_id: WF2,
      version: 7,
      created_by: 'operator',
      created_at: '2026-10-22T00:00:00Z',
      status: 'archived',
      content_hash: 'sha256:abc',
    })
    expect(kept).toMatchObject({
      workflow_id: WF2,
      version: 7,
      created_by: 'operator',
      created_at: '2026-10-22T00:00:00Z',
      status: 'archived',
      content_hash: 'sha256:abc',
    })
  })

  it('copyNodeDef / copyEdgeDef / copyLayout 都是深拷贝', () => {
    const original = node('a', { config: { tags: ['x'], limit: 1 }, retry: { max_attempts: 2, backoff_ms: 300 } })
    const copied = copyNodeDef(original)
    copied.config.tags = ['y']
    copied.retry.max_attempts = 4
    expect(original.config.tags).toEqual(['x'])
    expect(original.retry.max_attempts).toBe(2)

    const edge: EdgeDef = { source: 'a', target: 'b', condition: '' }
    expect(copyEdgeDef(edge)).not.toBe(edge)

    const layout = copyLayout({ positions: { a: { x: 1, y: 2 } }, viewport: null, notes: [sticky()] })
    expect(layout.positions.a).not.toBe(undefined)
    if (layout.positions.a !== undefined) layout.positions.a.x = 9
    expect(copyLayout(layout).positions.a).toEqual({ x: 9, y: 2 })
  })
})

describe('版式：裁剪、自动布局与补全', () => {
  it('只保留仍存在的节点坐标', () => {
    const pruned = prunePositions({ a: { x: 1, y: 2 }, ghost: { x: 9, y: 9 } }, new Set(['a']))
    expect(pruned).toEqual({ a: { x: 1, y: 2 } })
  })

  it('空文档不做自动布局，返回原对象', () => {
    const empty = doc({ nodes: [] })
    expect(autoLayoutDocument(empty)).toBe(empty)
  })

  it('有环的草稿退回网格布局，不丢节点', () => {
    const cyclic = doc({
      nodes: [node('a'), node('b'), node('c')],
      edges: [
        { source: 'a', target: 'b', condition: '' },
        { source: 'b', target: 'a', condition: '' },
      ],
    })
    const laidOut = autoLayoutDocument(cyclic)
    expect(Object.keys(laidOut.layout.positions).sort()).toEqual(['a', 'b', 'c'])
    expect(laidOut.layout.positions.c).toEqual({ x: 560, y: 0 })
  })

  it('完全没有坐标的旧定义自动布局；只剩残留坐标时先剪掉再补', () => {
    const missing = ensureDocumentLayout(doc({ nodes: [node('a'), node('b')] }))
    expect(Object.keys(missing.layout.positions).sort()).toEqual(['a', 'b'])

    const stale = ensureDocumentLayout(
      doc({
        nodes: [node('a')],
        layout: { positions: { ghost: { x: 9, y: 9 } }, viewport: null, notes: [] },
      }),
    )
    expect(stale.layout.positions.a).toBeDefined()
    expect(stale.layout.positions.ghost).toBeUndefined()
  })

  it('已有坐标时只剪枝，不给没坐标的节点补位置', () => {
    const partial = ensureDocumentLayout(
      doc({
        nodes: [node('a'), node('b')],
        layout: { positions: { a: { x: 3, y: 4 } }, viewport: null, notes: [] },
      }),
    )
    expect(partial.layout.positions).toEqual({ a: { x: 3, y: 4 } })
  })
})

describe('脏比较与字节量', () => {
  it('键序不同不算改动；两边的 null 判定按同一性', () => {
    const left = doc()
    const right = doc({
      layout: { positions: {}, viewport: null, notes: [] },
    })
    expect(canonicalDocumentJson(left)).toBe(canonicalDocumentJson(right))
    expect(documentsEqual(left, right)).toBe(true)
    expect(documentsEqual(null, null)).toBe(true)
    expect(documentsEqual(left, null)).toBe(false)
  })

  it('undefined 字段在规范化时被跳过，字节数按 UTF-8 算', () => {
    const withUndefined = doc({ description: undefined as unknown as string })
    expect(canonicalDocumentJson(withUndefined)).not.toContain('undefined')
    expect(documentByteLength(doc({ name: '链路' }))).toBeGreaterThan(0)
  })
})

describe('文档体检：结构与版式规则', () => {
  it('合法文档零错误', () => {
    expect(validateDocument(doc())).toEqual([])
  })

  it('schema_version 不受支持时只报这一条，不再叠加后续噪音', () => {
    expect(validateDocument(doc({ schema_version: 2, name: '' }))).toEqual(['不支持的文档 schema_version: 2'])
  })

  it('名称/描述/lineage/节点数一条条点名', () => {
    const errors = validateDocument(
      doc({
        lineage_id: 'bad',
        name: '',
        description: 'x'.repeat(513),
        nodes: [],
      }),
    )
    expect(errors).toEqual(
      expect.arrayContaining([
        'lineage_id 形状不合法: bad',
        '工作流名称长度须在 1-64 字符之间',
        '工作流描述不得超过 512 字符',
        '工作流至少需要一个节点',
      ]),
    )
  })

  it('节点 ID 重复/非法、类型非法、超时超过 SLA、config 过大', () => {
    const errors = validateDocument(
      doc({
        nodes: [
          node('Bad-ID'),
          node('Bad-ID', { type: 'Future_Node' as NodeDef['type'], timeout_ms: 6_000 }),
          node('big', { config: { blob: 'x'.repeat(MAX_NODE_CONFIG_BYTES + 1) } }),
          node('bad_timeout', { sla_ms: 500, timeout_ms: 600 }),
        ],
      }),
    )
    expect(errors).toEqual(
      expect.arrayContaining([
        '节点 ID 重复',
        '节点 ID 非法: Bad-ID',
        '节点类型非法: Future_Node',
        '节点 [Bad-ID] timeout_ms 不得大于 sla_ms',
        expect.stringContaining('config 超过'),
        '节点 [bad_timeout] timeout_ms 不得大于 sla_ms',
      ]),
    )
  })

  it('节点与连线数量超过上限时给出实际数量', () => {
    const tooManyNodes = validateDocument(
      doc({ nodes: Array.from({ length: MAX_DOCUMENT_NODES + 1 }, (_, index) => node(`n_${index}`)) }),
    )
    expect(tooManyNodes).toContain(`节点数不得超过 ${MAX_DOCUMENT_NODES}`)

    const tooManyEdges = validateDocument(
      doc({
        nodes: [node('a'), node('b')],
        edges: Array.from({ length: MAX_DOCUMENT_EDGES + 1 }, () => ({ source: 'a', target: 'b', condition: '' })),
      }),
    )
    expect(tooManyEdges).toContain(`连线数不得超过 ${MAX_DOCUMENT_EDGES}`)
  })

  it('悬空边、自环、多个 end、start 有入边、end 有出边都报出来', () => {
    const errors = validateDocument(
      doc({
        nodes: [node('start', { type: 'start' }), node('end_1', { type: 'end' }), node('end_2', { type: 'end' })],
        edges: [
          { source: 'ghost', target: 'end_1', condition: '' },
          { source: 'start', target: 'ghost', condition: '' },
          { source: 'end_1', target: 'end_1', condition: '' },
          { source: 'end_1', target: 'start', condition: '' },
        ],
      }),
    )
    expect(errors).toEqual(
      expect.arrayContaining([
        '边的源节点不存在: ghost',
        '边的目标节点不存在: ghost',
        '不允许自环: end_1',
        'end 节点至多一个',
        'start 节点 start 不允许有入边',
        'end 节点 end_1 不允许有出边',
      ]),
    )
  })

  it('存在 start 时它必须是唯一根，且所有节点可达', () => {
    const errors = validateDocument(
      doc({
        nodes: [node('start', { type: 'start' }), node('a'), node('b'), node('island'), node('end', { type: 'end' })],
        edges: [
          { source: 'start', target: 'a', condition: '' },
          { source: 'start', target: 'b', condition: '' },
          { source: 'a', target: 'end', condition: '' },
        ],
      }),
    )
    expect(errors).toEqual(
      expect.arrayContaining([
        '存在 start 时它必须是唯一根节点，当前根节点: island、start',
        '这些节点从 start 不可达: island',
      ]),
    )
  })

  it('结构都合法后才继续查环', () => {
    const errors = validateDocument(
      doc({
        nodes: [node('start', { type: 'start' }), node('a'), node('b'), node('end', { type: 'end' })],
        edges: [
          { source: 'start', target: 'a', condition: '' },
          { source: 'a', target: 'b', condition: '' },
          { source: 'b', target: 'a', condition: '' },
          { source: 'a', target: 'end', condition: '' },
        ],
      }),
    )
    expect(errors).toEqual(['工作流图存在环: a → b → a'])
  })

  it('全部合法时仍拦下超过 2MB 的文档', () => {
    const blob = 'x'.repeat(MAX_NODE_CONFIG_BYTES - 16)
    const nodes = Array.from({ length: 36 }, (_, index) => node(`n_${index}`, { config: { blob } }))
    const errors = validateDocument(doc({ nodes }))
    expect(errors).toEqual([expect.stringContaining('文档序列化后超过')])
  })
})

describe('Start 变量声明', () => {
  it('四种类型与默认值都合法时零错误', () => {
    expect(
      validateDocument(
        startDoc([
          { name: 'region', type: 'string', default: 'RG-01' },
          { name: 'count', type: 'number', default: 3 },
          { name: 'enabled', type: 'boolean', default: false },
          { name: 'payload', type: 'json', default: { a: 1 } },
          { name: 'no_type' },
          { name: 'no_default', type: 'string' },
        ]),
      ),
    ).toEqual([])
  })

  it('至多一个 start，且 variables 必须是数组', () => {
    const errors = validateDocument(
      doc({
        nodes: [node('start_1', { type: 'start' }), node('start_2', { type: 'start', config: { variables: 'nope' } })],
      }),
    )
    expect(errors).toContain('start 节点至多一个')
    expect(errors).toContain('start 节点 start_2 的 variables 必须是数组')
  })

  it('变量名非法/重复/原型键，类型与默认值不匹配都点名到下标', () => {
    const errors = validateDocument(
      startDoc([
        'not-an-object',
        { name: '1bad', type: 'string' },
        { name: '__proto__', type: 'string' },
        { name: 'dup', type: 'string' },
        { name: 'dup', type: 'string' },
        { name: 'bad_type', type: 'date' },
        { name: 'str_default', type: 'string', default: 1 },
        { name: 'num_default', type: 'number', default: Number.POSITIVE_INFINITY },
        { name: 'bool_default', type: 'boolean', default: 'yes' },
      ]),
    )
    expect(errors).toEqual(
      expect.arrayContaining([
        'start.variables[0] 必须是对象',
        'start.variables[1].name 不合法',
        'start.variables[2].name 不允许使用 __proto__',
        'start 变量名重复: dup',
        'start.variables[5].type 不合法: date',
        '变量 str_default 的 default 必须是字符串',
        '变量 num_default 的 default 必须是数字',
        '变量 bool_default 的 default 必须是布尔值',
      ]),
    )
  })

  it('变量超过上限时给出实际数量，但仍逐个检查', () => {
    const variables = Array.from({ length: MAX_START_VARIABLES + 1 }, (_, index) => ({
      name: `v_${index}`,
      type: 'string',
    }))
    const errors = validateDocument(startDoc(variables))
    expect(errors).toContain(`start 变量不得超过 ${MAX_START_VARIABLES} 项（实际 ${MAX_START_VARIABLES + 1}）`)
  })
})
