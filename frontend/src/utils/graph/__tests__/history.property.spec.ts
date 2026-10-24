import fc from 'fast-check'
import { describe, expect, it } from 'vitest'

import { documentsEqual, emptyLayout, type WorkflowDocument } from '../document'
import {
  canRedoHistory,
  canUndoHistory,
  commitHistory,
  createHistoryState,
  redoHistory,
  undoHistory,
  undoHistoryLabel,
  type WorkflowHistoryState,
} from '../history'

function documentOf(name: string): WorkflowDocument {
  return {
    schema_version: 1,
    lineage_id: 'lin_abcdef',
    name,
    description: '',
    nodes: [],
    edges: [],
    layout: emptyLayout(),
  }
}

function replay(names: readonly string[], limit: number): WorkflowHistoryState | null {
  if (names.length === 0) return null
  let state = createHistoryState(documentOf(names[0] ?? ''))
  for (const name of names.slice(1)) {
    state = commitHistory(state, documentOf(name), name, limit)
  }
  return state
}

describe('撤销/重做的性质（fast-check）', () => {
  it('撤销到底再重做到底，回到同一份文档；边界操作是同一引用', () => {
    fc.assert(
      fc.property(
        fc.array(fc.string({ minLength: 1, maxLength: 12 }), { minLength: 2, maxLength: 20 }),
        fc.integer({ min: 1, max: 8 }),
        (names, limit) => {
          const state = replay(names, limit)
          if (state === null) throw new Error('样本为空')

          const steps = state.past.length
          expect(canUndoHistory(state)).toBe(steps > 0)

          let undone = state
          for (let index = 0; index < steps; index += 1) undone = undoHistory(undone)
          expect(canUndoHistory(undone)).toBe(false)
          expect(undoHistory(undone)).toBe(undone)

          let redone = undone
          for (let index = 0; index < steps; index += 1) redone = redoHistory(redone)
          expect(canRedoHistory(redone)).toBe(false)
          expect(redoHistory(redone)).toBe(redone)
          expect(documentsEqual(redone.present.document, state.present.document)).toBe(true)
        },
      ),
      { numRuns: 100 },
    )
  })

  it('同文提交不产生新步骤；新提交清空重做栈；past 长度不超过上限', () => {
    fc.assert(
      fc.property(
        fc.array(fc.string({ minLength: 1, maxLength: 12 }), { minLength: 1, maxLength: 20 }),
        fc.integer({ min: 1, max: 6 }),
        (names, limit) => {
          const state = replay(names, limit)
          if (state === null) throw new Error('样本为空')

          expect(state.past.length).toBeLessThanOrEqual(limit)
          expect(commitHistory(state, state.present.document, '同文', limit)).toBe(state)

          let undone = state
          while (canUndoHistory(undone)) undone = undoHistory(undone)
          if (undone.future.length === 0) return

          const resumed = commitHistory(undone, documentOf('新的一步'), '新的一步', limit)
          expect(resumed.future).toEqual([])
          expect(resumed.present.label).toBe('新的一步')
          expect(canRedoHistory(resumed)).toBe(false)
        },
      ),
      { numRuns: 80 },
    )
  })

  it('撤销标签描述的是即将撤掉的那一步', () => {
    fc.assert(
      fc.property(fc.string({ minLength: 1, maxLength: 12 }), (label) => {
        const state = commitHistory(createHistoryState(documentOf('a')), documentOf('b'), label)
        expect(undoHistoryLabel(state)).toBe(label)
        expect(undoHistoryLabel(undoHistory(state))).toBeNull()
      }),
      { numRuns: 40 },
    )
  })
})
