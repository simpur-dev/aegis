import { mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'

import { useWorkflowShortcuts, type WorkflowShortcutActions } from '../useWorkflowShortcuts'

function actions(): WorkflowShortcutActions {
  return {
    save: vi.fn(),
    deleteSelection: vi.fn(() => true),
    copy: vi.fn(async () => true),
    cut: vi.fn(async () => true),
    paste: vi.fn(async () => true),
    duplicate: vi.fn(() => true),
    undo: vi.fn(() => true),
    redo: vi.fn(() => true),
    setMode: vi.fn(),
    organize: vi.fn(),
    zoomToFit: vi.fn(),
    resetZoom: vi.fn(),
    zoomTo100: vi.fn(),
    zoomTo50: vi.fn(),
    zoomIn: vi.fn(),
    zoomOut: vi.fn(),
    help: vi.fn(),
    locate: vi.fn(),
    moveSelection: vi.fn(() => true),
  }
}

function press(key: string, extra: KeyboardEventInit = {}, target?: EventTarget): KeyboardEvent {
  const event = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...extra })
  if (target !== undefined) Object.defineProperty(event, 'target', { value: target })
  return event
}

const mounted: Array<ReturnType<typeof mount>> = []

function shortcuts(spy: WorkflowShortcutActions): (event: KeyboardEvent) => void {
  let handle: ((event: KeyboardEvent) => void) | null = null
  const wrapper = mount(
    defineComponent({
      setup: () => {
        handle = useWorkflowShortcuts(spy).handleKeydown
        return () => h('div')
      },
    }),
  )
  mounted.push(wrapper)
  if (handle === null) throw new Error('setup 未返回 handleKeydown')
  return handle
}

afterEach(() => {
  mounted.splice(0).forEach((wrapper) => wrapper.unmount())
})

describe('useWorkflowShortcuts 键盘分发', () => {
  it('表格快捷键分派到对应动作（剪切/撤销/重做/模式/视图）', () => {
    const spy = actions()
    const handleKeydown = shortcuts(spy)

    handleKeydown(press('x', { code: 'KeyX', ctrlKey: true }))
    handleKeydown(press('c', { code: 'KeyC', ctrlKey: true }))
    handleKeydown(press('v', { code: 'KeyV', ctrlKey: true }))
    handleKeydown(press('d', { code: 'KeyD', ctrlKey: true }))
    handleKeydown(press('z', { code: 'KeyZ', ctrlKey: true }))
    handleKeydown(press('z', { code: 'KeyZ', ctrlKey: true, shiftKey: true }))
    handleKeydown(press('y', { code: 'KeyY', ctrlKey: true }))
    handleKeydown(press('v', { code: 'KeyV' }))
    handleKeydown(press('h', { code: 'KeyH' }))
    handleKeydown(press('o', { code: 'KeyO', ctrlKey: true }))
    handleKeydown(press('f', { code: 'KeyF', ctrlKey: true }))
    handleKeydown(press('F1', { key: 'F1' }))

    expect(spy.cut).toHaveBeenCalledTimes(1)
    expect(spy.copy).toHaveBeenCalledTimes(1)
    expect(spy.paste).toHaveBeenCalledTimes(1)
    expect(spy.duplicate).toHaveBeenCalledTimes(1)
    expect(spy.undo).toHaveBeenCalledTimes(1)
    expect(spy.redo).toHaveBeenCalledTimes(2)
    expect(spy.setMode).toHaveBeenNthCalledWith(1, 'pointer')
    expect(spy.setMode).toHaveBeenNthCalledWith(2, 'hand')
    expect(spy.organize).toHaveBeenCalledTimes(1)
    expect(spy.locate).toHaveBeenCalledTimes(1)
    expect(spy.help).not.toHaveBeenCalled()
  })

  it('画布单键与方向键走兜底分支', () => {
    const spy = actions()
    const handleKeydown = shortcuts(spy)

    handleKeydown(press('1', { code: 'Digit1' }))
    handleKeydown(press('0', { code: 'Digit0' }))
    handleKeydown(press('+', { code: 'Equal', shiftKey: true }))
    handleKeydown(press('-', { code: 'Minus' }))
    handleKeydown(press('ArrowUp', { code: 'ArrowUp', shiftKey: true }))

    expect(spy.zoomToFit).toHaveBeenCalledTimes(1)
    expect(spy.resetZoom).toHaveBeenCalledTimes(1)
    expect(spy.zoomIn).toHaveBeenCalledTimes(1)
    expect(spy.zoomOut).toHaveBeenCalledTimes(1)
    expect(spy.moveSelection).toHaveBeenCalledWith({ x: 0, y: -20 })
  })

  it('Delete/Backspace 删选中，`?` 与 Shift 缩放各自归位', () => {
    const spy = actions()
    const handleKeydown = shortcuts(spy)

    handleKeydown(press('Delete', { code: 'Delete' }))
    handleKeydown(press('Backspace', { code: 'Backspace' }))
    handleKeydown(press('?', { code: 'Slash', shiftKey: true }))
    handleKeydown(press('!', { code: 'Digit1', shiftKey: true }))
    handleKeydown(press('%', { code: 'Digit5', shiftKey: true }))

    expect(spy.deleteSelection).toHaveBeenCalledTimes(2)
    expect(spy.help).toHaveBeenCalledTimes(1)
    expect(spy.zoomTo100).toHaveBeenCalledTimes(1)
    expect(spy.zoomTo50).toHaveBeenCalledTimes(1)
  })

  it('输入框里不抢画布键，但 Mod+S 仍然保存', () => {
    const spy = actions()
    const handleKeydown = shortcuts(spy)
    const input = document.createElement('input')

    handleKeydown(press('z', { code: 'KeyZ', ctrlKey: true }, input))
    handleKeydown(press('Delete', { code: 'Delete' }, input))
    handleKeydown(press('1', { code: 'Digit1' }, input))
    handleKeydown(press('s', { code: 'KeyS', ctrlKey: true }, input))

    expect(spy.undo).not.toHaveBeenCalled()
    expect(spy.deleteSelection).not.toHaveBeenCalled()
    expect(spy.zoomToFit).not.toHaveBeenCalled()
    expect(spy.save).toHaveBeenCalledTimes(1)
  })

  it('已消费的事件与不认识的键都不触发动作', () => {
    const spy = actions()
    const handleKeydown = shortcuts(spy)

    const consumed = press('c', { code: 'KeyC', ctrlKey: true })
    consumed.preventDefault()
    handleKeydown(consumed)
    handleKeydown(press('a', { code: 'KeyA' }))
    handleKeydown(press('Escape', { code: 'Escape' }))

    expect(spy.copy).not.toHaveBeenCalled()
    expect(spy.zoomToFit).not.toHaveBeenCalled()
    expect(spy.resetZoom).not.toHaveBeenCalled()
    expect(spy.help).not.toHaveBeenCalled()
  })

  it('命中的键会 preventDefault，避免浏览器默认行为（如 Backspace 后退）', () => {
    const handleKeydown = shortcuts(actions())
    const event = press('Backspace', { code: 'Backspace' })
    handleKeydown(event)
    expect(event.defaultPrevented).toBe(true)
  })
})
