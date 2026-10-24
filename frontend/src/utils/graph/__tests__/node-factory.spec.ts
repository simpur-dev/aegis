import { describe, expect, it } from 'vitest'

import nodeFixture from '../__fixtures__/dify-1.17.1/node-factory.json'
import { genNewNodeTitleFromOld, getTopLeftNodePosition } from '../node-factory'

describe('node-factory（Dify T1 端口）', () => {
  it('标题递增与上游 fixture 逐项一致', () => {
    for (const row of nodeFixture.titleCases) {
      expect(genNewNodeTitleFromOld(row.input), row.input).toBe(row.actual)
    }
  })

  it('边界：空标题、纯空白、超大序号', () => {
    expect(genNewNodeTitleFromOld('')).toBe(' (1)')
    expect(genNewNodeTitleFromOld('   ')).toBe('    (1)')
    expect(genNewNodeTitleFromOld('节点 (999999999)')).toBe('节点 (1000000000)')
  })

  it('非法后缀：括号不闭合、负数、非数字都当作新基名', () => {
    expect(genNewNodeTitleFromOld('节点 (x)')).toBe('节点 (x) (1)')
    expect(genNewNodeTitleFromOld('节点 (-1)')).toBe('节点 (-1) (1)')
    expect(genNewNodeTitleFromOld('节点 (1')).toBe('节点 (1 (1)')
  })

  it('左上角落点与上游 fixture 一致；空集显式返回 null', () => {
    for (const row of nodeFixture.positionCases) {
      if (row.name === 'empty') continue
      const nodes = row.nodes.map((node) => ({ position: node.position }))
      expect(getTopLeftNodePosition(nodes), row.name).toEqual(row.actual)
    }
    expect(getTopLeftNodePosition([])).toBeNull()
  })

  it('边界：含 NaN/Infinity 的坐标不返回伪落点', () => {
    expect(getTopLeftNodePosition([{ position: { x: Number.NaN, y: 5 } }])).toBeNull()
    expect(getTopLeftNodePosition([{ position: { x: 0, y: Number.POSITIVE_INFINITY } }])).toBeNull()
  })
})
