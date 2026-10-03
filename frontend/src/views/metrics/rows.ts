/**
 * 指标页的行换算：账本出口 → 表行 + 图数据。
 *
 * 抽成纯函数模块，是为了让"单位"这件事只在这条线上被解决一次：模板里不判后缀、
 * 列头不写死 (ms)、达标与否不在两处各算一遍。
 *
 * 单位只认出口自己声明的 `unit`。指标名后缀在这里只用于**一致性核对**：
 * `ingest_end_to_end_seconds` 若声称自己是毫秒，说明账本与命名漂了——这时必须炸，
 * 而不是挑一个单位渲染。挑错的症状是"一行绿字读错 1000 倍"，没有任何东西会红。
 */

import type { LatencyStats } from '@/api/types'
import { unitFromMetricName, unitOfMetric, type MetricUnit } from '@/utils/metricUnits'

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

const QUANTILES = ['p50', 'p95', 'max'] as const

export function latencyRows(
  metrics: Readonly<Record<string, LatencyStats>>,
  labels: Readonly<Record<string, string>> = {},
): LatencyRow[] {
  return Object.entries(metrics).map(([name, stats]) => {
    const unit = unitOfMetric(stats.unit)
    const claimed = unitFromMetricName(name)
    if (claimed !== null && claimed !== unit) {
      throw new Error(`${name}：出口单位 ${unit} 与指标名后缀 ${claimed} 不一致，先修账本再渲染`)
    }
    for (const key of QUANTILES) {
      if (typeof stats[key] !== 'number') {
        throw new Error(`时延账本 ${name} 缺少 ${key}：出口形状变了，指标页不能凭旧键名继续渲染`)
      }
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
