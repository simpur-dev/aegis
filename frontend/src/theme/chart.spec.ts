/**
 * ECharts 主题派生层门禁（阶段 1 后半）。
 *
 * 画布这一面有两件事容易坏：
 * ① 派生层自己抄一份色值——令牌改了画布不改，又是一张皮；
 * ② 主题注册没生效（名字打错、init 漏传），画布静默退回 ECharts 内置浅色板，
 *    深色下就是"卡片已经翻黑、轴标还是 #666"——改造前量到的是 91.2% 的成笔像素对比不足 4.5:1。
 * ① 在这里钉死；② 交给真机那条画布像素判据（`e2e/ui-overlap.spec.ts` 的 `ECharts 画布换肤`），
 *    因为"注册上了没有"只有看画出来的像素才算数，读源码读不出来。
 */

import { describe, expect, it } from 'vitest'

import { chartTheme, chartThemeName } from './chart'
import { colorSlots, font, type ColorSlot, type ThemeMode } from './tokens'

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

const modes: ThemeMode[] = ['light', 'dark']

/** 把 theme 对象里所有值摊平成 `路径 → 值`，好在逐条断言 */
function flatten(value: unknown, prefix = ''): Record<string, unknown> {
  if (value === null || typeof value !== 'object') return { [prefix]: value }
  const out: Record<string, unknown> = {}
  for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
    const next = prefix ? `${prefix}.${k}` : k
    if (Array.isArray(v)) v.forEach((item, i) => Object.assign(out, flatten(item, `${next}[${i}]`)))
    else Object.assign(out, flatten(v, next))
  }
  return out
}

describe('chartTheme：画布用色只能来自令牌', () => {
  it('每一个色值都能在当主题的槽位表里找到（派生层不许自带一份色）', () => {
    for (const mode of modes) {
      const allowed = new Set<string>(Object.values(colorSlots[mode]))
      const leaked = Object.entries(flatten(chartTheme(mode)))
        .filter(([, v]) => typeof v === 'string' && /^#[0-9a-fA-F]{3,8}$/.test(v) && !allowed.has(v))
        .map(([k, v]) => `${k}=${String(v)}`)
      expect(leaked, `${mode} 主题里有令牌之外的色值：${leaked.join(', ')}`).toEqual([])
    }
  })

  it('两主题的键集合一模一样：深色少一个槽就是规则点名的"白底补丁"', () => {
    const light = Object.keys(flatten(chartTheme('light'))).sort()
    const dark = Object.keys(flatten(chartTheme('dark'))).sort()
    expect(light.length, '展平后一个键都没拿到，判据在空跑').toBeGreaterThan(30)
    expect(dark).toEqual(light)
  })

  it('series 调色板第一支是主色，且不含 ECharts 内置草绿 #91cc75', () => {
    for (const mode of modes) {
      const palette = chartTheme(mode).color as string[]
      expect(palette.length, '调色板是空的——init 时会退回内置色板').toBeGreaterThanOrEqual(4)
      expect(palette[0]).toBe(colorSlots[mode].brand)
      expect(palette.map((p) => p.toLowerCase()), `${mode} 混进了内置色板`).not.toContain('#91cc75')
    }
  })

  it('画布上的文字（轴标/图例/标题/tooltip）对所在表面都过 AA 4.5:1', () => {
    const cases: Array<{ label: string; pick: (t: Record<string, unknown>) => string; bg: ColorSlot; mode: ThemeMode }> = []
    for (const mode of modes) {
      const base = {
        '轴标签': (t: Record<string, unknown>) => ((t.categoryAxis as Record<string, unknown>).axisLabel as Record<string, string>).color,
        '图例文字': (t: Record<string, unknown>) => ((t.legend as Record<string, unknown>).textStyle as Record<string, string>).color,
        '标题': (t: Record<string, unknown>) => ((t.title as Record<string, unknown>).textStyle as Record<string, string>).color,
        'tooltip 文字': (t: Record<string, unknown>) => ((t.tooltip as Record<string, unknown>).textStyle as Record<string, string>).color,
        '正文默认色': (t: Record<string, unknown>) => (t.textStyle as Record<string, string>).color,
      }
      for (const [label, pick] of Object.entries(base)) {
        cases.push({ label, pick, bg: label === 'tooltip 文字' ? 'elevated' : 'card', mode })
      }
    }
    for (const { label, pick, bg, mode } of cases) {
      const fg = pick(chartTheme(mode))
      const r = ratio(fg, colorSlots[mode][bg])
      expect(r, `${mode}/${label}：${fg} 落在 ${bg}(${colorSlots[mode][bg]}) 上只有 ${r.toFixed(2)}:1`).toBeGreaterThanOrEqual(4.5)
    }
  })

  it('字号取自令牌阶，不许出现阶外的数', () => {
    const sizes = Object.values(font.size) as number[]
    for (const mode of modes) {
      const bad = Object.entries(flatten(chartTheme(mode)))
        .filter(([k, v]) => k.endsWith('.fontSize') && typeof v === 'number' && !sizes.includes(v))
        .map(([k, v]) => `${k}=${String(v)}`)
      expect(bad, `${mode} 用了阶外字号：${bad.join(', ')}（可用的就 ${sizes.join('/')}）`).toEqual([])
    }
  })

  it('背景保持透明：填实色就等于在卡片之下再造一块底，翻转主题时最先漏馅', () => {
    for (const mode of modes) expect(chartTheme(mode).backgroundColor).toBe('transparent')
  })

  it('主题名两档不同且带 aegis- 前缀（防"注册了却 init 传错名字"）', () => {
    expect(chartThemeName.light).toBe('aegis-light')
    expect(chartThemeName.dark).toBe('aegis-dark')
    expect(chartThemeName.light).not.toBe(chartThemeName.dark)
  })
})
