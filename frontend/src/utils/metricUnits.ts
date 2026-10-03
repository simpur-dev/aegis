/**
 * 指标单位的唯一判定处（零依赖，可离线单测）。
 *
 * 单位由**账本自己声明**：后端 `LatencyStats.as_dict()` 的键名是中性的（`p50 / p95 / budget`），
 * 毫秒还是秒只看随行的 `unit`。这里只认 `unit`，认不出就抛错——绝不按埋点名猜。
 *
 * 理由很具体：后端曾经的键名统一是 `p50_ms / budget_ms`，而 `ingest_end_to_end_seconds`、
 * `report_intake_seconds` 记的是**秒**，于是"0.019"配着 `_ms` 的键名出去，按键名读的一方
 * 差 1000 倍（Prometheus 的 `aegis_latency_ms` 直方图当时就在这么读）。判定不会变红
 * （两侧同为秒才可比），骗人的只有键名——所以宁可在这里硬失败。
 */

export type MetricUnit = 'ms' | 's'

export const METRIC_UNITS: readonly MetricUnit[] = ['ms', 's']

/** 埋点名里允许出现的单位后缀；跨端门禁拿它核对后端 instrumentation.py 的指标名。 */
export const METRIC_SUFFIXES: readonly string[] = ['_ms', '_seconds']

/** 只认账本自己给出的单位；缺失或未知一律抛错，不猜。 */
export function unitOfMetric(raw: string | undefined): MetricUnit {
  if (raw === 'ms' || raw === 's') return raw
  throw new Error(`时延账本未声明单位或单位未知：${String(raw)}（只支持 ms / s）`)
}

/**
 * 名字后缀声称的单位：仅用于与出口 `unit` 做一致性核对，不参与渲染取值。
 * 无后缀的历史名字（`collab_txn`）返回 null——它没有可核对的声明。
 */
export function unitFromMetricName(name: string): MetricUnit | null {
  const suffix = METRIC_SUFFIXES.find((item) => name.endsWith(item))
  if (suffix === undefined) return null
  return suffix === '_ms' ? 'ms' : 's'
}

/** 换算成毫秒：共用一根毫秒轴画图时才需要，别拿它改表格里显示的数字。 */
export function toMillis(unit: MetricUnit, value: number): number {
  return unit === 's' ? value * 1000 : value
}

/** 表格单元格的显示形态：数字与单位不分离，避免"值在一列、单位在列头"再次分家。 */
export function formatMetricValue(unit: MetricUnit, value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return `${value} ${unit}`
}
