/**
 * 单行接入时延的显示口径。核心是"测不出来就不许显示成 0 ms"。
 */

import { describe, expect, it } from 'vitest'

import { ingestLatencyLabel } from './ingestLatency'

describe('ingestLatencyLabel', () => {
  it('两个戳逐字节相同（模拟站就是如此）时报"测不出"，而不是一个永远完美的 0 ms', () => {
    const same = { observed_at: '2026-10-03T18:09:57.846Z', ingested_at: '2026-10-03T18:09:57.846Z' }
    expect(ingestLatencyLabel(same)).toContain('测不出')
    expect(ingestLatencyLabel(same)).not.toContain('0 ms')
  })

  it('真有时差时按毫秒显示', () => {
    expect(ingestLatencyLabel({ observed_at: '2026-10-03T04:00:00Z', ingested_at: '2026-10-03T04:00:01Z' })).toBe('1000 ms')
    expect(ingestLatencyLabel({ observed_at: '2026-10-03T04:00:00.000Z', ingested_at: '2026-10-03T04:00:00.412Z' })).toBe('412 ms')
  })

  it('时钟倒挂要露出来：那是站端时钟慢了，不是零时延', () => {
    const label = ingestLatencyLabel({ observed_at: '2026-10-03T04:00:05Z', ingested_at: '2026-10-03T04:00:00Z' })
    expect(label).toContain('时钟倒挂')
    expect(label).toContain('5000 ms')
  })

  it('缺任一侧时间戳说不"无从判断"，不拿 Date(undefined) 的 NaN 去凑', () => {
    expect(ingestLatencyLabel({ observed_at: '2026-10-03T04:00:00Z', ingested_at: null })).toContain('无从判断')
    expect(ingestLatencyLabel({ observed_at: undefined, ingested_at: '2026-10-03T04:00:00Z' })).toContain('无从判断')
    expect(ingestLatencyLabel({})).toContain('无从判断')
  })

  it('格式能过但值非法（如 0000-00-00）时判不可解析，而不是显示 NaN ms', () => {
    const label = ingestLatencyLabel({ observed_at: '0000-00-00T00:00:00Z', ingested_at: '2026-10-03T04:00:00Z' })
    expect(label).toContain('不可解析')
    expect(label).not.toContain('NaN')
  })
})
