import fc from 'fast-check'
import { describe, expect, it } from 'vitest'

import type { EdgeDef, NodeDef, NodeType, Position } from '../../graph'
import { remapWorkflowClipboardSubgraph, type ClipboardPayload } from '../clipboard'

const TYPES: readonly NodeType[] = ['start', 'data_fetch', 'join', 'end']

function nodeOf(nodeId: string, type: NodeType, config: Record<string, unknown> = {}): NodeDef {
  return {
    node_id: nodeId,
    type,
    name: nodeId,
    config: config as NodeDef['config'],
    sla_ms: 5_000,
    timeout_ms: 5_000,
    on_failure: 'retry',
    retry: { max_attempts: 1, backoff_ms: 200 },
  }
}

const defaults = (type: NodeType, nodeId: string): NodeDef => nodeOf(nodeId, type)

function remap(payload: ClipboardPayload, offset: Position) {
  let index = 0
  return remapWorkflowClipboardSubgraph(payload, {
    allocateId: (type) => {
      index += 1
      return `${type}_${index}`
    },
    createDefault: defaults,
    offset,
  })
}

const edgeKey = (edge: EdgeDef): string => `${edge.source}→${edge.target}|${edge.condition}`

describe('粘贴换号的性质（fast-check）', () => {
  it('合法内部边一一对应地换号；节点数、位置与 id 映射保持一一对应', () => {
    const graph = fc.record({
      size: fc.integer({ min: 2, max: 8 }),
      edges: fc.array(fc.tuple(fc.nat(7), fc.nat(7), fc.constantFrom('', 'high', 'low')), { maxLength: 16 }),
      offset: fc.record({ x: fc.integer({ min: -500, max: 500 }), y: fc.integer({ min: -500, max: 500 }) }),
    })

    fc.assert(
      fc.property(graph, ({ size, edges, offset }) => {
        const ids = Array.from({ length: size }, (_, index) => `n${index}`)
        const nodes = ids.map((id, index) => ({
          node: nodeOf(id, TYPES[index % TYPES.length] ?? 'data_fetch', { upstream: ids }) as unknown as Record<
            string,
            unknown
          >,
          position: { x: index * 200, y: index * 50 },
        }))
        const payload: ClipboardPayload = {
          nodes,
          edges: edges
            .filter(([source, target]) => source < size && target < size && source !== target)
            .map(([source, target, condition]) => ({ source: `n${source}`, target: `n${target}`, condition })),
        }

        const result = remap(payload, offset)

        expect(result.nodes).toHaveLength(size)
        expect(Object.keys(result.idMapping)).toHaveLength(size)
        expect(new Set(Object.values(result.idMapping)).size).toBe(size)

        for (const [index, id] of ids.entries()) {
          const mapped = result.idMapping[id]
          if (mapped === undefined) throw new Error(`节点 ${id} 没有新号`)
          expect(result.positions[mapped]).toEqual({ x: index * 200 + offset.x, y: index * 50 + offset.y })
          expect(result.nodes[index]?.config.upstream).toEqual(ids.map((source) => result.idMapping[source]))
        }

        const expectedEdges = new Set<string>()
        for (const edge of payload.edges) {
          const source = result.idMapping[edge.source]
          const target = result.idMapping[edge.target]
          if (source === undefined || target === undefined || source === target) continue
          expectedEdges.add(edgeKey({ source, target, condition: edge.condition }))
        }

        const actualEdges = result.edges.map(edgeKey)
        expect(new Set(actualEdges).size, '结果里有重复边').toBe(actualEdges.length)
        expect(new Set(actualEdges)).toEqual(expectedEdges)
        for (const edge of result.edges) expect(edge.source).not.toBe(edge.target)
      }),
      { numRuns: 120 },
    )
  })

  it('未知类型节点被丢弃，涉及它的边一并消失，映射不会指向不存在的节点', () => {
    fc.assert(
      fc.property(fc.boolean(), (keepInternalEdge) => {
        const payload: ClipboardPayload = {
          nodes: [
            { node: nodeOf('a', 'data_fetch') as unknown as Record<string, unknown>, position: { x: 0, y: 0 } },
            { node: { node_id: 'x', type: 'definitely_not_a_node', name: 'x' }, position: { x: 10, y: 10 } },
            { node: nodeOf('b', 'end') as unknown as Record<string, unknown>, position: { x: 20, y: 20 } },
          ],
          edges: [
            { source: 'a', target: 'x', condition: '' },
            { source: 'x', target: 'b', condition: '' },
            ...(keepInternalEdge ? [{ source: 'a', target: 'b', condition: 'high' }] : []),
          ],
        }

        const result = remap(payload, { x: 0, y: 0 })
        expect(result.nodes).toHaveLength(2)
        expect(result.idMapping.x).toBeUndefined()
        expect(result.edges.some((edge) => edge.source === 'x' || edge.target === 'x')).toBe(false)
        expect(result.edges).toHaveLength(keepInternalEdge ? 1 : 0)
        for (const edge of result.edges) {
          expect(result.nodes.some((node) => node.node_id === edge.source)).toBe(true)
          expect(result.nodes.some((node) => node.node_id === edge.target)).toBe(true)
        }
      }),
      { numRuns: 80 },
    )
  })
})
