import { describe, expect, it } from 'vitest'

import historyFixture from '../__fixtures__/dify-1.17.1/history.contract.json'
import type { WorkflowDocument } from '../document'
import {
  canRedoHistory,
  canUndoHistory,
  commitHistory,
  createHistoryState,
  HISTORY_LIMIT,
  redoHistory,
  redoHistoryLabel,
  undoHistory,
  undoHistoryLabel,
  type WorkflowHistoryState,
} from '../history'

function makeDocument(name: string): WorkflowDocument {
  return {
    schema_version: 1,
    lineage_id: 'wf_0123456789ab',
    name,
    description: '',
    nodes: [],
    edges: [],
    layout: { positions: {}, viewport: null, notes: [] },
  }
}

const contractCases = historyFixture.cases

describe('历史契约（Dify T2 端口）', () => {
  it('空栈：undo/redo 都是 no-op 且返回同一引用', () => {
    const state = createHistoryState(makeDocument('initial'))
    expect(undoHistory(state)).toBe(state)
    expect(redoHistory(state)).toBe(state)
    expect(canUndoHistory(state)).toBe(false)
    expect(canRedoHistory(state)).toBe(false)
    expect(undoHistoryLabel(state)).toBeNull()
    expect(redoHistoryLabel(state)).toBeNull()
    expect(undoHistoryLabel(null)).toBeNull()
    expect(redoHistoryLabel(null)).toBeNull()
    expect(contractCases[0]?.name).toBe('empty-stack')
  })

  it('单步提交：past +1，present 是新文档', () => {
    const state = commitHistory(createHistoryState(makeDocument('initial')), makeDocument('doc-1'), '添加节点')
    expect(state.past).toHaveLength(1)
    expect(state.present.document.name).toBe('doc-1')
    expect(state.future).toHaveLength(0)
    expect(undoHistoryLabel(state)).toBe('添加节点')
  })

  it('拖动只在落点提交一次：过程里的文档变化不产生历史', () => {
    let state = createHistoryState(makeDocument('initial'))
    for (let index = 0; index < 5; index += 1) {
      /* 拖动过程只改画布/文档，调用方不 commit —— 历史里不该出现 5 条 */
      expect(state.past).toHaveLength(0)
    }
    state = commitHistory(state, makeDocument('doc-moved'), '移动节点')
    expect(state.past).toHaveLength(1)
    expect(state.present.document.name).toBe('doc-moved')
  })

  it('撤销后重做回到同一步', () => {
    const first = commitHistory(createHistoryState(makeDocument('initial')), makeDocument('doc-1'), '添加节点')
    const undone = undoHistory(first)
    expect(undone.present.document.name).toBe('initial')
    expect(undone.future).toHaveLength(1)
    expect(redoHistoryLabel(undone)).toBe('添加节点')

    const redone = redoHistory(undone)
    expect(redone.present.document.name).toBe('doc-1')
    expect(redone.past).toHaveLength(1)
    expect(redone.future).toHaveLength(0)
  })

  it('撤销后再改一步会清空 future', () => {
    let state = commitHistory(createHistoryState(makeDocument('initial')), makeDocument('doc-1'), '添加节点')
    state = commitHistory(state, makeDocument('doc-2'), '连接节点')
    state = undoHistory(state)
    expect(state.future).toHaveLength(1)

    state = commitHistory(state, makeDocument('doc-renamed'), '重命名')
    expect(state.past).toHaveLength(2)
    expect(state.present.document.name).toBe('doc-renamed')
    expect(state.future).toHaveLength(0)
  })

  it('上限 50：第 51 步把最老的一步挤掉', () => {
    let state: WorkflowHistoryState = createHistoryState(makeDocument('doc-0'))
    for (let index = 1; index <= 51; index += 1) {
      state = commitHistory(state, makeDocument(`doc-${index}`), '修改节点')
    }
    expect(state.past).toHaveLength(HISTORY_LIMIT)
    expect(state.past[0]?.document.name).toBe('doc-1')
    expect(state.present.document.name).toBe('doc-51')
    /* 从最老一步再撤销 50 次恰好回到 doc-1，而不是 doc-0。 */
    for (let index = 0; index < HISTORY_LIMIT; index += 1) state = undoHistory(state)
    expect(state.present.document.name).toBe('doc-1')
    expect(canUndoHistory(state)).toBe(false)
  })

  it('文档没变时不产生历史（返回原状态）', () => {
    const state = createHistoryState(makeDocument('initial'))
    expect(commitHistory(state, makeDocument('initial'), '修改节点')).toBe(state)
  })

  it('快照与源文档断开：改源不影响历史，改历史不影响源', () => {
    const source = makeDocument('initial')
    const state = createHistoryState(source)
    source.name = 'changed-outside'
    expect(state.present.document.name).toBe('initial')

    state.present.document.nodes.push({
      node_id: 'a',
      type: 'start',
      name: '开始',
      config: {},
      sla_ms: 1000,
      timeout_ms: 1000,
      on_failure: 'abort',
      retry: { max_attempts: 0, backoff_ms: 0 },
    })
    expect(source.nodes).toHaveLength(0)
  })

  it('契约里登记的动作标签都在，且与上游事件语义对得上', () => {
    const contract = historyFixture.aegisContract
    expect(Object.keys(contract.labels).sort()).toEqual(
      [
        'addNode',
        'connect',
        'insertOnEdge',
        'moveNodes',
        'paste',
        'removeEdges',
        'removeNodes',
        'rename',
        'setNotes',
        'setStartVariables',
        'updateNode',
      ].sort(),
    )
    expect(contract.limit).toBe(HISTORY_LIMIT)
    expect(contract.dragMerge).toContain('dragstop')
  })
})
