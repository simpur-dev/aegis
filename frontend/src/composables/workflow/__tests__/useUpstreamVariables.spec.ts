import { describe, expect, it } from 'vitest'
import { ref } from 'vue'

import type { NodeTypeEntry } from '@/api/workflow/contract'
import { useUpstreamVariables, upstreamVariableGroups } from '@/composables/workflow/useUpstreamVariables'
import upstreamFixture from '@/utils/graph/__fixtures__/dify-1.17.1/upstream-variables.contract.json'
import type { EdgeDef, NodeType } from '@/utils/graph'

type Case = (typeof upstreamFixture.cases)[number]

/** fixture 的节点只给 ID：这里把 ID 同时当类型用，让 catalog 输出表按类型取值。 */
const nodes = upstreamFixture.nodes.map((id) => ({ node_id: id, type: id as NodeType }))
const edges = upstreamFixture.edges as EdgeDef[]

function groupsFor(targetNodeId: string, override: Partial<Case> = {}) {
  const outputsByType = {
    ...upstreamFixture.catalogOutputs,
    ...(override.catalogOutputsOverride ?? {}),
  } as Record<string, readonly string[]>
  const lastRunOutputsByNode = {
    ...upstreamFixture.lastRunOutputs,
    ...(override.lastRunOutputsOverride ?? {}),
  } as Record<string, Record<string, unknown>>
  return upstreamVariableGroups({
    nodes,
    edges,
    targetNodeId,
    outputsByType,
    lastRunOutputsByNode,
  })
}

describe('useUpstreamVariables 契约（Dify T2，AEGIS 收窄口径）', () => {
  for (const row of upstreamFixture.cases) {
    it(`${row.name}：祖先、键序与空组省略都与 fixture 一致`, () => {
      const groups = groupsFor(row.target, row)
      expect(groups.map((group) => ({ nodeId: group.nodeId, keys: group.keys }))).toEqual(row.expected)
      for (const group of groups) expect(group.nodeType, '合成节点的类型就是 ID').toBe(group.nodeId)
      for (const excluded of row.excluded ?? []) {
        expect(
          groups.map((group) => group.nodeId),
          `${excluded} 不是 ${row.target} 的祖先`,
        ).not.toContain(excluded)
      }
    })
  }

  it('未知目标节点返回空数组，不把整张图当上游', () => {
    expect(groupsFor('missing')).toEqual([])
  })

  it('环上的图不退化成整页报错：祖先仍按 ID 顺序给出', () => {
    const cyclic: EdgeDef[] = [
      { source: 'start', target: 'a', condition: '' },
      { source: 'a', target: 'start', condition: '' },
    ]
    const groups = upstreamVariableGroups({
      nodes: [
        { node_id: 'start', type: 'start' },
        { node_id: 'a', type: 'a' as NodeType },
      ],
      edges: cyclic,
      targetNodeId: 'a',
      outputsByType: { start: ['started'] },
    })
    expect(groups).toEqual([{ nodeId: 'start', nodeType: 'start', keys: ['started'] }])
  })

  it('上次运行输出里的原型链名字不进入选择器', () => {
    const groups = upstreamVariableGroups({
      nodes,
      edges,
      targetNodeId: 'c',
      outputsByType: { ...upstreamFixture.catalogOutputs, a: ['rain_10min', 'constructor'] },
      lastRunOutputsByNode: JSON.parse(
        '{"a":{"__proto__":{"polluted":true},"prototype":1,"constructor":2,"observed_at":"2026-10-22T09:00:00Z"}}',
      ) as Record<string, Record<string, unknown>>,
    })
    const a = groups.find((group) => group.nodeId === 'a')
    expect(a?.keys).toEqual(['rain_10min', 'observed_at'])
    expect(({} as Record<string, unknown>).polluted).toBeUndefined()
  })

  it('组合函数随目标、目录与实例输出重算', () => {
    const definition = ref({ nodes, edges })
    const targetNodeId = ref<string | null>('c')
    const catalog = ref<NodeTypeEntry[]>([])
    const lastRunOutputs = ref<Record<string, Record<string, string | number | boolean | null>> | null>(null)
    const groups = useUpstreamVariables({
      definition: () => definition.value,
      targetNodeId: () => targetNodeId.value,
      catalog: () => catalog.value,
      lastRunOutputs: () => lastRunOutputs.value,
    })

    // 目录为空时走本地镜像：合成类型 a/x 不在镜像里，只剩声明过输出键的 start。
    expect(groups.value.map((group) => group.nodeId)).toEqual(['start'])

    targetNodeId.value = 'a'
    expect(groups.value).toEqual([{ nodeId: 'start', nodeType: 'start', keys: ['started', 'variables'] }])

    // 目标清空（面板关闭）时不再给组，避免残留上一颗节点的变量。
    targetNodeId.value = null
    expect(groups.value).toEqual([])
    targetNodeId.value = 'c'

    catalog.value = [
      {
        type: 'a' as NodeType,
        category: 'intelligence',
        required_config: [],
        optional_config: [],
        min_config: {},
        outputs: ['rain_10min'],
        description: '',
      },
    ]
    lastRunOutputs.value = { a: { observed_at: '2026-10-22T09:00:00Z' } }
    expect(groups.value.find((group) => group.nodeId === 'a')?.keys).toEqual(['rain_10min', 'observed_at'])
  })
})
