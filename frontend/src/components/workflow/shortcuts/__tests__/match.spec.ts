import { describe, expect, it } from 'vitest'

import {
  findWorkflowShortcut,
  isTypingTarget,
  matchesWorkflowHotkey,
  resolveWorkflowKey,
  type WorkflowKeyEventLike,
} from '../match'

function key(key: string, extra: Partial<WorkflowKeyEventLike> = {}): WorkflowKeyEventLike {
  return { key, ...extra }
}

describe('matchesWorkflowHotkey', () => {
  it('Mod 在 Ctrl 与 Meta 上任一成立，单独 Ctrl/Meta 不会被非 Mod 规则接受', () => {
    expect(matchesWorkflowHotkey('Mod+C', key('c', { ctrlKey: true }))).toBe(true)
    expect(matchesWorkflowHotkey('Mod+C', key('c', { metaKey: true }))).toBe(true)
    expect(matchesWorkflowHotkey('Mod+C', key('c', { ctrlKey: true, metaKey: true }))).toBe(true)
    expect(matchesWorkflowHotkey('Mod+C', key('c'))).toBe(false)
    expect(matchesWorkflowHotkey('V', key('v', { ctrlKey: true }))).toBe(false)
  })

  it('没写的修饰键必须没按：Mod+Z 不会被 Mod+Shift+Z 抢先', () => {
    expect(matchesWorkflowHotkey('Mod+Z', key('z', { ctrlKey: true, shiftKey: true }))).toBe(false)
    expect(matchesWorkflowHotkey('Mod+Shift+Z', key('z', { ctrlKey: true, shiftKey: true }))).toBe(true)
    expect(matchesWorkflowHotkey('Mod+C', key('c', { ctrlKey: true, altKey: true }))).toBe(false)
    expect(matchesWorkflowHotkey('Alt+C', key('c', { altKey: true }))).toBe(true)
  })

  it('Shift 数字按 event.code 判，符号键也能对上', () => {
    expect(matchesWorkflowHotkey('Shift+1', key('!', { code: 'Digit1', shiftKey: true }))).toBe(true)
    expect(matchesWorkflowHotkey('Shift+1', key('!', { shiftKey: true }))).toBe(true)
    expect(matchesWorkflowHotkey('Mod+1', key('!', { ctrlKey: true, shiftKey: true }))).toBe(false)
    expect(matchesWorkflowHotkey('Mod+1', key('!', { ctrlKey: true }))).toBe(true)
    expect(matchesWorkflowHotkey('Shift+1', key('1', { code: 'Digit1', shiftKey: true }))).toBe(true)
    expect(matchesWorkflowHotkey('Mod+-', key('-', { ctrlKey: true }))).toBe(true)
    expect(matchesWorkflowHotkey('Mod+=', key('+', { ctrlKey: true, shiftKey: false }))).toBe(true)
  })

  it('`?` 允许 Shift（那正是敲出问号的方式），其余键多按 Shift 就不算', () => {
    expect(matchesWorkflowHotkey('?', key('?', { shiftKey: true }))).toBe(true)
    expect(matchesWorkflowHotkey('?', key('/', { code: 'Slash' }))).toBe(true)
    expect(matchesWorkflowHotkey('V', key('V', { shiftKey: true }))).toBe(false)
  })

  it('字母大小写不敏感，但空描述串不匹配任何事件', () => {
    expect(matchesWorkflowHotkey('Mod+S', key('S', { ctrlKey: true }))).toBe(true)
    expect(matchesWorkflowHotkey('Mod+S', key('s', { code: 'KeyS', metaKey: true }))).toBe(true)
    expect(matchesWorkflowHotkey('', key('s', { ctrlKey: true }))).toBe(false)
  })
})

describe('findWorkflowShortcut / resolveWorkflowKey', () => {
  it('表内快捷键优先于画布单键', () => {
    expect(findWorkflowShortcut(key('d', { ctrlKey: true }))).toBe('workflow.duplicate')
    expect(findWorkflowShortcut(key('Delete'))).toBe('workflow.delete')
    expect(findWorkflowShortcut(key('Backspace'))).toBe('workflow.delete')
    expect(findWorkflowShortcut(key('Escape'))).toBeNull()
  })

  it('无修饰的单键落到画布缩放，方向键落到微移', () => {
    expect(resolveWorkflowKey(key('1'))).toEqual({ kind: 'canvas', action: 'fit' })
    expect(resolveWorkflowKey(key('-'))).toEqual({ kind: 'canvas', action: 'zoom-out' })
    expect(resolveWorkflowKey(key('ArrowLeft'))).toEqual({ kind: 'move', delta: { x: -5, y: 0 } })
    expect(resolveWorkflowKey(key('ArrowDown', { shiftKey: true }))).toEqual({
      kind: 'move',
      delta: { x: 0, y: 20 },
    })
    expect(resolveWorkflowKey(key('a'))).toBeNull()
  })

  it('带修饰键的单键不触发画布缩放（留给浏览器）', () => {
    expect(resolveWorkflowKey(key('1', { altKey: true }))).toBeNull()
    expect(resolveWorkflowKey(key('0', { ctrlKey: true }))).toBeNull()
  })
})

describe('isTypingTarget', () => {
  it('输入框/文本域/下拉/可编辑区算输入环境，普通元素不算', () => {
    const input = document.createElement('input')
    const textarea = document.createElement('textarea')
    const select = document.createElement('select')
    const editable = document.createElement('div')
    editable.setAttribute('contenteditable', 'true')
    const button = document.createElement('button')

    expect(isTypingTarget(input)).toBe(true)
    expect(isTypingTarget(textarea)).toBe(true)
    expect(isTypingTarget(select)).toBe(true)
    expect(isTypingTarget(editable)).toBe(true)
    expect(isTypingTarget(button)).toBe(false)
    expect(isTypingTarget(null)).toBe(false)
    expect(isTypingTarget(document)).toBe(false)
  })
})
