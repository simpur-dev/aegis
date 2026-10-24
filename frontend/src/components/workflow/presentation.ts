/**
 * 节点呈现层：类型 → 图标字形 + 强调色。
 *
 * 为什么单列一份：`NodeIcon`（块选择器/画布）与 `NodeShell`（卡片边框）要的是"某一类长什么样"，
 * 而 `registry.ts` 管的是"某一类填什么"。两类信息变化频率不同——换图标不该碰表单，
 * 加字段也不该动配色。`Record<NodeType, …>` 的穷尽性由 TS 保证：后端新增类型时这里编译即失败。
 *
 * 未知类型不抛错：后端先行扩展时，画布上常见的是"旧前端打开新定义"。
 * 这类节点必须画得出来（否则整张图丢节点），所以统一落到中性字形与灰色强调，
 * 由 `known=false` 让卡片/检查器降级成只读。
 */
import type { NodeType } from '@/utils/graph'

export type NodeGlyph =
  | 'start'
  | 'end'
  | 'fetch'
  | 'http'
  | 'device'
  | 'notify'
  | 'threshold'
  | 'branch'
  | 'join'
  | 'delay'
  | 'feedback'
  | 'stage'
  | 'hazard'
  | 'risk'
  | 'simulate'
  | 'warning'
  | 'publish'
  | 'degrade'
  | 'human'
  | 'unknown'

/** 强调色只给语义名，具体值在 `theme/tokens.ts` 里按明暗两套落地。 */
export type NodeAccent = 'blue' | 'violet' | 'teal' | 'amber' | 'rose' | 'slate'

export interface NodePresentation {
  glyph: NodeGlyph
  accent: NodeAccent
}

export interface ResolvedPresentation extends NodePresentation {
  /** 类型是否在当前前端注册表内；false 时调用方必须降级（只读、不给配置表单）。 */
  known: boolean
}

export const NODE_PRESENTATION: Record<NodeType, NodePresentation> = {
  api_call: { glyph: 'http', accent: 'violet' },
  branch: { glyph: 'branch', accent: 'violet' },
  chain_stage: { glyph: 'stage', accent: 'slate' },
  data_fetch: { glyph: 'fetch', accent: 'teal' },
  delay: { glyph: 'delay', accent: 'slate' },
  degrade_to_rule: { glyph: 'degrade', accent: 'rose' },
  device_control: { glyph: 'device', accent: 'amber' },
  end: { glyph: 'end', accent: 'slate' },
  feedback_collect: { glyph: 'feedback', accent: 'teal' },
  hazard_identify: { glyph: 'hazard', accent: 'rose' },
  human_review: { glyph: 'human', accent: 'violet' },
  join: { glyph: 'join', accent: 'blue' },
  notify: { glyph: 'notify', accent: 'blue' },
  risk_assess: { glyph: 'risk', accent: 'amber' },
  situation_simulate: { glyph: 'simulate', accent: 'teal' },
  start: { glyph: 'start', accent: 'blue' },
  threshold: { glyph: 'threshold', accent: 'violet' },
  warning_generate: { glyph: 'warning', accent: 'amber' },
  warning_publish: { glyph: 'publish', accent: 'rose' },
}

const UNKNOWN_PRESENTATION: NodePresentation = { glyph: 'unknown', accent: 'slate' }

export function resolveNodePresentation(type: string): ResolvedPresentation {
  const row = (NODE_PRESENTATION as Record<string, NodePresentation | undefined>)[type]
  return row === undefined ? { ...UNKNOWN_PRESENTATION, known: false } : { ...row, known: true }
}

/** 字形对应的 24×24 描边路径；`NodeIcon` 只负责渲染，不在这里再写一套 v-if。 */
export const NODE_GLYPHS: Record<NodeGlyph, readonly string[]> = {
  start: ['M8 5.5v13L19 12 8 5.5Z'],
  end: ['M7 7h10v10H7z'],
  fetch: [
    'M5 6c0-1.7 3.1-3 7-3s7 1.3 7 3-3.1 3-7 3-7-1.3-7-3Z',
    'M5 6v6c0 1.7 3.1 3 7 3s7-1.3 7-3V6',
    'M5 12v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6',
  ],
  http: [
    'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Z',
    'M3 12h18',
    'M12 3c2.5 2.7 3.8 5.7 3.8 9S14.5 18.3 12 21c-2.5-2.7-3.8-5.7-3.8-9S9.5 5.7 12 3Z',
  ],
  device: [
    'M12 13a2 2 0 1 0 0-4 2 2 0 0 0 0 4Z',
    'M7.8 7.8a6 6 0 0 0 0 8.4',
    'M16.2 7.8a6 6 0 0 1 0 8.4',
    'M5 5a10 10 0 0 0 0 14',
    'M19 5a10 10 0 0 1 0 14',
  ],
  notify: ['M18 9a6 6 0 1 0-12 0c0 5-2 6-2 6h16s-2-1-2-6Z', 'M10 20a2.2 2.2 0 0 0 4 0'],
  threshold: [
    'M4 7h10',
    'M18 7h2',
    'M4 17h4',
    'M12 17h8',
    'M16 5a2 2 0 1 0 0 4 2 2 0 0 0 0-4Z',
    'M10 15a2 2 0 1 0 0 4 2 2 0 0 0 0-4Z',
  ],
  branch: ['M4 12h5', 'M9 12c3 0 4-5 7-5', 'M9 12c3 0 4 5 7 5', 'M14 5l2 2-2 2', 'M14 19l2-2-2-2'],
  join: ['M20 12h-5', 'M15 12c-3 0-4-5-7-5', 'M15 12c-3 0-4 5-7 5', 'M8 5 6 7l2 2', 'M8 19l-2-2 2-2'],
  delay: ['M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Z', 'M12 7v5l3 2'],
  feedback: ['M20 11a8 8 0 1 0-2.3 6.3', 'M20 5v6h-6'],
  stage: ['M12 4l9 5-9 5-9-5 9-5Z', 'M3 13l9 5 9-5'],
  hazard: ['M12 4l10 16H2L12 4Z', 'M12 9v5', 'M12 17h.01'],
  risk: ['M5 20v-8', 'M12 20V5', 'M19 20v-5', 'M3 20h18'],
  simulate: ['M3 17l5-5 3 3 7-7', 'M16 8h4v4'],
  warning: ['M4 10v4h3l11 5V5L7 10H4Z', 'M18 9a3 3 0 0 1 0 6'],
  publish: ['M21 3 3 10.5l7 2.5 2.5 7L21 3Z', 'M10 13l4-4'],
  degrade: ['M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6l8-3Z', 'M12 8v5', 'M9.5 11.5 12 14l2.5-2.5'],
  human: ['M12 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Z', 'M5 20c.8-3.6 3.6-5.5 7-5.5s6.2 1.9 7 5.5'],
  unknown: ['M9.5 9a2.5 2.5 0 1 1 3.6 2.2c-.7.4-1.1 1-1.1 1.8v.5', 'M12 17h.01'],
}
