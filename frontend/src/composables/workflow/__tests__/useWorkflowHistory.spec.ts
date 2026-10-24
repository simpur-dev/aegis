import { describe, expect, it } from 'vitest'

import type { WorkflowDocument } from '@/utils/graph/document'

import { useWorkflowHistory } from '../useWorkflowHistory'

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

function harness(initial: WorkflowDocument | null = makeDocument('initial')) {
  let document = initial
  const writes: string[] = []
  const history = useWorkflowHistory({
    read: () => document,
    write: (next) => {
      writes.push(next.name)
      document = next
    },
  })
  return {
    history,
    writes,
    current: () => document,
    set: (next: WorkflowDocument | null) => {
      document = next
    },
  }
}

describe('历史组合式', () => {
  it('记录一次修改：撤销把文档写回改前，重做再写回改后', async () => {
    const h = harness()
    expect(
      h.history.record('添加节点', () => {
        h.set(makeDocument('doc-1'))
      }),
    ).toBe(true)
    expect(h.history.canUndo.value).toBe(true)
    expect(h.history.undoLabel.value).toBe('添加节点')

    expect(await h.history.undo()).toBe(true)
    expect(h.current()?.name).toBe('initial')
    expect(h.history.canRedo.value).toBe(true)
    expect(h.history.redoLabel.value).toBe('添加节点')

    expect(await h.history.redo()).toBe(true)
    expect(h.current()?.name).toBe('doc-1')
    expect(h.writes).toEqual(['initial', 'doc-1'])
  })

  it('空栈时 undo/redo 返回 false 且不写文档', async () => {
    const h = harness()
    h.history.reset()
    expect(await h.history.undo()).toBe(false)
    expect(await h.history.redo()).toBe(false)
    expect(h.writes).toEqual([])
  })

  it('加载中（read 为 null）不建基线，也不执行修改', () => {
    const h = harness(null)
    expect(
      h.history.record('添加节点', () => {
        h.set(makeDocument('late'))
      }),
    ).toBe(false)
    expect(h.current()).toBeNull()
    expect(h.history.canUndo.value).toBe(false)
  })

  it('mutation 抛错时原样冒泡，历史不前进', () => {
    const h = harness()
    expect(() =>
      h.history.record('连接节点', () => {
        throw new Error('后端拒绝')
      }),
    ).toThrow('后端拒绝')
    expect(h.history.pastCount.value).toBe(0)
  })

  it('文档没变不产生历史', () => {
    const h = harness()
    expect(h.history.record('修改节点', () => undefined)).toBe(false)
    expect(h.history.pastCount.value).toBe(0)
  })

  it('异步修改（粘贴）等 mutation 完成才提交一步', async () => {
    const h = harness()
    const ok = await h.history.recordAsync('粘贴', async () => {
      await Promise.resolve()
      h.set(makeDocument('pasted'))
    })
    expect(ok).toBe(true)
    expect(h.history.pastCount.value).toBe(1)
    await h.history.undo()
    expect(h.current()?.name).toBe('initial')
  })

  it('写回失败时历史停在原处，避免"界面撤销了、文档没动"', async () => {
    let document: WorkflowDocument | null = makeDocument('initial')
    const history = useWorkflowHistory({
      read: () => document,
      write: () => {
        throw new Error('保存失败')
      },
    })
    history.record('添加节点', () => {
      document = makeDocument('doc-1')
    })
    await expect(history.undo()).rejects.toThrow('保存失败')
    expect(history.canUndo.value).toBe(true)
    expect(document?.name).toBe('doc-1')
  })

  it('reset 换文档后旧历史清空，且以后按新文档起算', () => {
    const h = harness()
    h.history.record('添加节点', () => {
      h.set(makeDocument('doc-1'))
    })
    expect(h.history.pastCount.value).toBe(1)

    h.history.reset(makeDocument('loaded'))
    expect(h.history.pastCount.value).toBe(0)
    expect(h.history.canUndo.value).toBe(false)

    h.history.record('重命名', () => {
      h.set(makeDocument('renamed'))
    })
    expect(h.history.pastCount.value).toBe(1)
    expect(h.history.undoLabel.value).toBe('重命名')
  })

  it('上限可配置：超过上限时丢弃最老一步', () => {
    const h = harness()
    const capped = useWorkflowHistory({
      read: () => h.current(),
      write: (next) => h.set(next),
      limit: 2,
    })
    capped.reset(makeDocument('base'))
    for (let index = 1; index <= 3; index += 1) {
      h.set(makeDocument(`doc-${index}`))
      capped.commitCurrent('修改节点')
    }
    expect(capped.pastCount.value).toBe(2)
  })

  it('没有基线时 undo/redo/recordAsync 都安全返回 false', async () => {
    const h = harness(null)
    h.history.reset(null)
    expect(await h.history.undo()).toBe(false)
    expect(await h.history.redo()).toBe(false)
    expect(await h.history.recordAsync('粘贴', async () => {})).toBe(false)
  })

  it('基线还在但文档被清空（切页/重载中）时提交为 no-op', () => {
    const h = harness()
    h.history.reset()
    h.set(null)
    expect(h.history.commitCurrent('修改节点')).toBe(false)
    expect(h.history.pastCount.value).toBe(0)
  })

  it('commitCurrent 只提交已发生的文档变化', () => {
    const h = harness()
    h.history.reset()
    expect(h.history.commitCurrent('重命名')).toBe(false)
    h.set(makeDocument('renamed'))
    expect(h.history.commitCurrent('重命名')).toBe(true)
    expect(h.history.pastCount.value).toBe(1)
    expect(h.history.futureCount.value).toBe(0)
  })
})
