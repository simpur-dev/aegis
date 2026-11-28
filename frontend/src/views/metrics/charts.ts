import { echarts, type ChartOption } from '@/components/echarts'
import { colorSlots, type ThemeMode } from '@/theme/tokens'
import { operatingClock } from '@/utils/clock'
import { formatMetricValue, toMillis } from '@/utils/metricUnits'
import type { LatencyRow } from './rows'

export type TrendSample = [at: number, p95Ms: number | null]
export const TREND_WINDOW_MS = 3_600_000

const gradient = (from: string, to: string) => new echarts.graphic.LinearGradient(
  0, 0, 0, 1, [{ offset: 0, color: from }, { offset: 1, color: to }],
)

export function latencyChart(rows: LatencyRow[], theme: ThemeMode, compact: boolean): ChartOption {
  const c = colorSlots[theme]
  const measurements = (key: 'p50' | 'p95') => rows.map((row) => {
    // 对数轴只接受正值。零值保留在提示和明细中。
    return row[key] > 0 ? toMillis(row.unit, row[key]) : null
  })
  const p50 = measurements('p50')
  const p95 = measurements('p95')
  const values = [...p50, ...p95].filter((value): value is number => value !== null)
  const budgeted = rows.filter((row) => row.budget !== null && row.budget > 0)
  // 单项阈值可以标在共用轴上，标签写明归属。多个不同口径的阈值随各行显示。
  const thresholdRow = budgeted.length === 1 ? budgeted[0] : undefined
  const threshold = thresholdRow ? toMillis(thresholdRow.unit, thresholdRow.budget!) : null
  const lower = 10 ** (Math.floor(Math.log10(Math.min(...values, threshold ?? Infinity))) - 1)
  const upper = 10 ** Math.ceil(Math.log10(Math.max(...values, threshold ?? 0) * 1.5))

  const bar = (key: 'p50' | 'p95', data: (number | null)[], from: string, to: string) => ({
    type: 'bar',
    name: key.toUpperCase(),
    data,
    barMaxWidth: 11,
    itemStyle: { color: gradient(to, from), borderRadius: 6, shadowBlur: theme === 'dark' ? 5 : 0, shadowColor: from },
    label: {
      show: !compact, position: 'right', color: c.text, fontSize: 12, distance: 7,
      formatter: ({ dataIndex }: { dataIndex: number }) => formatMetricValue(rows[dataIndex].unit, rows[dataIndex][key]),
    },
  })
  return {
    animation: false,
    tooltip: {
      trigger: 'axis',
      backgroundColor: c.elevated,
      borderColor: c.border,
      textStyle: { color: c.text },
      renderMode: 'richText',
      formatter: (params: Array<{ dataIndex: number }>) => {
        const row = rows[params[0]?.dataIndex]
        if (!row) return ''
        return [
          row.label,
          `P50  ${formatMetricValue(row.unit, row.p50)}`,
          `P95  ${formatMetricValue(row.unit, row.p95)}`,
          `阈值  ${formatMetricValue(row.unit, row.budget)}`,
        ].join('\n')
      },
      axisPointer: { type: 'shadow', shadowStyle: { color: c['brand-soft'] } },
    },
    grid: { left: compact ? 96 : 197, right: compact ? 14 : 82, top: threshold === null ? 8 : 30, bottom: 40 },
    yAxis: {
      type: 'category', inverse: true, data: rows.map((row) => row.label),
      axisLabel: { width: compact ? 83 : 184, overflow: 'truncate', color: c['text-secondary'], fontSize: compact ? 11 : 13 },
      axisLine: { show: false }, axisTick: { show: false },
    },
    xAxis: {
      type: 'log', logBase: 10, min: lower, max: upper, startValue: lower,
      name: '时延（ms，对数轴）', nameLocation: 'middle', nameGap: 29,
      nameTextStyle: { color: c['text-muted'], fontSize: 11 },
      axisLabel: { color: c['text-secondary'], fontSize: 11, hideOverlap: true },
      axisLine: { show: true, lineStyle: { color: c.border } },
      axisTick: { show: false },
      splitLine: { lineStyle: { color: c['border-subtle'], type: 'dashed' } },
    },
    series: [
      bar('p50', p50, c['chart-bar-p50-from'], c['chart-bar-p50-to']),
      {
        ...bar('p95', p95, c['chart-bar-p95-from'], c['chart-bar-p95-to']),
        markLine: threshold === null || !thresholdRow ? undefined : {
          symbol: 'none',
          lineStyle: { color: c['status-danger'], type: 'dashed', width: 1.5 },
          label: {
            show: !compact, position: 'start', color: c['status-danger'],
            backgroundColor: c.elevated, borderColor: c['status-danger'], borderWidth: 1, borderRadius: 4, padding: [3, 6],
            formatter: `阈值 ${formatMetricValue(thresholdRow.unit, thresholdRow.budget)}`,
          },
          data: [{ xAxis: threshold, name: `${thresholdRow.label}阈值` }],
        },
      },
    ],
  }
}

export function p95Trend(samples: TrendSample[], theme: ThemeMode): ChartOption {
  const c = colorSlots[theme]
  const clock = (at: number) => operatingClock(new Date(at).toISOString())
  const firstAt = samples[0]?.[0] ?? 0
  const lastAt = samples.at(-1)?.[0] ?? firstAt
  const showSeconds = lastAt - firstAt < 120_000
  const timePadding = lastAt - firstAt < 30_000 ? 15_000 : 0
  return {
    animation: false,
    tooltip: {
      trigger: 'axis', renderMode: 'richText', backgroundColor: c.elevated, borderColor: c.border,
      textStyle: { color: c.text, fontSize: 12 },
      formatter: (params: Array<{ value: TrendSample }>) => {
        const [at, value] = params[0]?.value ?? [0, null]
        return `${clock(at)}（UTC+8）\nP95  ${formatMetricValue('ms', value)}`
      },
    },
    grid: { left: 5, right: 8, top: 5, bottom: 4, containLabel: true },
    xAxis: {
      type: 'time', min: firstAt - timePadding, max: lastAt + timePadding, splitNumber: 3, minInterval: 5000,
      axisLabel: {
        hideOverlap: true, color: c['text-muted'], fontSize: 10,
        formatter: (at: number) => showSeconds ? clock(at) : clock(at).slice(0, 5),
      },
      axisLine: { lineStyle: { color: c.border } }, axisTick: { show: false },
      splitLine: { show: false },
    },
    yAxis: {
      type: 'value', min: 0, splitNumber: 2,
      axisLabel: { color: c['text-muted'], fontSize: 10, formatter: '{value} ms' },
      splitLine: { lineStyle: { color: c['border-subtle'], type: 'dashed' } },
    },
    series: [{
      type: 'line', data: samples, connectNulls: false, symbol: 'circle', symbolSize: 5,
      itemStyle: { color: c.brand, borderColor: c['overview-accent'], borderWidth: 1.5 },
      lineStyle: { color: c.brand, width: 2 },
      areaStyle: { color: gradient(c['brand-line'], c['brand-soft']) },
    }],
  }
}
