import { describe, expect, it } from 'vitest'

import type { EdgeDef, JsonValue, NodeDef, NodeType, Position } from '../../graph'
import clipboardFixture from '../__fixtures__/dify-1.17.1/clipboard.json'
import {
  ClipboardError,
  clipboardErrorMessage,
  clipboardOffsetTo,
  copyClipboardPayload,
  extractWorkflowClipboardSubgraph,
  isClipboardEdgeStructurallyValid,
  isClipboardNodeStructurallyValid,
  isClipboardPayloadEmpty,
  isClipboardValueCompatibleWithDefault,
  MAX_CLIPBOARD_BYTES,
  mergeClipboardNodeDef,
  parseWorkflowClipboardText,
  remapWorkflowClipboardSubgraph,
  sanitizeClipboardJson,
  sanitizeClipboardValueByDefault,
  stringifyWorkflowClipboardData,
  WORKFLOW_CLIPBOARD_KIND,
  WORKFLOW_CLIPBOARD_VERSION,
  type ClipboardNode,
  type ClipboardPayload,
} from '../clipboard'
import { emptyLayout, type WorkflowDocument } from '../document'

interface SanitizeCase {
  name: string
  def?: unknown
  incoming: unknown
  actual: unknown
  compatible: boolean
}

interface StructuralCase {
  name: string
  value: unknown
  fn: 'node' | 'edge'
  actual: boolean
}

interface ParseCase {
  name: string
  text: string
  current: string
}

interface SecurityCase {
  name: string
  def: unknown
  incoming: Record<string, unknown>
  upstreamActual: { ownKeys: string[]; prototypePolluted: boolean }
  aegisExpectation: { ownKeys: string[]; prototypePolluted: boolean; droppedKeys: string[] }
}

const sanitizeCases = clipboardFixture.sanitizeCases as unknown as SanitizeCase[]
const structuralCases = clipboardFixture.structuralCases as unknown as StructuralCase[]
const parseCases = clipboardFixture.parseCases as unknown as ParseCase[]
const securityCases = clipboardFixture.securityCases as unknown as SecurityCase[]

/**
 * fixture 记录的是上游的 Node/Edge 形状（position 内嵌在节点上、字段叫 id/data）；
 * AEGIS 的粘贴单位是 `{ node: NodeDef, position }`。对拍前在这一处换形状，
 * 语义（哪些字段缺失/类型错）保持不变。
 */
function toAegisNode(value: unknown): unknown {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return value
  const record = value as Record<string, unknown>
  return {
    node: { node_id: record.id, type: record.type, config: record.data },
    position: record.position,
  }
}

/** 把上游 payload 的形状换到 AEGIS，但保留 version/edges 的原始形态（用来触发目标错误）。 */
function reshapeUpstreamText(raw: string, kind = WORKFLOW_CLIPBOARD_KIND): string {
  const payload = JSON.parse(raw) as Record<string, unknown>
  const nodes = Array.isArray(payload.nodes) ? payload.nodes.map(toAegisNode) : payload.nodes
  return JSON.stringify({ kind, version: payload.version, nodes, edges: payload.edges })
}

function errorCodeOf(text: string, version: string): string | null {
  try {
    parseWorkflowClipboardText(text, version)
    return null
  } catch (error) {
    return error instanceof ClipboardError ? error.code : 'unexpected'
  }
}

function thrownCode(action: () => unknown): string | null {
  try {
    action()
    return null
  } catch (error) {
    return error instanceof ClipboardError ? error.code : 'unexpected'
  }
}

describe('骨架函数自身可判别（守卫的守卫）', () => {
  it('节点校验认 AEGIS 形状，不认上游形状', () => {
    expect(
      isClipboardNodeStructurallyValid({
        node: { node_id: 'a', type: 'start', config: {} },
        position: { x: 0, y: 0 },
      }),
    ).toBe(true)
    expect(isClipboardNodeStructurallyValid({ id: 'a', type: 'start', data: {}, position: { x: 0, y: 0 } })).toBe(false)
  })
})

describe('sanitizeClipboardValueByDefault（Dify T1 端口）', () => {
  it('fixture 每个用例的合并结果与兼容性都一致', () => {
    for (const row of sanitizeCases) {
      expect(sanitizeClipboardValueByDefault(row.def, row.incoming), row.name).toEqual(row.actual)
      expect(isClipboardValueCompatibleWithDefault(row.def, row.incoming), row.name).toBe(row.compatible)
    }
  })

  it('默认值对象被深拷贝：改动合并结果不能反写进默认配置', () => {
    const def = { nested: { c: 2 } }
    const merged = sanitizeClipboardValueByDefault(def, {}) as Record<string, unknown>
    const nested = merged.nested as Record<string, unknown>
    nested.c = 9
    expect(def.nested.c).toBe(2)
  })

  it('原型污染：__proto__ 作为普通键被丢弃，全局原型不受影响', () => {
    const row = securityCases[0]
    if (row === undefined) throw new Error('fixture 缺安全用例')
    /* fixture 里的 `"__proto__": {...}` 经 JS 模块求值会变成原型而非自有键，
       所以这里用 JSON.parse 还原“外部剪贴板文本”的真实形态。 */
    const incoming = JSON.parse('{"__proto__":{"polluted":true},"a":1}') as Record<string, unknown>
    expect(Object.keys(incoming)).toEqual(['__proto__', 'a'])

    const merged = sanitizeClipboardValueByDefault(row.def, incoming) as Record<string, unknown>

    expect(Object.keys(merged)).toEqual(row.aegisExpectation.ownKeys)
    expect(({} as Record<string, unknown>).polluted).toBeUndefined()
    expect(Object.getPrototypeOf(merged)).toBe(Object.prototype)
    expect(row.aegisExpectation.droppedKeys).toEqual(['__proto__'])
  })

  it('嵌套的原型段也过滤，而非只挡顶层', () => {
    const incoming = JSON.parse('{"nested":{"__proto__":{"polluted":true},"ok":1}}') as Record<string, unknown>
    const merged = sanitizeClipboardValueByDefault({ nested: {} }, incoming) as {
      nested: Record<string, unknown>
    }
    expect(Object.keys(merged.nested)).toEqual(['ok'])
    expect(({} as Record<string, unknown>).polluted).toBeUndefined()
  })
})

describe('结构校验（Dify T1 端口）', () => {
  it('fixture 的 node/edge 用例逐条一致', () => {
    for (const row of structuralCases) {
      const value = row.fn === 'node' ? toAegisNode(row.value) : row.value
      const actual =
        row.fn === 'node' ? isClipboardNodeStructurallyValid(value) : isClipboardEdgeStructurallyValid(value)
      expect(actual, row.name).toBe(row.actual)
    }
  })
})

describe('parseWorkflowClipboardText（Dify T1 端口 + AEGIS 显式错误）', () => {
  it('上游的混合有效性用例：非法单项被过滤，合法项保留', () => {
    for (const [name, current, expectedMismatch] of [
      ['mixed-validity-version-mismatch', '1', true],
      ['same-version', '2', false],
    ] as const) {
      const row = parseCases.find((item) => item.name === name)
      if (row === undefined) throw new Error(`fixture 缺 ${name}`)
      const parsed = parseWorkflowClipboardText(reshapeUpstreamText(row.text), current)

      expect(parsed.nodes, name).toHaveLength(1)
      expect(parsed.nodes[0]?.node.node_id, name).toBe('n1')
      // 上游保留 targetHandle；AEGIS 的 EdgeDef 只有 source/target/condition。
      expect(parsed.edges, name).toEqual([
        { source: 'n1', target: 'n2', condition: '' },
        { source: 'n1', target: 'n2', condition: '' },
      ])
      expect(parsed.sourceVersion, name).toBe('2')
      expect(parsed.isVersionMismatch, name).toBe(expectedMismatch)
    }
  })

  it('上游的静默空结果在 AEGIS 都有明确 code', () => {
    const expectations: Record<string, string> = {
      'empty-text': 'empty',
      'invalid-json': 'invalid-json',
      'wrong-kind': 'wrong-kind',
      'missing-version': 'bad-payload',
      'nodes-not-array': 'bad-payload',
    }

    for (const row of parseCases) {
      const expected = expectations[row.name]
      if (expected === undefined) continue

      let text = row.text
      if (row.name === 'wrong-kind') text = reshapeUpstreamText(row.text, 'other')
      else if (row.name === 'missing-version' || row.name === 'nodes-not-array') text = reshapeUpstreamText(row.text)
      else if (row.name !== 'empty-text' && row.name !== 'invalid-json') text = reshapeUpstreamText(row.text)

      expect(errorCodeOf(text, row.current), row.name).toBe(expected)
    }
  })

  it('kind 对但缺版本 / nodes 不是数组，各自给出 bad-payload', () => {
    expect(errorCodeOf(JSON.stringify({ kind: WORKFLOW_CLIPBOARD_KIND, nodes: [], edges: [] }), '1')).toBe(
      'bad-payload',
    )
    expect(
      errorCodeOf(JSON.stringify({ kind: WORKFLOW_CLIPBOARD_KIND, version: '1', nodes: {}, edges: [] }), '1'),
    ).toBe('bad-payload')
  })

  it('空文本与全是废节点的载荷都不算“成功粘贴”', () => {
    expect(errorCodeOf('', '1')).toBe('empty')
    expect(
      errorCodeOf(
        JSON.stringify({
          kind: WORKFLOW_CLIPBOARD_KIND,
          version: '1',
          nodes: [{ node: { node_id: 7 }, position: { x: 0, y: 0 } }],
          edges: [],
        }),
        '1',
      ),
    ).toBe('no-valid-nodes')
  })

  it('超过 2MB 的文本在 JSON.parse 之前就被拒绝', () => {
    const oversized = `{"kind":"${WORKFLOW_CLIPBOARD_KIND}","version":"1","nodes":[],"edges":[],"pad":"${'x'.repeat(
      MAX_CLIPBOARD_BYTES,
    )}"}`
    expect(errorCodeOf(oversized, '1')).toBe('too-large')
  })

  it('跨版本仍可粘贴，但标记 isVersionMismatch', () => {
    const text = JSON.stringify({
      kind: WORKFLOW_CLIPBOARD_KIND,
      version: '0',
      nodes: [{ node: { node_id: 'a', type: 'start', config: {} }, position: { x: 1, y: 2 } }],
      edges: [],
    })
    const parsed = parseWorkflowClipboardText(text)
    expect(parsed.isVersionMismatch).toBe(true)
    expect(parsed.sourceVersion).toBe('0')
  })

  it('字符串化 → 解析往返一致，且 kind 是 AEGIS 自己的', () => {
    const payload = {
      nodes: [{ node: { node_id: 'a', type: 'start', config: { key: 'v' } }, position: { x: 1, y: 2 } }],
      edges: [{ source: 'a', target: 'b', condition: 'high' }],
    }
    const text = stringifyWorkflowClipboardData(payload)
    expect(text).toContain(WORKFLOW_CLIPBOARD_KIND)
    expect(text).not.toContain(['dify', 'workflow', 'clipboard'].join('-'))
    expect(parseWorkflowClipboardText(text)).toMatchObject({
      nodes: payload.nodes,
      edges: payload.edges,
      sourceVersion: WORKFLOW_CLIPBOARD_VERSION,
      isVersionMismatch: false,
    })
  })

  it('写出超限时抛 too-large，而不是交给浏览器失败', () => {
    const node: ClipboardNode = {
      node: { node_id: 'a', type: 'start', config: { blob: 'x'.repeat(MAX_CLIPBOARD_BYTES) } },
      position: { x: 0, y: 0 },
    }
    expect(thrownCode(() => stringifyWorkflowClipboardData({ nodes: [node], edges: [] }))).toBe('too-large')
  })
})

describe('载荷辅助', () => {
  it('拷贝是深拷贝：改副本不影响原载荷', () => {
    const payload = {
      nodes: [{ node: { node_id: 'a', type: 'start', config: { nested: { deep: 1 } } }, position: { x: 0, y: 0 } }],
      edges: [{ source: 'a', target: 'b', condition: '' }],
    }
    const copy = copyClipboardPayload(payload)
    const copyNested = (copy.nodes[0]?.node.config as Record<string, unknown>).nested as Record<string, unknown>
    const copyEdge = copy.edges[0]
    if (copyEdge === undefined) throw new Error('拷贝丢了边')
    copyNested.deep = 2
    copyEdge.condition = 'changed'
    expect((payload.nodes[0]?.node.config.nested as Record<string, unknown>).deep).toBe(1)
    expect(payload.edges[0]?.condition).toBe('')
  })

  it('空载荷判空与错误文案不暴露内部对象', () => {
    expect(isClipboardPayloadEmpty({ nodes: [], edges: [] })).toBe(true)
    expect(clipboardErrorMessage(new ClipboardError('empty', '剪贴板里没有可粘贴的内容'))).toBe(
      '剪贴板里没有可粘贴的内容',
    )
    expect(clipboardErrorMessage(new Error('NotAllowedError'))).toBe('NotAllowedError')
    expect(clipboardErrorMessage({ message: 'denied' })).toBe('denied')
    expect(clipboardErrorMessage({ message: 503 })).toBe('剪贴板不可用')
    expect(clipboardErrorMessage(null)).toBe('剪贴板不可用')
  })

  it('JSON 净化把非有限数折成 null，并丢原型段', () => {
    expect(sanitizeClipboardJson(Number.NaN)).toBeNull()
    expect(sanitizeClipboardJson({ a: Number.POSITIVE_INFINITY, __proto__: { polluted: true } })).toEqual({ a: null })
    expect(sanitizeClipboardJson(undefined)).toBeNull()
    expect(({} as Record<string, unknown>).polluted).toBeUndefined()
  })
})

function nodeDef(nodeId: string, type: NodeType = 'data_fetch', config: Record<string, JsonValue> = {}): NodeDef {
  return {
    node_id: nodeId,
    type,
    name: nodeId,
    config,
    sla_ms: 5_000,
    timeout_ms: 5_000,
    on_failure: 'retry',
    retry: { max_attempts: 1, backoff_ms: 200 },
  }
}

function documentOf(nodes: NodeDef[], edges: EdgeDef[], positions: Record<string, Position>): WorkflowDocument {
  return {
    schema_version: 1,
    lineage_id: 'lin_abcdef',
    name: '测试图',
    description: '',
    nodes,
    edges,
    layout: { ...emptyLayout(), positions },
  }
}

function sequentialIds(): (type: NodeType) => string {
  let index = 0
  return (type) => {
    index += 1
    return `${type}_${index}`
  }
}

const defaults = (type: NodeType, nodeId: string): NodeDef => nodeDef(nodeId, type)

describe('extractWorkflowClipboardSubgraph（复制：子图 + 内部边 + 屏上坐标）', () => {
  const document = documentOf(
    [nodeDef('a'), nodeDef('b'), nodeDef('c')],
    [
      { source: 'a', target: 'b', condition: '' },
      { source: 'b', target: 'c', condition: 'high' },
    ],
    { a: { x: 40, y: 60 }, b: { x: 280, y: 60 } },
  )

  it('只收被选中节点与两端都在选区内的边', () => {
    const payload = extractWorkflowClipboardSubgraph(document, ['a', 'b'])
    expect(payload.nodes.map((item) => item.node.node_id)).toEqual(['a', 'b'])
    expect(payload.edges).toEqual([{ source: 'a', target: 'b', condition: '' }])
  })

  it('位置取 layout；layout 缺项时退回分层坐标而不是 (0,0)', () => {
    const payload = extractWorkflowClipboardSubgraph(document, ['a', 'b', 'c'])
    expect(payload.nodes.map((item) => item.position)).toEqual([
      { x: 40, y: 60 },
      { x: 280, y: 60 },
      { x: 560, y: 0 },
    ])
  })

  it('片段与画布对象断开：改片段不反写文档，且顺序跟文档一致', () => {
    const payload = extractWorkflowClipboardSubgraph(document, ['b', 'a'])
    expect(payload.nodes.map((item) => item.node.node_id)).toEqual(['a', 'b'])
    const first = payload.nodes[0]
    if (first === undefined) throw new Error('缺节点')
    first.node.name = '被改了'
    first.position.x = 999
    expect(document.nodes[0]?.name).toBe('a')
    expect(document.layout.positions.a).toEqual({ x: 40, y: 60 })
  })

  it('选空集合得到空载荷', () => {
    expect(isClipboardPayloadEmpty(extractWorkflowClipboardSubgraph(document, []))).toBe(true)
  })
})

describe('clipboardOffsetTo', () => {
  it('偏移把片段左上角对齐到落点，负坐标也按最小值算', () => {
    const nodes: ClipboardNode[] = [
      { node: {}, position: { x: 10, y: -30 } },
      { node: {}, position: { x: -20, y: 70 } },
    ]
    expect(clipboardOffsetTo(nodes, { x: 100, y: 200 })).toEqual({ x: 120, y: 230 })
  })

  it('空片段退化成原样落点', () => {
    expect(clipboardOffsetTo([], { x: 7, y: 8 })).toEqual({ x: 7, y: 8 })
  })
})

describe('mergeClipboardNodeDef（净化 + 默认值并集）', () => {
  const base = nodeDef('n1', 'data_fetch', { url: 'https://example.test', method: 'GET' })

  it('缺字段由默认值补齐，多出来的配置键保留', () => {
    const merged = mergeClipboardNodeDef({ type: 'data_fetch', config: { extra: 1 } }, base)
    expect(merged?.config).toEqual({ url: 'https://example.test', method: 'GET', extra: 1 })
    expect(merged?.name).toBe('n1')
  })

  it('类型不认识时整条丢弃', () => {
    expect(mergeClipboardNodeDef({ type: 'not_a_node' }, base)).toBeNull()
    expect(mergeClipboardNodeDef({ type: 42 }, base)).toBeNull()
  })

  it('数值落进合法区间：sla 夹紧、timeout 不超过 sla、retry 有上下界', () => {
    const merged = mergeClipboardNodeDef(
      {
        type: 'data_fetch',
        sla_ms: Number.MAX_SAFE_INTEGER,
        timeout_ms: 1,
        retry: { max_attempts: 99, backoff_ms: -5 },
        on_failure: 'explode',
      },
      base,
    )
    expect(merged?.sla_ms).toBe(3_600_000)
    expect(merged?.timeout_ms).toBe(100)
    expect(merged?.retry).toEqual({ max_attempts: 10, backoff_ms: 0 })
    expect(merged?.on_failure).toBe('retry')
  })

  it('非有限数值退回默认；空白名称不覆盖默认名', () => {
    const merged = mergeClipboardNodeDef(
      { type: 'data_fetch', sla_ms: Number.NaN, timeout_ms: Number.POSITIVE_INFINITY, name: '   ' },
      base,
    )
    expect(merged?.sla_ms).toBe(5_000)
    expect(merged?.timeout_ms).toBe(5_000)
    expect(merged?.name).toBe('n1')
  })

  it('原型段在配置里同样被丢', () => {
    const record = JSON.parse('{"type":"data_fetch","config":{"__proto__":{"polluted":true},"ok":"v"}}') as Record<
      string,
      unknown
    >
    const merged = mergeClipboardNodeDef(record, base)
    expect(merged?.config).toEqual({ url: 'https://example.test', method: 'GET', ok: 'v' })
    expect(({} as Record<string, unknown>).polluted).toBeUndefined()
  })
})

describe('remapWorkflowClipboardSubgraph（换号 + 偏移 + 引用重指）', () => {
  function payloadOf(): ClipboardPayload {
    return {
      nodes: [
        { node: { ...nodeDef('a', 'data_fetch'), config: { url: 'u' } }, position: { x: 100, y: 100 } },
        {
          node: { ...nodeDef('b', 'join', { upstream: ['a', 'outside'] }) },
          position: { x: 380, y: 100 },
        },
        { node: { type: 'not_a_node' }, position: { x: 0, y: 0 } },
        { node: { node_id: '', type: 'data_fetch' }, position: { x: 0, y: 0 } },
      ],
      edges: [
        { source: 'a', target: 'b', condition: '' },
        { source: 'b', target: 'a', condition: 'high' },
        { source: 'a', target: 'b', condition: '' },
        { source: 'a', target: 'outside', condition: '' },
        { source: 'b', target: 'b', condition: '' },
      ],
    }
  }

  const options = (): {
    allocateId: (type: NodeType) => string
    createDefault: typeof defaults
    offset: Position
  } => ({ allocateId: sequentialIds(), createDefault: defaults, offset: { x: 20, y: 30 } })

  it('每个合法节点拿新号，位置整体偏移', () => {
    const result = remapWorkflowClipboardSubgraph(payloadOf(), options())
    expect(result.nodes.map((node) => node.node_id)).toEqual(['data_fetch_1', 'join_2'])
    expect(result.idMapping).toEqual({ a: 'data_fetch_1', b: 'join_2' })
    expect(result.positions).toEqual({
      data_fetch_1: { x: 120, y: 130 },
      join_2: { x: 400, y: 130 },
    })
  })

  it('只留两端都在片段内的边，去自环去重，条件保留', () => {
    const result = remapWorkflowClipboardSubgraph(payloadOf(), options())
    expect(result.edges).toEqual([
      { source: 'data_fetch_1', target: 'join_2', condition: '' },
      { source: 'join_2', target: 'data_fetch_1', condition: 'high' },
    ])
  })

  it('upstream 指向片段内的改新号，指向片段外的清掉', () => {
    const result = remapWorkflowClipboardSubgraph(payloadOf(), options())
    expect(result.nodes[1]?.config.upstream).toEqual(['data_fetch_1'])
  })

  it('字符串形式的 upstream 同样重指；外部引用清成空串', () => {
    const payload: ClipboardPayload = {
      nodes: [
        { node: { ...nodeDef('a', 'data_fetch') }, position: { x: 0, y: 0 } },
        { node: { ...nodeDef('b', 'join', { upstream: 'a' }) }, position: { x: 1, y: 1 } },
        { node: { ...nodeDef('c', 'join', { upstream: 'outside' }) }, position: { x: 2, y: 2 } },
      ],
      edges: [],
    }
    const result = remapWorkflowClipboardSubgraph(payload, options())
    expect(result.nodes[1]?.config.upstream).toBe('data_fetch_1')
    expect(result.nodes[2]?.config.upstream).toBe('')
  })

  it('整段无效载荷得到空结果而不是抛错', () => {
    const result = remapWorkflowClipboardSubgraph(
      { nodes: [{ node: { type: 'nope' }, position: { x: 0, y: 0 } }], edges: [] },
      options(),
    )
    expect(result).toEqual({ nodes: [], edges: [], positions: {}, idMapping: {} })
  })
})
