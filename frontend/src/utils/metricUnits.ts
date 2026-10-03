/**
 * 指标单位的唯一判定处（零依赖，可离线单测）。
 *
 * 后端时延账本里字段名统一是 `p50_ms / p95_ms / max_ms / budget_ms`——那是"值"的中性槽位名，
 * 不是单位：以 `_seconds` 结尾的那几条埋点记的是**秒**（`ingest_end_to_end_seconds`、
 * `report_intake_seconds`）。指标页此前把表头一律写成 `(ms)`，于是"数据接入端到端时延
 * P50 0.01、阈值 300"这行真实含义是 0.01 秒与 300 秒，被读成毫秒就差 1000 倍。
 * 判定本身没错（两侧同为秒才可比），骗人的只有标签——这种缺陷不会让任何东西变红，
 * 只会让人对着一行绿字得出错的结论。
 */

export type MetricUnit = 'ms' | 's'

export function metricUnit(name: string): MetricUnit {
  return name.endsWith('_seconds') ? 's' : 'ms'
}

/** 把任一埋点的值换算成毫秒：图表共用一根轴时才需要，别拿它改表格里显示的数字。 */
export function toMillis(name: string, value: number): number {
  return metricUnit(name) === 's' ? value * 1000 : value
}

/** 表格单元格的显示形态：数字与单位不分离，避免"值在 ms 列、单位在标签里"再次分家。 */
export function formatMetricValue(name: string, value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return `${value} ${metricUnit(name)}`
}
