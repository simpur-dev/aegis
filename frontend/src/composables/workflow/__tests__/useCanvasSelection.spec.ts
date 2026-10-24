import { describe, expect, it } from 'vitest'

import selectionFixture from '@/utils/graph/__fixtures__/dify-1.17.1/selection.contract.json'
import type { Position } from '@/utils/graph'

import { nodesInMarquee, useCanvasSelection, type MarqueeRect } from '../useCanvasSelection'

interface FixtureNode {
  id: string
  position: Position
}

const nodes = selectionFixture.nodes as unknown as FixtureNode[]
type Action = Record<string, unknown>

function make(overrides: { readOnly?: boolean; onChange?: (s: unknown) => void } = {}) {
  return useCanvasSelection({
    readOnly: overrides.readOnly === undefined ? undefined : () => overrides.readOnly === true,
    onChange: overrides.onChange,
  })
}

function runActions(selection: ReturnType<typeof make>, actions: Action[], positions: Record<string, Position>) {
  for (const action of actions) {
    if (action.kind === 'marquee') {
      selection.marquee(nodes, action.rect as MarqueeRect, action.shift === true)
    } else if (action.kind === 'click') {
      selection.clickNode(action.nodeId as string, action.shift === true)
    } else if (action.kind === 'clickBlank') {
      selection.clickBlank()
    } else if (action.kind === 'moveNodes') {
      const current = selection.moveSelected(positions, action.delta as Position)
      Object.assign(positions, current)
    } else {
      throw new Error(`未处理的 fixture 动作：${String(action.kind)}`)
    }
  }
}

describe('选择契约（Dify T2 端口）', () => {
  it('fixture 的每个用例都一致', () => {
    for (const row of selectionFixture.cases) {
      const selection = make({ readOnly: row.readOnly === true })
      const positions: Record<string, Position> = Object.fromEntries(
        nodes.map((node) => [node.id, { ...node.position }]),
      )

      const expected = row.expected as { selectedNodeIds: string[]; positions?: Record<string, Position> }
      runActions(selection, row.actions as unknown as Action[], positions)

      expect(selection.selectedNodeIds.value, row.name).toEqual(expected.selectedNodeIds)
      const expectedPositions = expected.positions
      if (expectedPositions !== undefined) {
        for (const [nodeId, position] of Object.entries(expectedPositions)) {
          expect(positions[nodeId], `${row.name}/${nodeId}`).toEqual(position)
        }
      }
    }
  })

  it('框选相交判据含贴边：fixture 的 partial-intersection 正是压着 b 左沿', () => {
    expect(nodesInMarquee(nodes, { x: 200, y: 20, width: 100, height: 20 })).toEqual(['a', 'b'])
    expect(nodesInMarquee(nodes, { x: 250, y: 0, width: 60, height: 50 })).toEqual(['b'])
    expect(nodesInMarquee(nodes, { x: 1000, y: 1000, width: 10, height: 10 })).toEqual([])
  })

  it('Shift 框选与已有选择取并集，Shift 点选可取消单个', () => {
    const selection = make()
    selection.clickNode('a')
    selection.marquee(nodes, { x: 600, y: 200, width: 10, height: 10 }, true)
    expect(selection.selectedNodeIds.value).toEqual(['a', 'c'])
    selection.clickNode('c', true)
    expect(selection.selectedNodeIds.value).toEqual(['a'])
    expect(selection.primaryNodeId.value).toBe('a')
  })

  it('点节点清掉边的选择；点边清掉节点的选择；空白清空', () => {
    const selection = make()
    selection.clickEdge('a->b')
    expect(selection.selectedEdgeIds.value).toEqual(['a->b'])
    selection.clickNode('a')
    expect(selection.selectedEdgeIds.value).toEqual([])
    selection.clickEdge('a->b', true)
    selection.clickEdge('b->c', true)
    selection.clickEdge('b->c', true)
    expect(selection.selectedEdgeIds.value).toEqual(['a->b'])
    expect(selection.selectedNodeIds.value).toEqual([])
    selection.clickBlank()
    expect(selection.isEmpty.value).toBe(true)
  })

  it('只读时移动返回同一引用（调用方据此不落盘）', () => {
    const selection = make({ readOnly: true })
    const positions: Record<string, Position> = { a: { x: 0, y: 0 } }
    selection.clickNode('a')
    expect(selection.moveSelected(positions, { x: 10, y: 0 })).toBe(positions)
    expect(selection.selectedNodeIds.value).toEqual(['a'])
  })

  it('可写时只移动选中的、存在的节点并返回新对象', () => {
    const selection = make()
    const positions: Record<string, Position> = { a: { x: 0, y: 0 }, b: { x: 300, y: 0 } }
    selection.clickNode('a')
    selection.clickNode('missing', true)
    const moved = selection.moveSelected(positions, { x: 5, y: -5 })
    expect(moved).not.toBe(positions)
    expect(moved).toEqual({ a: { x: 5, y: -5 }, b: { x: 300, y: 0 } })
    expect(positions.a).toEqual({ x: 0, y: 0 })
  })

  it('空选择移动也是同一引用', () => {
    const selection = make()
    const positions: Record<string, Position> = { a: { x: 0, y: 0 } }
    expect(selection.moveSelected(positions, { x: 1, y: 1 })).toBe(positions)
  })

  it('每次变化都通知外部（编辑层据此同步选中节点）', () => {
    const seen: string[][] = []
    const selection = make({
      onChange: (state) => {
        const typed = state as { nodeIds: readonly string[] }
        seen.push([...typed.nodeIds])
      },
    })
    selection.clickNode('a')
    selection.clickNode('b', true)
    selection.clickBlank()
    expect(seen).toEqual([['a'], ['a', 'b'], []])
  })

  it('setNodes/setEdges 去重，便于接 Vue Flow 的框选回调', () => {
    const selection = make()
    selection.setNodes(['a', 'a', 'b'])
    expect(selection.selectedNodeIds.value).toEqual(['a', 'b'])
    selection.setEdges(['e1', 'e1'])
    expect(selection.selectedEdgeIds.value).toEqual(['e1'])
  })
})
