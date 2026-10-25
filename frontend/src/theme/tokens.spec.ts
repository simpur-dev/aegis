/**
 * 令牌层门禁（阶段 0 的"测试"那一半）。
 *
 * 这里不测外观，测的是**令牌本身合不合格**：4px 网格、字号阶、两主题槽位齐不平齐、
 * 以及最硬的一条——**文字槽位对它所在表面的 WCAG 对比度**。
 * 对比度是这里现算的，不是谁手写的：规则要求"两主题都清晰可辨"，
 * 手抄一个"约 5:1"等于没验（这类"结论由账面模型成立"的坑项目里吃过）。
 */

import { describe, expect, it } from 'vitest'

import { antdTheme } from './antd'
import { colorSlots, font, radius, riskFill, riskText, space, type ColorSlot } from './tokens'
import { applyTheme, themeVars } from './vars'

type RGB = { r: number; g: number; b: number }

function hex(value: string): RGB {
  const m = /^#([0-9a-f]{6})$/i.exec(value.trim())
  if (!m) throw new Error(`令牌里出现了非 #rrggbb 的颜色：${value}`)
  const n = parseInt(m[1], 16)
  return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 }
}

function lum(c: RGB): number {
  const f = (v: number) => {
    const s = v / 255
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4
  }
  return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b)
}

function ratio(a: string, b: string): number {
  const l1 = lum(hex(a))
  const l2 = lum(hex(b))
  return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05)
}

/** 每个主题里"文字该落在哪些表面上"——不写这张表就不知道拿谁比谁 */
const textPairs: Record<'light' | 'dark', Array<{ fg: ColorSlot; bg: ColorSlot; size: 'body' | 'large' }>> = {
  light: [
    { fg: 'text', bg: 'card', size: 'body' },
    { fg: 'text', bg: 'page', size: 'body' },
    { fg: 'text-secondary', bg: 'card', size: 'body' },
    { fg: 'text-secondary', bg: 'page', size: 'body' },
    { fg: 'text-muted', bg: 'card', size: 'body' },
    { fg: 'text-muted', bg: 'alter', size: 'body' },
    { fg: 'text-inverse', bg: 'brand', size: 'body' },
    { fg: 'brand', bg: 'card', size: 'body' },
    { fg: 'status-success', bg: 'card', size: 'body' },
    { fg: 'status-warning', bg: 'card', size: 'body' },
    { fg: 'status-danger', bg: 'card', size: 'body' },
    { fg: 'status-info', bg: 'card', size: 'body' },
  ],
  dark: [
    { fg: 'text', bg: 'card', size: 'body' },
    { fg: 'text', bg: 'page', size: 'body' },
    { fg: 'text-secondary', bg: 'card', size: 'body' },
    { fg: 'text-secondary', bg: 'elevated', size: 'body' },
    { fg: 'text-muted', bg: 'card', size: 'body' },
    { fg: 'text-muted', bg: 'alter', size: 'body' },
    { fg: 'text-inverse', bg: 'brand', size: 'body' },
    { fg: 'brand', bg: 'card', size: 'body' },
    { fg: 'status-success', bg: 'card', size: 'body' },
    { fg: 'status-warning', bg: 'card', size: 'body' },
    { fg: 'status-danger', bg: 'card', size: 'body' },
    { fg: 'status-info', bg: 'card', size: 'body' },
  ],
}

describe('设计令牌真源', () => {
  it('间距全部是 4px 的倍数（DoD 的"4px 网格"）', () => {
    for (const [k, v] of Object.entries(space)) expect(v % 4, `space.${k}=${v} 不在 4px 网格上`).toBe(0)
  })

  it('字号阶递增且都是整像素', () => {
    const sizes = Object.entries(font.size).sort((a, b) => a[1] - b[1])
    for (const [k, v] of sizes) {
      expect(Number.isInteger(v), `font.size.${k}=${v} 不是整像素`).toBe(true)
      expect(v, `font.size.${k}=${v} 小于 11px，现场读不清`).toBeGreaterThanOrEqual(11)
    }
    expect(sizes.length).toBeGreaterThanOrEqual(6)
  })

  it('圆角只有阶上的五档', () => {
    expect(Object.values(radius).sort((a, b) => a - b)).toEqual([6, 10, 16, 22, 999])
  })

  it('两主题的语义槽位同名同集合——少一个键，切深色就是一块白底补丁', () => {
    const light = Object.keys(colorSlots.light).sort()
    const dark = Object.keys(colorSlots.dark).sort()
    expect(dark, '深浅两主题的槽位对不上').toEqual(light)
    expect(light.length, '槽位少到可疑（至少该有表面/边框/文字/品牌/状态这几组）').toBeGreaterThanOrEqual(20)
  })

  it('文字槽位落在自己的表面上都过 AA（两主题各算一遍）', () => {
    for (const mode of ['light', 'dark'] as const) {
      for (const { fg, bg, size } of textPairs[mode]) {
        const need = size === 'large' ? 3 : 4.5
        const r = ratio(colorSlots[mode][fg], colorSlots[mode][bg])
        expect(r, `${mode} 主题：${fg}(${colorSlots[mode][fg]}) 落在 ${bg}(${colorSlots[mode][bg]}) 上只有 ${r.toFixed(2)}:1，要 ${need}:1`).toBeGreaterThanOrEqual(need)
      }
    }
  })

  it('风险等级文字色落在两主题表面上都过 AA，且五档互不相同', () => {
    for (const mode of ['light', 'dark'] as const) {
      const surface = colorSlots[mode].card
      for (const level of [1, 2, 3, 4, 5] as const) {
        const r = ratio(riskText[mode][level], surface)
        expect(r, `${mode}：等级 ${level} 的文字色 ${riskText[mode][level]} 落在 ${surface} 上只有 ${r.toFixed(2)}:1`).toBeGreaterThanOrEqual(4.5)
      }
      const fills = Object.values(riskFill[mode])
      expect(new Set(fills).size, `${mode}：有五档风险色却只填出 ${new Set(fills).size} 种颜色`).toBe(5)
    }
    /* 相邻两档不能近到"看着一个色"：RGB 欧氏距离给个下限（黄/橙这类同族色靠文字标签区分，见门禁第 9 条） */
    for (const mode of ['light', 'dark'] as const) {
      const levels = [1, 2, 3, 4, 5] as const
      for (const a of levels) {
        for (const b of levels) {
          if (a >= b) continue
          const x = hex(riskFill[mode][a])
          const y = hex(riskFill[mode][b])
          const d = Math.sqrt((x.r - y.r) ** 2 + (x.g - y.g) ** 2 + (x.b - y.b) ** 2)
          expect(d, `${mode}：等级 ${a} 与 ${b} 的底色太接近（距离 ${d.toFixed(0)}）`).toBeGreaterThan(40)
        }
      }
    }
  })

  it('antd 主题由令牌派生：浅色逐项等于改造前的字面值（这一步不掺视觉变化）', () => {
    const token = antdTheme('light').token as Record<string, unknown>
    expect(token.colorPrimary).toBe('#2563EB')
    expect(token.colorInfo).toBe('#2563EB')
    expect(token.colorLink).toBe('#2563EB')
    expect(token.colorPrimaryHover).toBe('#1D4ED8')
    expect(token.colorPrimaryActive).toBe('#1E40AF')
    expect(token.colorBgLayout).toBe('#F2F4F8')
    expect(token.colorBgContainer).toBe('#FFFFFF')
    expect(token.colorFillAlter).toBe('#F7F9FC')
    expect(token.colorBorder).toBe('#E6E8EF')
    expect(token.colorBorderSecondary).toBe('#EEF0F5')
    expect(token.colorText).toBe('#0A0A0C')
    expect(token.colorTextDescription).toBe('#5A6072')
    expect(token.borderRadius).toBe(10)
    expect(token.borderRadiusLG).toBe(16)
    expect(token.motionDurationMid).toBe('0.15s')
    expect(String(token.fontFamily)).toContain('Space Grotesk')
    expect(String(token.fontFamily)).not.toMatch(/https?:|fonts\.googleapis/)
  })

  it('深色派生确实换了算法与底色（不是把浅色对象复制一遍）', () => {
    const dark = antdTheme('dark')
    const light = antdTheme('light')
    expect(dark.algorithm).toBeDefined()
    expect(dark.algorithm).not.toEqual(light.algorithm)
    const token = dark.token as Record<string, unknown>
    expect(token.colorBgContainer).toBe('#111A2B')
    expect(token.colorText).toBe('#E6EDF7')
    expect(hex(String(token.colorBgContainer)).r, '深色容器底色还是亮的').toBeLessThan(40)
  })

  it('CSS 变量：两主题键集合一致，applyTheme 真的写进 documentElement', () => {
    const light = themeVars('light')
    const dark = themeVars('dark')
    expect(Object.keys(dark).sort()).toEqual(Object.keys(light).sort())
    expect(Object.keys(light).every((k) => k.startsWith('--aeg-')), '有变量没走 --aeg- 前缀').toBe(true)
    applyTheme('dark')
    const root = document.documentElement
    expect(root.dataset.theme).toBe('dark')
    expect(root.style.getPropertyValue('--aeg-color-card').trim()).toBe('#111A2B')
    expect(root.style.getPropertyValue('--aeg-space-s4')).toBe('16px')
    applyTheme('light')
    expect(root.style.getPropertyValue('--aeg-color-card').trim()).toBe('#FFFFFF')
  })

  it('动作按钮与「成/败」徽标：文字落在自己的底上都过 AA（两主题各算一遍）', () => {
    /** 徽标底是半透明的：先按卡片底色合成一次，再算对比度（直接拿 rgba 比是没意义的） */
    function over(baseHex: string, top: string): string {
      const m = /^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)$/i.exec(top.trim())
      if (!m) return top
      const alpha = m[4] === undefined ? 1 : Number(m[4])
      const base = hex(baseHex)
      const blend = (topChannel: number, baseChannel: number) => Math.round(topChannel * alpha + baseChannel * (1 - alpha))
      const channels = [blend(Number(m[1]), base.r), blend(Number(m[2]), base.g), blend(Number(m[3]), base.b)]
      return `#${channels.map((v) => v.toString(16).padStart(2, '0')).join('')}`
    }

    for (const mode of ['light', 'dark'] as const) {
      const slots = colorSlots[mode]
      expect(ratio(slots['action-text'], slots['action-bg']), `${mode} 主题：动作按钮文字读不清`).toBeGreaterThanOrEqual(4.5)
      expect(ratio(slots['action-text'], slots['action-bg-hover']), `${mode} 主题：hover 态文字读不清`).toBeGreaterThanOrEqual(4.5)
      expect(ratio(slots['action-text'], slots['action-bg-active']), `${mode} 主题：active 态文字读不清`).toBeGreaterThanOrEqual(4.5)
      const chip = over(slots.card, slots['stat-chip-bg'])
      expect(ratio(slots['stat-chip-text'], chip), `${mode} 主题：成/败徽标文字读不清（合成底 ${chip}）`).toBeGreaterThanOrEqual(4.5)
    }
  })
})
