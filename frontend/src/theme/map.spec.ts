/**
 * `theme/map.ts` 的自检（阶段 1 后半 · Cesium 那一面）。
 *
 * 钉的是四件事，都是这轮改造真会出错的地方：
 * ① 派生层不许自带色值（每个字段都能在原子里找到同一支）；
 * ② 两主题的字段集合必须一模一样——少一个键就是"翻了一半"的白底补丁；
 * ③ 标签字色对标签底板实算 WCAG AA（画布上的字也受这条约束，不是只有 DOM 才算）；
 * ④ 风险档映射：深色档把浅色档的五支基色逐档换过去，认不出的（等级未知灰、通道红蓝）原样透出，
 *    绝不允许"映射不到就变透明/变黑"。
 */

import { describe, expect, it } from 'vitest'

import { RISK_COLORS, type RiskLevel } from '@/api/types'

import { colorSlots, riskFill } from './tokens'
import { amapStyleName, mapPalette } from './map'

function channel(v: number): number {
  const c = v / 255
  return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
}

function luminance(css: string): number {
  const hex = css.replace('#', '')
  const [r, g, b] = [hex.slice(0, 2), hex.slice(2, 4), hex.slice(4, 6)].map((part) => Number.parseInt(part, 16))
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)
}

function contrast(a: string, b: string): number {
  const [hi, lo] = luminance(a) > luminance(b) ? [a, b] : [b, a]
  return (luminance(hi) + 0.05) / (luminance(lo) + 0.05)
}

const MODES = ['light', 'dark'] as const

describe('theme/map —— Cesium 场景调色板派生层', () => {
  it('每个色值字段都等于某一支令牌槽位（派生层不许自造色）', () => {
    for (const mode of MODES) {
      const p = mapPalette(mode)
      const slots = Object.values(colorSlots[mode]) as string[]
      for (const field of ['globeBase', 'spaceBackdrop', 'labelText', 'labelHalo', 'labelBackground', 'markerOutline'] as const) {
        expect(slots, `${mode}.${field}=${p[field]} 不在令牌槽位里`).toContain(p[field])
      }
    }
  })

  it('两主题的字段集合一模一样（少一个键就是翻了一半的画面）', () => {
    expect(Object.keys(mapPalette('light')).sort()).toEqual(Object.keys(mapPalette('dark')).sort())
  })

  it('标签字色对标签底板过 AA，且光晕与字色反向（同色等于没有光晕）', () => {
    for (const mode of MODES) {
      const p = mapPalette(mode)
      expect(contrast(p.labelText, p.labelBackground), `${mode} 标签字压在本档底板上不到 AA`).toBeGreaterThanOrEqual(4.5)
      expect(contrast(p.labelText, p.labelHalo), `${mode} 光晕与字色同档——压在影像上就没有描边了`).toBeGreaterThanOrEqual(4.5)
    }
  })

  it('地球底色与画面外底色随主题翻面：浅档是亮面，深档是暗面', () => {
    const light = mapPalette('light')
    const dark = mapPalette('dark')
    expect(luminance(light.globeBase)).toBeGreaterThan(0.5)
    expect(luminance(dark.globeBase)).toBeLessThan(0.1)
    expect(light.globeBase).not.toBe(dark.globeBase)
    expect(light.spaceBackdrop).not.toBe(dark.spaceBackdrop)
    /* 深色档的画面外底色不许比地球底更亮，否则等于在暗画布上开一个亮洞。 */
    expect(luminance(dark.spaceBackdrop)).toBeLessThanOrEqual(luminance(dark.globeBase))
  })

  it('风险档映射：深色档逐档换到 riskFill.dark，浅色档一律原样透出', () => {
    const dark = mapPalette('dark')
    const levels = Object.keys(RISK_COLORS) as unknown as RiskLevel[]
    expect(levels).toHaveLength(5)
    for (const level of levels) {
      expect(dark.fillFor(RISK_COLORS[level]), `等级 ${level} 没映射到深色档`).toBe(riskFill.dark[level])
    }
    for (const level of levels) {
      expect(mapPalette('light').fillFor(RISK_COLORS[level]), `浅色档不该动等级 ${level}`).toBe(RISK_COLORS[level])
    }
  })

  it('映射不到的颜色原样透出（等级未知灰与"未知状态"深灰不是风险五档）', () => {
    const dark = mapPalette('dark')
    for (const css of ['#bfbfbf', '#434343', '#000000']) {
      expect(dark.fillFor(css), `${css} 不该被改`).toBe(css)
    }
  })

  it('同一支风险色不管大小写都映射到同一档（上游 RISK_COLORS 写的是小写，令牌里是大写）', () => {
    const dark = mapPalette('dark')
    for (const level of [1, 4] as RiskLevel[]) {
      const upstream = RISK_COLORS[level]
      expect(dark.fillFor(upstream), `${upstream}（上游原样）没映射`).toBe(riskFill.dark[level])
      expect(dark.fillFor(upstream.toUpperCase()), '大写写法没映射').toBe(riskFill.dark[level])
    }
    /* 通道色与风险档同值（劣化=等级1红、正常=等级4蓝），所以它跟着换档是一致的，不是漏网。 */
    expect(dark.fillFor('#cf1322')).toBe(riskFill.dark[1])
    expect(dark.fillFor('#1677ff')).toBe(riskFill.dark[4])
  })

  it('高德样式名两档不同、都来自官方样式表，且浅色就是官方默认（组件不许自挑档位）', () => {
    expect(amapStyleName('light')).toBe('amap://styles/normal')
    expect(amapStyleName('dark')).toBe('amap://styles/grey')
    expect(amapStyleName('light')).not.toBe(amapStyleName('dark'))
  })
})
