import { describe, expect, it } from 'vitest'

import { colorSlots } from '@/theme/tokens'
import connectionFixture from '@/utils/graph/__fixtures__/dify-1.17.1/connections.json'
import edgeColorFixture from '@/utils/graph/__fixtures__/dify-1.17.1/edge-color.json'
import variableFixture from '@/utils/graph/__fixtures__/dify-1.17.1/variable.json'
import type { NodeState } from '@/utils/graph'
import { normalizeWorkflowOutputName, variableTransformer } from '@/utils/graph/variable'

import {
  getNodesConnectedSourceOrTargetHandleIdsMap,
  type ConnectedHandleIds,
  type ConnectedHandleNode,
  type ConnectionChange,
} from '../editor/connections'
import { getEdgeColor } from '../editor/edge-color'

describe('edge-color（Dify T1 端口）', () => {
  it('AEGIS 全状态与 fixture 的 aegisCases 一致', () => {
    for (const row of edgeColorFixture.aegisCases) {
      expect(getEdgeColor(row.state as NodeState | null, row.isFailBranch), `${row.state}/${row.isFailBranch}`).toBe(
        row.actual,
      )
    }
  })

  it('边界：省略状态与未知状态都走默认边色', () => {
    expect(getEdgeColor()).toBe('var(--aeg-color-editor-edge)')
    expect(getEdgeColor(undefined, true)).toBe('var(--aeg-color-editor-edge)')
    expect(getEdgeColor('exception' as NodeState)).toBe('var(--aeg-color-editor-edge)')
  })

  it('返回的每个 token 在深浅两套主题里都存在', () => {
    const tokens = new Set(edgeColorFixture.aegisCases.map((row) => row.actual))
    for (const token of tokens) {
      const slot = token.replace(/^var\(--aeg-color-/, '').replace(/\)$/, '')
      expect(colorSlots.light, `浅色缺 ${slot}`).toHaveProperty(slot)
      expect(colorSlots.dark, `深色缺 ${slot}`).toHaveProperty(slot)
    }
  })
})

describe('connections（Dify T1 端口）', () => {
  function initialNodes(): ConnectedHandleNode[] {
    return connectionFixture.initialNodes.map((node) => ({
      id: node.id,
      data: {
        _connectedSourceHandleIds: node.data._connectedSourceHandleIds,
        _connectedTargetHandleIds: node.data._connectedTargetHandleIds,
      },
    }))
  }

  /** JSON fixture 的 `type` 推成 string；这里收窄成端口要求的字面量联合。 */
  function changesOf(row: (typeof connectionFixture.cases)[number]): ConnectionChange[] {
    return row.changes.map((change) => ({
      type: change.type as ConnectionChange['type'],
      edge: change.edge,
    }))
  }

  it('加边/删边/默认 handle/同节点多次变更与上游 fixture 一致', () => {
    for (const row of connectionFixture.cases) {
      if (row.name === 'remove-missing-handle') continue
      const result = getNodesConnectedSourceOrTargetHandleIdsMap(changesOf(row), initialNodes())
      expect(result, row.name).toEqual(row.actual)
    }
  })

  it('偏差：删除不存在的 handle 是 no-op，不误删末尾（fixture aegisExpectation）', () => {
    const row = connectionFixture.cases.find((item) => item.name === 'remove-missing-handle')
    expect(row).toBeDefined()
    const result = getNodesConnectedSourceOrTargetHandleIdsMap(changesOf(row!), initialNodes())
    const expected = connectionFixture.aegisExpectation['remove-missing-handle']
    expect(result.a?._connectedSourceHandleIds).toEqual(expected.expectedSourceHandles)
    expect(result.b?._connectedTargetHandleIds).toEqual(expected.expectedTargetHandles)
  })

  it('边界：边的一端不在节点表里时只更新存在的一端', () => {
    const result = getNodesConnectedSourceOrTargetHandleIdsMap(
      [{ type: 'add', edge: { source: 'a', target: 'ghost', sourceHandle: 'then', targetHandle: 'target' } }],
      initialNodes(),
    )
    expect(Object.keys(result)).toEqual(['a'])
    expect(result.a).toEqual({
      _connectedSourceHandleIds: ['then', 'else', 'then'],
      _connectedTargetHandleIds: [],
    } satisfies ConnectedHandleIds)
  })

  it('不修改传入节点的原始 handle 数组', () => {
    const nodes = initialNodes()
    getNodesConnectedSourceOrTargetHandleIdsMap(
      [{ type: 'remove', edge: { source: 'a', target: 'b', sourceHandle: 'then' } }],
      nodes,
    )
    expect(nodes[0]?.data?._connectedSourceHandleIds).toEqual(['then', 'else'])
    expect(nodes[1]?.data?._connectedTargetHandleIds).toEqual(['target'])
  })

  it('非法输入：空 source/target 名称不会凭空造出节点条目', () => {
    const result = getNodesConnectedSourceOrTargetHandleIdsMap(
      [{ type: 'remove', edge: { source: '', target: '' } }],
      initialNodes(),
    )
    expect(result).toEqual({})
  })
})

describe('variable（Dify T1 端口）', () => {
  it('选择器 ↔ 模板串与 1.17.1 fixture 逐例一致', () => {
    for (const row of variableFixture.selectorCases) {
      const actual =
        typeof row.input === 'string'
          ? variableTransformer(row.input as string)
          : variableTransformer(row.input as readonly string[])
      expect(actual, row.name).toEqual(row.actual)
    }
  })

  it('输出名归一与 fixture 一致（undefined 归空串而不是 "undefined"）', () => {
    for (const row of variableFixture.normalizeCases) {
      expect(normalizeWorkflowOutputName(row.input), JSON.stringify(row.input)).toBe(row.actual)
    }
  })

  it('反方向回填：node_id + 输出键拼出可直接塞进配置的整串引用', () => {
    expect(variableTransformer(['fetch_1', 'rows'])).toBe('{{#fetch_1.rows#}}')
    expect(variableTransformer(['fetch_1', 'rows'])).not.toContain('undefined')
  })
})
