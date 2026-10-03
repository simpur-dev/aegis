/**
 * 指标单位的唯一判定处（零依赖，可离线单测）。
 *
 * 单位由**账本自己声明**：后端 `LatencyStats.as_dict()` 的键名是中性的（`p50 / p95 / budget`），
 * 到底是毫秒还是秒只看随行的 `unit`。这里只认 `unit`，认不出就抛错——绝不按埋点名猜单位。
 *
 * 这条边界的存在理由很具体：后端曾经的键名统一是 `p50_ms / budget_ms`，而
 * `ingest_end_to_end_seconds`、`report_intake_seconds` 记的是**秒**，于是"0.01 / 300"这两个数
 * 的真实含义（0.01 秒、300 秒）被键名说成了毫秒，差 1000 倍。判定本身不会变红（两侧同为秒才可比），
 * 骗人的只有键名——所以宁可在这里硬失败。
 */

export type MetricUnit = 'ms' | 's'

export const METRIC_UNITS: readonly MetricUnit[] = ['ms', 's']

/** 埋点名的合法后缀：跨端门禁拿它去核对后端 instrumentation.py 里的指标名。 */
export const METRIC_SUFFIXES: readonly string[] = ['_ms', '_seconds']

/** 只认账本自己给出的单位；缺失或未知一律抛错，不猜。 */
export function unitOfMetric(raw: string | undefined): MetricUnit {
  if (raw === 'ms' || raw === 's') return raw
  throw new Error(`时延账本未声明单位或单位未知：${String(raw)}（期望 ms 或 s）`)
}

/** 把某个单位的值换算成毫秒：图表共用一根轴时才需要，别拿它改表格里显示的数字。 */
export function toMillis(unit: MetricUnit, value: number): number {
  return unit === 's' ? value * 1000 : value
}

/** 表格单元格的显示形态：数字与单位不分离，避免"值在 ms 列、单位在标签里"再次分家。 */
export function formatMetricValue(unit: MetricUnit, value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return `${value} ${unit}`
}
