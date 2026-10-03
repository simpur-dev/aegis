/**
 * 指标页的行换算：账本出口 → 表行 + 图数据。
 *
 * 单独抽一个纯函数模块，是为了让"单位"这件事只在这条线上被解决一次：
 * 页面模板里不再判断后缀、不在列头写死 `(ms)`、也不在两处各算一遍达标与否。
 *
 * 单位只认出口自己声明的 `unit`（`@/utils/metricUnits`），认不出就抛错。
 * 名字后缀在这里只用来做**一致性核对**：`ingest_end_to_end_seconds` 若声称自己是毫秒，
 * 说明账本与命名漂移了，这时必须炸而不是挑一个单位渲染——挑错的症状是"一行绿字读错 1000 倍"。
 */

import type { LatencyStats } from '@/api/types'
import { METRIC_SUFFIXES, unitOfMetric, type MetricUnit } from '@/utils/metricUnits'

export interface LatencyRow {
  name: string
  label: string
  unit: MetricUnit
  count: number
  p50: number
  p95: number
  max: number
  budget: number | null
  breaches: number | null
  /** P95 与阈值同为该指标的单位，可直接比；未设阈值时为 null（不是"达标"）。 */
  pass: boolean | null
}

export function latencyRows(
  metrics: Readonly<Record<string, LatencyStats>>,
  labels: Readonly<Record<string, string>> = {},
): LatencyRow[] {
  return Object.entries(metrics).map(([name, stats]) => {
    const unit = unitOfMetric(stats.unit)
    assertUnitMatchesName(name, unit)
    if (typeof stats.p95 !== 'number') {
      throw new Error(`时延账本 ${name} 缺少 p95 字段：出口形状变了，指标页不能凭旧字段名继续渲染`)
    }
    const budget = stats.budget ?? null
    return {
      name,
      label: labels[name] ?? name,
      unit,
      count: stats.count,
      p50: stats.p50,
      p95: stats.p95,
      max: stats.max,
      budget,
      breaches: stats.breaches ?? null,
      pass: budget === null ? null : stats.p95 <= budget,
    }
  })
}

/** 名字里写了后缀就必须与出口一致；没写后缀的（`collab_txn`）无可核对，跳过。 */
function assertUnitMatchesName(name: string, unit: MetricUnit): void {
  const suffix = METRIC_SUFFIXES.find((item) => name.endsWith(item))
  if (suffix === undefined) return
  const fromName = suffix === '_ms' ? 'ms' : 's'
  if (fromName !== unit) {
    throw new Error(`${name}：出口单位 ${unit} 与指标名后缀 ${fromName} 不一致，先修账本再渲染`)
  }
}
