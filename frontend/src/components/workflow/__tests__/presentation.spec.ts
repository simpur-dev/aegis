import { describe, expect, it } from 'vitest'

import {
  NODE_GLYPHS,
  NODE_PRESENTATION,
  resolveNodePresentation,
  type NodeAccent,
  type NodeGlyph,
} from '@/components/workflow/presentation'
import { backendNodeTypes } from '@/testing/repoSource'
import { colorSlots } from '@/theme/tokens'
import { NODE_SPECS } from '@/utils/graph'

const ACCENTS: readonly NodeAccent[] = ['blue', 'violet', 'teal', 'amber', 'rose', 'slate']

describe('节点呈现层', () => {
  it('每个后端类型都有一行呈现：多一个少一个都算漂移', () => {
    expect(Object.keys(NODE_PRESENTATION).sort()).toEqual(backendNodeTypes().sort())
  })

  it('每个字形都有非空路径：图标是画布里唯一的类别表情，缺一条就是一块空白', () => {
    const missing: string[] = []
    for (const row of Object.values(NODE_PRESENTATION)) {
      const paths = NODE_GLYPHS[row.glyph]
      if (!paths || paths.length === 0) missing.push(`${row.glyph} 没有路径`)
      else if (paths.some((d) => d.trim() === '')) missing.push(`${row.glyph} 有空路径`)
    }
    if (NODE_GLYPHS.unknown.length === 0) missing.push('unknown 没有兜底路径')
    expect(missing, missing.join('\n')).toEqual([])
  })

  it('强调色只落在这六个令牌名上，且明暗两套都有值', () => {
    const bad: string[] = []
    for (const [type, row] of Object.entries(NODE_PRESENTATION)) {
      if (!ACCENTS.includes(row.accent)) bad.push(`${type}: 未登记的强调色 ${row.accent}`)
      const key = `editor-accent-${row.accent}`
      if (!(key in colorSlots.light) || !(key in colorSlots.dark)) bad.push(`${type}: 令牌 ${key} 在主题里缺失`)
    }
    expect(bad, bad.join('\n')).toEqual([])
  })

  it('未知类型降级成灰色问号而不是抛错：旧前端要画得出新节点的卡', () => {
    const resolved = resolveNodePresentation('future_type')
    expect(resolved).toEqual({ glyph: 'unknown', accent: 'slate', known: false })
  })

  it('已知类型标记 known 且不与镜像表脱节', () => {
    for (const type of Object.keys(NODE_SPECS) as Array<keyof typeof NODE_SPECS>) {
      const resolved = resolveNodePresentation(type)
      expect(resolved.known, type).toBe(true)
      expect(resolved.glyph, type).toBe(NODE_PRESENTATION[type].glyph)
    }
  })

  it('所有字形的路径集合自洽：没有只为某个类型写、却漏进 NODE_GLYPHS 的键', () => {
    const glyphs = new Set<NodeGlyph>(Object.keys(NODE_GLYPHS) as NodeGlyph[])
    const used = [...new Set(Object.values(NODE_PRESENTATION).map((row) => row.glyph)), 'unknown' as const]
    expect(used.filter((glyph) => !glyphs.has(glyph))).toEqual([])
  })
})
