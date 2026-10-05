import { describe, expect, it } from 'vitest'

import { CANVAS_DEFAULT_ZOOM, canvasShortcut, strokeCompensation } from './graph'

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

/**
 * 描边补偿：画布 zoom 作用在变换层上，1px 的连线与节点描边会跟着缩。
 * 真机打开那份 111 节点的定义，fit 落在 0.318 ⇒ 描边只剩 0.32 设备像素。
 */
describe('strokeCompensation', () => {
  it('zoom=1 不补，放大也不补（下限 1）', () => {
    expect(strokeCompensation(1)).toBe(1)
    expect(strokeCompensation(2)).toBe(1)
  })

  it('缩小才补：0.5 → 2 倍', () => {
    expect(strokeCompensation(0.5)).toBe(2)
  })

  it('封顶 3 倍（再往上补只会让线糊成一团）', () => {
    expect(strokeCompensation(0.318)).toBe(3)
    expect(strokeCompensation(0.2)).toBe(3)
  })

  it('非法 zoom 按 1 处理，不炸出 NaN 把样式整个带坏', () => {
    for (const bad of [Number.NaN, 0, -1, Number.POSITIVE_INFINITY]) {
      expect(strokeCompensation(bad), `zoom=${bad}`).toBe(1)
    }
  })

  it('真机那一档补满之后，1.5px 的线仍有 ≥1.4 设备像素（这就是封顶取 3 的依据）', () => {
    const zoom = 0.318
    expect(1.5 * strokeCompensation(zoom) * zoom).toBeGreaterThanOrEqual(1.4)
    /* 对照：不补的话是 0.48 设备像素——细到基本看不见。 */
    expect(1.5 * zoom).toBeLessThan(0.5)
  })
})
