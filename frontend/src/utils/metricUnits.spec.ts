/**
 * 单位判定与账本出口的跨端对账。
 *
 * 症状很安静：一条秒制埋点被读成毫秒，数值差 1000 倍，而判定列照样是绿的。
 * 所以这里既算清换算，也直接读后端源文件核对出口形状——后端改了写法就必须在这里响，
 * 而不是等某个人对着页面把一个数读错三个数量级。
 */

import { describe, expect, it } from 'vitest'

import { readRepoFile } from '@/testing/repoSource'
import {
  formatMetricValue,
  METRIC_SUFFIXES,
  METRIC_UNITS,
  toMillis,
  unitFromMetricName,
  unitOfMetric,
} from './metricUnits'

/** 后端 as_dict 的函数体（跳过它那段讲历史的 docstring，否则历史键名会被当成现状）。 */
function backendAsDict(): string {
  const text = readRepoFile('backend', 'src', 'aegis', 'observability', 'tracer.py')
  const start = text.indexOf('def as_dict')
  if (start < 0) throw new Error('tracer.py 里找不到 as_dict：出口写法变了，这里的解析要跟着改（不是把断言删掉）')
  const end = text.indexOf('def by_trace', start)
  const body = text.slice(start, end === -1 ? text.length : end)
  const docstringEnd = body.indexOf('"""', body.indexOf('"""') + 3)
  if (docstringEnd < 0) throw new Error('as_dict 的 docstring 边界解析不到')
  return body.slice(docstringEnd + 3)
}

describe('unitOfMetric：只认账本声明的单位', () => {
  it('ms 与 s 原样接受', () => {
    expect(unitOfMetric('ms')).toBe('ms')
    expect(unitOfMetric('s')).toBe('s')
  })

  it('缺失或未知单位抛错，不回落成毫秒', () => {
    // 回落 ms 就是把 0.019 秒读成 0.019 毫秒——这正是本次要钉死的失效方式
    expect(() => unitOfMetric(undefined)).toThrow(/未声明单位|未知/)
    expect(() => unitOfMetric('min')).toThrow(/未知/)
    expect(() => unitOfMetric('')).toThrow(/未知|未声明单位/)
  })

  it('单位集合只有两种，且都在合法后缀集合里', () => {
    expect(METRIC_UNITS).toEqual(['ms', 's'])
    expect(METRIC_SUFFIXES).toEqual(['_ms', '_seconds'])
  })
})

describe('unitFromMetricName：名字只用来核对，不用来取值', () => {
  it('后缀判单位，无后缀返回 null', () => {
    expect(unitFromMetricName('ingest_end_to_end_seconds')).toBe('s')
    expect(unitFromMetricName('report_intake_seconds')).toBe('s')
    expect(unitFromMetricName('warning_reach_ms')).toBe('ms')
    expect(unitFromMetricName('collab_txn')).toBeNull()
  })
})

describe('换算与显示', () => {
  it('共用毫秒轴时先按单位换算', () => {
    expect(toMillis('s', 0.019)).toBeCloseTo(19, 6)
    expect(toMillis('ms', 4200)).toBe(4200)
  })

  it('数字与单位一起显示，缺值写破折号而不是 0', () => {
    expect(formatMetricValue('s', 0.019)).toBe('0.019 s')
    expect(formatMetricValue('ms', 4200)).toBe('4200 ms')
    expect(formatMetricValue('ms', null)).toBe('—')
    expect(formatMetricValue('ms', undefined)).toBe('—')
  })
})

describe('跨端：后端出口必须自带 unit 且键名中性', () => {
  it('as_dict 声明 unit，分位数与预算的键名不带单位后缀', () => {
    const body = backendAsDict()
    expect(body).toContain('"unit"')
    for (const key of ['p50', 'p95', 'p99', 'max', 'mean', 'budget']) {
      expect(body, `${key} 的键名不得再声称单位`).toContain(`"${key}"`)
      expect(body, `${key}_ms 这类键名会把秒制指标说成毫秒`).not.toContain(`"${key}_ms"`)
      expect(body, `${key}_s 这类键名会把毫秒指标说成秒`).not.toContain(`"${key}_s"`)
    }
  })

  it('后端判成秒的后缀，前端也认它是秒', () => {
    const text = readRepoFile('backend', 'src', 'aegis', 'observability', 'tracer.py')
    const match = text.match(/SECOND_SUFFIXES\s*=\s*\(([^)]*)\)/)
    if (!match?.[1]) throw new Error('tracer.py 里解析不到 SECOND_SUFFIXES：后端单位判定写法变了')
    const secondsSuffixes = [...match[1].matchAll(/"([^"]+)"/g)].map((m) => m[1] as string)
    expect(secondsSuffixes.length).toBeGreaterThan(0)
    for (const suffix of secondsSuffixes) {
      // 后端认的秒制后缀必须落进前端认可的那两种之一，否则 unitFromMetricName 会把它判成 ms
      expect(suffix, `后端秒制后缀 ${suffix} 不在前端后缀集合里`).toMatch(/^_(seconds|s)$/)
      if (suffix === '_seconds') expect(unitFromMetricName(`x${suffix}`)).toBe('s')
    }
    expect(unitOfMetric('s')).toBe('s')
  })

  it('预算的单位与指标一致：后端按名字判单位，不再各处换算', () => {
    const text = readRepoFile('backend', 'src', 'aegis', 'observability', 'instrumentation.py')
    expect(text).toContain('millis_of(name, registered)')
  })
})
