/**
 * 指标单位判定的单测。
 *
 * 除了算式本身，这里还有一条跨端对账：后端账本里凡是名字以 `_seconds` 结尾的埋点，
 * 前端都必须按秒显示。新增一条秒制埋点而忘了跟进这里，症状是"指标页把那行读成毫秒"
 * ——数值差 1000 倍，而判定列照样是绿的，没有任何东西会红。
 */

import { describe, expect, it } from 'vitest'

import { formatMetricValue, metricUnit, toMillis } from './metricUnits'
import { readRepoFile } from '@/testing/repoSource'

/** 从后端预算登记表里读出所有秒制埋点名（真源只有一份，不在测试里抄清单）。 */
function backendSecondsMetrics(): string[] {
  const text = readRepoFile('backend', 'src', 'aegis', 'observability', 'instrumentation.py')
  const found = [...text.matchAll(/"([a-z_]+_seconds)":/g)].map((match) => match[1] as string)
  const unique = [...new Set(found)]
  if (unique.length === 0) throw new Error('instrumentation.py 里一个 _seconds 埋点都没解析到：写法变了，门禁要跟着改（不是把断言删掉）')
  return unique
}

describe('metricUnit：单位由埋点名决定', () => {
  it('秒制后缀判成 s，其余判成 ms', () => {
    expect(metricUnit('ingest_end_to_end_seconds')).toBe('s')
    expect(metricUnit('report_intake_seconds')).toBe('s')
    expect(metricUnit('warning_reach_ms')).toBe('ms')
    expect(metricUnit('collab_txn')).toBe('ms')
  })

  it('后端登记的每一条秒制埋点，前端都按秒处理（跨端对账）', () => {
    expect(backendSecondsMetrics().map(metricUnit)).toEqual(backendSecondsMetrics().map(() => 's'))
  })
})

describe('换算与显示', () => {
  it('toMillis 只换算秒制项，毫秒项原样', () => {
    expect(toMillis('report_intake_seconds', 0.661)).toBeCloseTo(661)
    expect(toMillis('warning_reach_ms', 1201.976)).toBe(1201.976)
  })

  it('单元格把单位带在值后面，缺阈值给破折号而不是 0 ms', () => {
    expect(formatMetricValue('ingest_store_ms', 5.839)).toBe('5.839 ms')
    expect(formatMetricValue('ingest_end_to_end_seconds', 0.01)).toBe('0.01 s')
    expect(formatMetricValue('ingest_publish_ms', null)).toBe('—')
    expect(formatMetricValue('ingest_publish_ms', undefined)).toBe('—')
  })
})
