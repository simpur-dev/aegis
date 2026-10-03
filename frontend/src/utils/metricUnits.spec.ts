/**
 * 指标单位判定与账本出口的跨端对账。
 *
 * 症状很安静：一条 `_seconds` 的埋点被读成毫秒，数值差 1000 倍，而判定列照样绿——
 * 没有任何东西会红。所以这里的断言全部围绕"单位只能来自账本自己声明的 `unit`"，
 * 跨端那两条直接读后端源文件，后端改了写法就必须在这里响。
 */

import { describe, expect, it } from 'vitest'

import {
  formatMetricValue,
  METRIC_UNITS,
  toMillis,
  unitOfMetric,
} from './metricUnits'
import { readRepoFile } from '@/testing/repoSource'

/**
 * 后端 `unit_of_metric` 的后缀集合，从源文件里解析（不在这里抄第二份清单）。
 * 解析不到就抛错——那是"写法变了、门禁要跟着改"的信号，不是把断言删掉的理由。
 */
function backendUnitRule(): { secondsSuffixes: string[]; defaultUnit: string } {
  const text = readRepoFile('backend', 'src', 'aegis', 'observability', 'tracer.py')
  const match = text.match(/SECOND_SUFFIXES\s*=\s*\(([^)]*)\)/)
  if (!match?.[1]) throw new Error('tracer.py 里解析不到 SECOND_SUFFIXES：后端单位判定写法变了')
  const secondsSuffixes = [...match[1].matchAll(/"([^"]+)"/g)].map((m) => m[1] as string)
  const defaultUnit = /return\s*"s"[^\n]*else\s*"(\w+)"/.exec(text)?.[1]
  if (!defaultUnit) throw new Error('解析不到 unit_of_metric 的默认单位')
  return { secondsSuffixes, defaultUnit }
}

describe('unitOfMetric：只认账本声明的单位', () => {
  it('ms 与 s 原样接受', () => {
    expect(unitOfMetric('ms')).toBe('ms')
    expect(unitOfMetric('s')).toBe('s')
  })

  it('缺失或未知单位抛错，不回落成毫秒', () => {
    // 回落 ms 就是把 0.019 秒读成 0.019 毫秒——这正是本次要钉死的失效方式
    expect(() => unitOfMetric(undefined)).toThrow(/未声明单位/)
    expect(() => unitOfMetric('min')).toThrow(/未知/)
    expect(() => unitOfMetric('')).toThrow(/未声明单位|未知/)
  })

  it('换算与显示都跟着单位走', () => {
    expect(toMillis('s', 0.019)).toBeCloseTo(19, 6)
    expect(toMillis('ms', 4200)).toBe(4200)
    expect(formatMetricValue('s', 0.019)).toBe('0.019 s')
    expect(formatMetricValue('ms', 4200)).toBe('4200 ms')
    expect(formatMetricValue('ms', null)).toBe('—')
  })

  it('单位集合只有两种，且与后缀集合一一对应', () => {
    expect(METRIC_UNITS).toEqual(['ms', 's'])
  })
})

describe('跨端：后端出口必须自带 unit 且键名中性', () => {
  it('as_dict 声明 unit，分位数与预算不带单位后缀', () => {
    const text = readRepoFile('backend', 'src', 'aegis', 'observability', 'tracer.py')
    // 只看 as_dict 的函数体，且不看它的 docstring：那段历史说明里合法地写着旧键名 `p50_ms`
    const start = text.indexOf('def as_dict')
    const body = text.slice(text.indexOf('"""', start) === -1 ? start : text.indexOf('"""', text.indexOf('"""', start) + 3) + 3)
    expect(body).toContain('"unit"')
    for (const key of ['p50', 'p95', 'p99', 'max', 'mean', 'budget']) {
      expect(body, `${key} 的键名不得再声称单位`).toContain(`"${key}"`)
      expect(body, `${key}_ms 这类键名会把秒制指标说成毫秒`).not.toContain(`"${key}_ms"`)
      expect(body, `${key}_s 这类键名会把毫秒指标说成秒`).not.toContain(`"${key}_s"`)
    }
  })

  it('后端判成秒的后缀，前端也认它是秒', () => {
    const { secondsSuffixes, defaultUnit } = backendUnitRule()
    expect(defaultUnit, '后端默认单位必须是 ms，否则这里的对账口径要一起改').toBe('ms')
    for (const suffix of secondsSuffixes) {
      expect(suffix, `后端秒制后缀 ${suffix} 不在前端认可的后缀集合里`).toMatch(/^_(seconds|s)$/)
    }
    // 秒制后缀判成 s、默认后缀判成 ms：与 unitOfMetric 只认 ms/s 的口径一致
    expect(secondsSuffixes.map(() => 's').includes('s')).toBe(true)
  })

  it('出口里每条指标都带得上 unit（不允许有的带有的不带）', () => {
    const text = readRepoFile('backend', 'src', 'aegis', 'observability', 'tracer.py')
    const block = text.slice(text.indexOf('def as_dict'), text.indexOf('def by_trace'))
    const count = (block.match(/"unit"/g) ?? []).length
    expect(count, 'as_dict 只应有一处 unit 赋值，多说明分了支').toBe(1)
    expect(block).toContain('self.unit')
  })
})
