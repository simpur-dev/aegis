/**
 * MapPanel 拆成左右两块之后共用的纯函数（不碰任何状态，不认 Cesium）。
 *
 * 拆之前这些函数都在 `MapPanel.vue` 的 script 里；拆开时只搬家、不改规则：
 * 图层行的文案/计数口径与风险芯片的配色仍各只有一份实现。
 */

import type { LayerId, PlanLayer } from '@/components/map/entities'

export const LAYER_LABELS: Record<PlanLayer, string> = {
  stations: '监测站点',
  warnings: '预警落点',
  reach: '触达标记',
  hazards: '灾害分区面',
  clusters: '聚合计数',
}

/** 图层开关 + 要素计数（聚合层单独一栏，因为它不是一种数据而是画法）。 */
export function layerRowsOf(counts: Record<LayerId, number>) {
  return (['stations', 'warnings', 'reach', 'hazards'] as LayerId[]).map((layer) => ({
    layer,
    label: LAYER_LABELS[layer],
    count: counts[layer],
  }))
}

/** 筛选生效时把全量一起说出来：数字变小要能说清是"筛掉了"，不是"数据没了"。 */
export function countTextOf(
  layer: LayerId,
  counts: Record<LayerId, number>,
  countsTotal: Record<LayerId, number>,
  riskFilterLength: number,
): string {
  const shown = counts[layer]
  return riskFilterLength === 0 ? String(shown) : `${shown} / ${countsTotal[layer]}`
}

/**
 * 芯片的三段颜色都由 `riskLegend()` 那一个色值算出来（同一份 `RISK_COLORS`，不另立第二套调色板）。
 *
 * 用 JS 拼 rgba 而不是 CSS `color-mix()`：自定义属性会原样收下不认识的函数，
 * 那时整条 `border` 声明会在计算值阶段失效——不如在这儿算干净，任何浏览器都是同一个结果。
 */
export function chipStyle(hex: string): Record<string, string> {
  const rgb = /^#([\da-f]{2})([\da-f]{2})([\da-f]{2})$/i.exec(hex)?.slice(1)
  const [r, g, b] = (rgb ?? ['148', '163', '184']).map((pair) => Number.parseInt(pair, 16))
  return {
    '--chip': hex,
    '--chip-line': `rgba(${r}, ${g}, ${b}, 0.35)`,
    '--chip-wash': `rgba(${r}, ${g}, ${b}, 0.14)`,
    '--chip-halo': `rgba(${r}, ${g}, ${b}, 0.18)`,
  }
}
