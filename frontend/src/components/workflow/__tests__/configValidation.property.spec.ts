import fc from 'fast-check'
import { describe, expect, it } from 'vitest'

import { nodeConfigIssues, referenceIssues, variableReferences } from '@/components/workflow/configValidation'
import { createNodeDef } from '@/components/workflow/registry'
import { upstreamVariableGroups } from '@/composables/workflow/useUpstreamVariables'
import {
  NODE_SPECS,
  NODE_TYPES,
  validateGraph,
  type EdgeDef,
  type JsonValue,
  type NodeDef,
  type NodeType,
  type WorkflowDef,
} from '@/utils/graph'
import { isSafeVariableKey, variableTransformer } from '@/utils/graph/variable'

/**
 * 批次 D4 的性质测试：D2/D3 那批判据的**恒等式**，不是逐例断言。
 *
 * 三件事各钉一条，都是"举例子举不全"的那类：
 *  1. 面板判据与整图校验（`validateGraph`）不许互相打脸：同一份配置，一处说合法、另一处说非法，
 *     症状就是"红字消了但保存还是失败"或反过来；
 *  2. 变量选择器永远只给真祖先、键都是安全键、祖先之间的先后与图的可达一致——
 *     这条一旦破，值班员点出来的引用就发不出去；
 *  3. `{{#a.b#}}` 与 `['a','b']` 互转对任意安全段都成立（端口对拍的恒等式，不只对 fixture 那一支）。
 */

const leaf: fc.Arbitrary<JsonValue> = fc.oneof(
  fc.constant(null),
  fc.boolean(),
  fc.integer({ min: -50, max: 50 }),
  fc.string({ maxLength: 8 }),
  fc.constant('__proto__'),
)
const jsonValue: fc.Arbitrary<JsonValue> = fc.oneof(
  leaf,
  fc.array(leaf, { maxLength: 3 }),
  fc.dictionary(fc.string({ maxLength: 6 }), leaf, { maxKeys: 3 }),
)

/** 从给定字符表拼字符串：fast-check v4 没有 `stringOf`，用 array+join 自己造。 */
function stringFrom(chars: readonly string[], min = 1, max = 6): fc.Arbitrary<string> {
  return fc.array(fc.constantFrom(...chars), { minLength: min, maxLength: max }).map((parts) => parts.join(''))
}

/** 任意 config：键取自该类型的白名单，也允许掺白名单外的键（存量定义里就有这种）。 */
function configArbitrary(type: NodeType): fc.Arbitrary<Record<string, JsonValue>> {
  const allowed = [...NODE_SPECS[type].required_config, ...NODE_SPECS[type].optional_config]
  const stray = stringFrom(['a', 'b', 'x', '_'])
  const keys = allowed.length > 0 ? fc.oneof(fc.constantFrom(...allowed), stray) : stray
  return fc.dictionary(keys, jsonValue, { maxKeys: allowed.length + 2 })
}

function nodeOf(type: NodeType, config: Record<string, JsonValue>): NodeDef {
  return { ...createNodeDef(type, 'n_1'), config }
}

function definitionOf(node: NodeDef): WorkflowDef {
  return {
    workflow_id: 'wf_0123456789ab',
    name: '性质用例',
    description: '',
    version: 1,
    nodes: [node],
    edges: [],
    created_by: 'test',
    status: 'active',
  }
}

/** `checkConfig` 那三类错误的原文形状（必填/未知参数/下限），都带节点类型名。 */
function configErrorsOf(graphErrors: readonly string[], type: NodeType): string[] {
  return graphErrors.filter(
    (message) =>
      message.includes(`节点 ${type} 缺少必填参数`) ||
      message.includes(`节点 ${type} 含未知参数`) ||
      message.includes(`节点 ${type} 参数`),
  )
}

describe('面板判据与整图校验同源（性质）', () => {
  it('任意 19 类 × 任意 config：体检不抛异常，每条待修都有话可说且不重复', () => {
    for (const type of NODE_TYPES) {
      fc.assert(
        fc.property(
          configArbitrary(type),
          fc.array(fc.string({ maxLength: 8 }), { maxLength: 3 }),
          (config, inbound) => {
            const issues = nodeConfigIssues(nodeOf(type, config), { inbound })
            for (const issue of issues) {
              expect(issue.message, `${type} 冒出一条没有话的待修`).not.toBe('')
            }
            const fingerprints = issues.map((issue) => `${issue.key}|${issue.message}`)
            expect(fingerprints.length, `${type} 同一句话摆了两遍`).toBe(new Set(fingerprints).size)
          },
        ),
        { numRuns: 120 },
      )
    }
  })

  /**
   * 反方向**故意不成立**，而且是量出来的：面板还判值形态
   * （`{"method":{}}` 这种"填了但不是文字"），`validateGraph` 与后端 `validate_config`
   * 只判必填、白名单与下限。写成 ⇔ 会在这一支上假红，所以把它单独钉成一条已知差异——
   * 哪天后端开始判类型形态，这条用例就会红，提醒回来把两个口径合上。
   */
  it('面板比整图校验多判"值形态"：这是已知差异，不是漏判', () => {
    const node = nodeOf('api_call', { method: {}, url: [] })
    expect(configErrorsOf(validateGraph(definitionOf(node)), 'api_call')).toEqual([])
    expect(nodeConfigIssues(node, {}).map((issue) => issue.message)).toEqual([
      '「请求方法」要是文字',
      '「接口地址」要是文字',
    ])
  })

  it('整图校验报的每一条配置错，面板都必须先说过（面板是超集）', () => {
    for (const type of NODE_TYPES) {
      fc.assert(
        fc.property(configArbitrary(type), (config) => {
          const node = nodeOf(type, config)
          // 不给 inbound：`upstream` 是否入边这一类判据 validateGraph 不管，判"同源"时单独排除。
          const issues = nodeConfigIssues(node, {})
          const graphErrors = configErrorsOf(validateGraph(definitionOf(node)), type)
          expect(
            issues.length > 0 || graphErrors.length === 0,
            `整图报了「${graphErrors.join('；')}」而面板一声不吭`,
          ).toBe(true)
        }),
        { numRuns: 120 },
      )
    }
  })

  it('引用体检不抛异常，且每条引用都真落在 config 的某一格里', () => {
    for (const type of NODE_TYPES) {
      fc.assert(
        fc.property(
          configArbitrary(type),
          fc.array(fc.string({ maxLength: 6 }), { maxLength: 3 }),
          (config, ancestors) => {
            const node = nodeOf(type, config)
            for (const issue of referenceIssues(node, { ancestors })) {
              expect(issue.key, '引用问题挂不到任何一格上').not.toBe('')
              expect(String(config[issue.key])).toContain('{{#')
            }
            const found = variableReferences(config)
            expect(found).toEqual([...new Set(found)])
          },
        ),
        { numRuns: 80 },
      )
    }
  })
})

describe('上游变量合成的恒等式（性质）', () => {
  const TYPES: NodeType[] = ['start', 'data_fetch', 'threshold', 'notify', 'risk_assess', 'branch', 'delay']

  interface Graph {
    nodes: { node_id: string; type: NodeType }[]
    edges: EdgeDef[]
    target: string
  }

  /** 边只允许从下标小的指向大的：按构造无环，拓扑分层不会走到兜底那一支。 */
  const graphArbitrary: fc.Arbitrary<Graph> = fc
    .integer({ min: 2, max: 7 })
    .chain((size) => {
      const ids = Array.from({ length: size }, (_, index) => `n${index}`)
      const possible: [string, string][] = []
      for (let i = 0; i < size; i += 1) {
        for (let j = i + 1; j < size; j += 1) possible.push([ids[i] as string, ids[j] as string])
      }
      return fc.tuple(
        fc.subarray(possible),
        fc.array(fc.constantFrom(...TYPES), { minLength: size, maxLength: size }),
        fc.constantFrom(...ids),
      )
    })
    .map(([pairs, types, target]) => ({
      nodes: types.map((type, index) => ({ node_id: `n${String(index)}`, type })),
      edges: pairs.map(([source, dst]) => ({ source, target: dst, condition: '' })),
      target,
    }))

  it('组内节点永远是目标的真祖先、不重复、键全是安全键', () => {
    fc.assert(
      fc.property(graphArbitrary, ({ nodes, edges, target }) => {
        const groups = upstreamVariableGroups({
          nodes,
          edges,
          targetNodeId: target,
          outputsByType: Object.fromEntries(NODE_TYPES.map((type) => [type, NODE_SPECS[type].outputs])),
        })
        const ancestors = trueAncestors(target, edges)
        const seen = new Set<string>()
        for (const group of groups) {
          expect(group.nodeId, '把自己列成了自己的上游').not.toBe(target)
          expect(ancestors.has(group.nodeId), `${group.nodeId} 不是 ${target} 的祖先`).toBe(true)
          expect(seen.has(group.nodeId), '同一个祖先被分成两组').toBe(false)
          seen.add(group.nodeId)
          expect(group.keys.length, '空组不该出现在面板里').toBeGreaterThan(0)
          for (const key of group.keys) expect(isSafeVariableKey(key), `键 ${key} 不该进面板`).toBe(true)
        }
      }),
      { numRuns: 300 },
    )
  })

  it('祖先之间的先后与图的可达一致：X 能通向 Y 时，Y 那一组一定排在 X 之后', () => {
    fc.assert(
      fc.property(graphArbitrary, ({ nodes, edges, target }) => {
        const groups = upstreamVariableGroups({
          nodes,
          edges,
          targetNodeId: target,
          outputsByType: Object.fromEntries(NODE_TYPES.map((type) => [type, NODE_SPECS[type].outputs])),
        })
        const order = new Map(groups.map((group, index) => [group.nodeId, index]))
        for (const left of order.keys()) {
          for (const right of order.keys()) {
            if (left === right) continue
            if (!reachable(left, right, edges)) continue
            expect(
              (order.get(left) ?? 0) < (order.get(right) ?? 0),
              `${left} 通向 ${right}，面板却把 ${right} 排在前面`,
            ).toBe(true)
          }
        }
      }),
      { numRuns: 300 },
    )
  })

  it('面板里点出来的每只引用，反向解析回选择器都是原样', () => {
    const segment = stringFrom(['a', 'b', 'c', 'd', 'e', '_', '1', '2', '3'], 1, 6)
    fc.assert(
      fc.property(fc.array(segment, { minLength: 1, maxLength: 4 }), (segments) => {
        fc.pre(segments.every(isSafeVariableKey))
        const reference = variableTransformer(segments)
        expect(typeof reference).toBe('string')
        expect(variableTransformer(reference)).toEqual(segments)
      }),
      { numRuns: 300 },
    )
  })
})

/** 反向可达（不含自身）与被测实现独立写一遍，才能当判据用。 */
function trueAncestors(target: string, edges: readonly EdgeDef[]): Set<string> {
  const parents = new Map<string, string[]>()
  for (const edge of edges) {
    const list = parents.get(edge.target) ?? []
    list.push(edge.source)
    parents.set(edge.target, list)
  }
  const seen = new Set<string>()
  const queue = [...(parents.get(target) ?? [])]
  while (queue.length > 0) {
    const id = queue.shift() as string
    if (id === target || seen.has(id)) continue
    seen.add(id)
    queue.push(...(parents.get(id) ?? []))
  }
  return seen
}

/** 正向可达：`from` 沿出边能不能走到 `to`。 */
function reachable(from: string, to: string, edges: readonly EdgeDef[]): boolean {
  const children = new Map<string, string[]>()
  for (const edge of edges) {
    const list = children.get(edge.source) ?? []
    list.push(edge.target)
    children.set(edge.source, list)
  }
  const seen = new Set<string>([from])
  const queue = [from]
  while (queue.length > 0) {
    const id = queue.shift() as string
    if (id === to) return true
    for (const child of children.get(id) ?? []) {
      if (seen.has(child)) continue
      seen.add(child)
      queue.push(child)
    }
  }
  return false
}
