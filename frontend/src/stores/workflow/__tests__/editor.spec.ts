import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { WorkflowError } from '@/api/workflow/contract'

import { createEditorStore, type EditorStore } from '../editor'

let editor: EditorStore

beforeEach(() => {
  setActivePinia(createPinia())
  editor = createEditorStore()()
})

describe('编辑层：易失界面状态', () => {
  it('初始是草稿模式、没有选中节点、没有错误', () => {
    expect(editor.mode).toEqual({ kind: 'draft' })
    expect(editor.selectedNodeId).toBeNull()
    expect(editor.error).toBeNull()
    expect(editor.loading).toBe(false)
  })

  it('模式与选中节点各自可设，错误可清', () => {
    editor.select('review_1')
    expect(editor.selectedNodeId).toBe('review_1')
    editor.select(null)
    expect(editor.selectedNodeId).toBeNull()

    editor.setMode({ kind: 'version', workflowId: 'wf_0123456789ab' })
    expect(editor.mode).toEqual({ kind: 'version', workflowId: 'wf_0123456789ab' })
    editor.setMode({ kind: 'instance', instanceId: 'wfi_0123456789ab', readOnly: true })
    expect(editor.mode).toEqual({
      kind: 'instance',
      instanceId: 'wfi_0123456789ab',
      readOnly: true,
    })

    editor.setError('接口没通')
    expect(editor.error).toBe('接口没通')
    editor.clearError()
    expect(editor.error).toBeNull()
  })

  it('节点与备注互斥：选中一个就松开另一个', () => {
    editor.select('review_1')
    editor.selectNote('note_2')
    expect(editor.selectedNoteId).toBe('note_2')
    expect(editor.selectedNodeId, '选中备注之后节点还在选中态——两处高亮会各说各话').toBeNull()

    editor.select('fetch_1')
    expect(editor.selectedNodeId).toBe('fetch_1')
    expect(editor.selectedNoteId, '反过来选节点时备注没松开').toBeNull()

    editor.selectNote(null)
    expect(editor.selectedNoteId).toBeNull()
  })

  it('runAction 成功后清掉旧错误，忙碌标记归位', async () => {
    editor.setError('上一次的错')
    const action = vi.fn(async () => {})
    expect(await editor.runAction(action, '执行失败')).toBe(true)
    expect(action).toHaveBeenCalledTimes(1)
    expect(editor.error).toBeNull()
    expect(editor.loading).toBe(false)
  })

  it('runAction 把后端原因收成“标签：原因”，不吞异常', async () => {
    const action = vi.fn(async () => {
      throw new WorkflowError('revision_conflict', 409, '草稿已被其他会话修改')
    })
    expect(await editor.runAction(action, '保存失败')).toBe(false)
    expect(editor.error).toBe('保存失败：草稿已被其他会话修改')
    expect(editor.loading).toBe(false)
  })

  it('非 WorkflowError 也只取 message，不拼 HTTP 码', async () => {
    const action = vi.fn(async () => {
      throw new Error('上游 502')
    })
    expect(await editor.runAction(action, '刷新失败')).toBe(false)
    expect(editor.error).toBe('刷新失败：上游 502')
  })
})
