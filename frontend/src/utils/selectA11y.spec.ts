import { afterEach, describe, expect, it } from 'vitest'

import { installSelectA11y, syncSelectA11y } from '@/utils/selectA11y'

/** 造一个 rc-select 关闭态的最小结构：外层带 aria-label，input 带悬空引用、缺 expanded。 */
function makeSelect(label: string, open = false): HTMLElement {
  const sel = document.createElement('div')
  sel.className = open ? 'ant-select ant-select-open' : 'ant-select'
  sel.setAttribute('aria-label', label)
  const input = document.createElement('input')
  input.setAttribute('role', 'combobox')
  input.setAttribute('aria-owns', 'rc_select_9_list')
  input.setAttribute('aria-controls', 'rc_select_9_list')
  input.setAttribute('aria-activedescendant', 'rc_select_9_list_0')
  sel.appendChild(input)
  return sel
}

function inputOf(sel: HTMLElement): HTMLInputElement {
  return sel.querySelector('input') as HTMLInputElement
}

afterEach(() => {
  document.body.innerHTML = ''
})

describe('rc-select 无障碍同步：关闭态摘悬空引用、补名补 expanded', () => {
  it('关闭态：label 抄给 input、expanded=false、三个悬空引用摘掉并暂存', () => {
    const sel = makeSelect('区域')
    document.body.appendChild(sel)
    syncSelectA11y()
    const input = inputOf(sel)
    expect(input.getAttribute('aria-label')).toBe('区域')
    expect(input.getAttribute('aria-expanded')).toBe('false')
    expect(input.hasAttribute('aria-owns')).toBe(false)
    expect(input.hasAttribute('aria-controls')).toBe(false)
    expect(input.hasAttribute('aria-activedescendant')).toBe(false)
    expect(input.dataset.aegariaowns).toBe('rc_select_9_list')
  })

  it('展开态：expanded=true，暂存的引用装回去', () => {
    const sel = makeSelect('指标')
    document.body.appendChild(sel)
    syncSelectA11y()
    sel.classList.add('ant-select-open')
    syncSelectA11y()
    const input = inputOf(sel)
    expect(input.getAttribute('aria-expanded')).toBe('true')
    expect(input.getAttribute('aria-owns')).toBe('rc_select_9_list')
    expect(input.getAttribute('aria-controls')).toBe('rc_select_9_list')
  })

  it('input 已有自己的 aria-label 时不被外层覆盖', () => {
    const sel = makeSelect('外层')
    inputOf(sel).setAttribute('aria-label', '内层')
    document.body.appendChild(sel)
    syncSelectA11y()
    expect(inputOf(sel).getAttribute('aria-label')).toBe('内层')
  })
})

describe('installSelectA11y：后挂的下拉与开合变化都跟得上', () => {
  it('observer 装上后新增的 select 也被同步', async () => {
    const uninstall = installSelectA11y()
    try {
      const sel = makeSelect('区域')
      document.body.appendChild(sel)
      await Promise.resolve()
      expect(inputOf(sel).getAttribute('aria-expanded')).toBe('false')
      expect(inputOf(sel).getAttribute('aria-label')).toBe('区域')
    } finally {
      uninstall()
    }
  })

  it('class 切到展开态时 expanded 跟着翻', async () => {
    const sel = makeSelect('区域')
    document.body.appendChild(sel)
    const uninstall = installSelectA11y()
    try {
      sel.classList.add('ant-select-open')
      await Promise.resolve()
      expect(inputOf(sel).getAttribute('aria-expanded')).toBe('true')
    } finally {
      uninstall()
    }
  })

  it('卸载后不再同步（observer 真的断开了）', async () => {
    const uninstall = installSelectA11y()
    uninstall()
    const sel = makeSelect('区域')
    document.body.appendChild(sel)
    await Promise.resolve()
    expect(inputOf(sel).hasAttribute('aria-expanded')).toBe(false)
  })
})