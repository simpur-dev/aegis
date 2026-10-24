import { describe, expect, it } from 'vitest'

import movementFixture from '@/utils/graph/__fixtures__/dify-1.17.1/keyboard-movement.json'

import { getKeyboardMovement } from '../keyboard-movement'

interface MovementRow {
  key: string
  modifiers: 'plain' | 'shift' | 'alt' | 'ctrl' | 'meta' | 'shift-alt'
  actual: { x: number; y: number } | null
}

const rows = movementFixture.cases as unknown as MovementRow[]

function eventOf(row: MovementRow): {
  key: string
  shiftKey: boolean
  altKey: boolean
  ctrlKey: boolean
  metaKey: boolean
} {
  return {
    key: row.key,
    shiftKey: row.modifiers === 'shift' || row.modifiers === 'shift-alt',
    altKey: row.modifiers === 'alt' || row.modifiers === 'shift-alt',
    ctrlKey: row.modifiers === 'ctrl',
    metaKey: row.modifiers === 'meta',
  }
}

describe('键盘微移（Dify T1 端口）', () => {
  it('fixture 的 30 条组合逐条一致', () => {
    expect(rows).toHaveLength(30)
    for (const row of rows) {
      expect(getKeyboardMovement(eventOf(row)), `${row.key}/${row.modifiers}`).toEqual(row.actual)
    }
  })

  it('空对象（缺省修饰键）等同 plain：方向键 5px，其它键 null', () => {
    expect(getKeyboardMovement({ key: 'ArrowUp' })).toEqual({ x: 0, y: -5 })
    expect(getKeyboardMovement({ key: 'a' })).toBeNull()
  })
})
