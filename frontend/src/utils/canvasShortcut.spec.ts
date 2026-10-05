import { describe, expect, it } from 'vitest'

import { CANVAS_DEFAULT_ZOOM, canvasShortcut } from './graph'

/**
 * 画布快捷键的判定表。
 *
 * 只做"看哪儿"四件，别的键一律不接——Tab 那套装焦点围栏在用（`useFocusTrap`），
 * 而带 Ctrl/Meta/Alt 的组合键属于浏览器与 antd（Ctrl+A 全选、Ctrl+S 我们另有绑定）。
 */
describe('canvasShortcut', () => {
  it.each([
    ['1', 'fit'],
    ['0', 'reset'],
    ['+', 'zoom-in'],
    ['=', 'zoom-in'],
    ['-', 'zoom-out'],
    ['_', 'zoom-out'],
  ] as const)('裸键 %s → %s', (key, expected) => {
    expect(canvasShortcut(key, false)).toBe(expected)
  })

  it('其余按键不接（含字母、方向键、Escape、Tab）', () => {
    for (const key of ['a', '1 ', '00', 'ArrowUp', 'Escape', 'Tab', '', 'F5']) {
      expect(canvasShortcut(key, false), `键 "${key}" 不该被画布接走`).toBeNull()
    }
  })

  it('带修饰键一律不接（Ctrl+1 是浏览器切标签页）', () => {
    for (const key of ['1', '0', '+', '-']) {
      expect(canvasShortcut(key, true), `组合键 ${key} 不该被画布接走`).toBeNull()
    }
  })

  it('默认缩放与画布初始档位同源（快捷键 0 要回到那一档）', () => {
    expect(CANVAS_DEFAULT_ZOOM).toBeGreaterThan(0.2)
    expect(CANVAS_DEFAULT_ZOOM).toBeLessThan(1)
  })
})
