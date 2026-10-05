import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { attachFocusTrap } from '../useFocusTrap'

/**
 * 焦点围栏：抽屉打开后 Tab 会溜到页面背后（真机量过：两次之内到 body，然后走顶栏链接），
 * 这里钉住"抓回来 / 首尾循环 / Esc 交出去 / detach 之后不再拦"。
 */
describe('attachFocusTrap', () => {
  let host: HTMLElement

  beforeEach(() => {
    host = document.createElement('div')
    host.innerHTML = '<button id="a">A</button><button id="b">B</button><button id="c">C</button>'
    document.body.append(host)
  })
  afterEach(() => {
    host.remove()
  })

  const btn = (id: string): HTMLElement => host.querySelector<HTMLElement>(`#${id}`) as HTMLElement
  const focused = (): string => (document.activeElement as HTMLElement)?.id ?? ''

  it('root 为 null 时不炸，detach 也是空操作', () => {
    const handle = attachFocusTrap(null)
    expect(() => handle.detach()).not.toThrow()
  })

  it('挂上就把焦点拉进容器，Tab 到最后一个之后回到第一个', () => {
    const handle = attachFocusTrap(host)
    expect(focused()).toBe('a')
    btn('c').focus()
    const event = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true })
    document.dispatchEvent(event)
    expect(focused()).toBe('a')
    handle.detach()
  })

  it('Shift+Tab 在第一个上回到最后一个', () => {
    const handle = attachFocusTrap(host)
    btn('a').focus()
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true, cancelable: true }))
    expect(focused()).toBe('c')
    handle.detach()
  })

  it('焦点已经在容器外时，Tab 被拦下并抓回来（不许让它去走顶栏链接）', () => {
    const outside = document.createElement('button')
    outside.id = 'out'
    document.body.append(outside)
    const handle = attachFocusTrap(host)
    outside.focus()
    const event = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true })
    const prevented = !document.dispatchEvent(event)
    expect(prevented, '这次 Tab 该被 preventDefault').toBe(true)
    expect(['a', 'b', 'c']).toContain(focused())
    handle.detach()
    outside.remove()
  })

  it('Esc 交回调用方（由它决定关不关），其它键不拦', () => {
    const onEscape = vi.fn()
    const handle = attachFocusTrap(host, onEscape)
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    expect(onEscape).toHaveBeenCalledTimes(1)
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    handle.detach()
  })

  it('detach 之后不再拦 Tab', () => {
    const handle = attachFocusTrap(host)
    handle.detach()
    btn('c').focus()
    const event = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true })
    expect(!event.defaultPrevented && document.dispatchEvent(event)).toBe(true)
    expect(focused()).toBe('c')
  })
})
