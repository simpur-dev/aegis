/**
 * 助手悬浮图标的两条纯规则：竖直夹紧、点击/拖拽判定。
 *
 * 布局本身（图标真的停在哪）由真机截图验，这里钉的是"越界不许出去、拖完松手不许误开"。
 */

import { describe, expect, it } from 'vitest'

import { clampTop, isClick } from '@/components/assistant/dock'

describe('竖直位置夹紧', () => {
  it('往上拖不过顶栏：上界 = 顶栏高 + 8', () => {
    expect(clampTop(0, 900, 68)).toBe(76)
    expect(clampTop(-400, 900, 68)).toBe(76)
  })

  it('往下拖不出视口：下界 = 视口高 − 56 − 8', () => {
    expect(clampTop(2000, 900, 68)).toBe(836)
  })

  it('正常范围原样返回（取整）', () => {
    expect(clampTop(400.4, 900, 68)).toBe(400)
    expect(clampTop(76, 900, 68)).toBe(76)
    expect(clampTop(836, 900, 68)).toBe(836)
  })

  it('视口比顶栏还矮时，上下界合并到上界，不出现倒挂区间', () => {
    expect(clampTop(500, 100, 68)).toBe(76)
  })
})

describe('点击与拖拽的判定', () => {
  it('位移小于 4px 算点击（松手要开/关抽屉）', () => {
    expect(isClick(0, 0)).toBe(true)
    expect(isClick(0, 3)).toBe(true)
    expect(isClick(2, 2)).toBe(true)
  })

  it('位移达到 4px 算拖拽（松手不许误触）', () => {
    expect(isClick(0, 4)).toBe(false)
    expect(isClick(10, 0)).toBe(false)
  })
})
