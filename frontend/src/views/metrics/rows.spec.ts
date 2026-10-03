/**
 * 指标页行换算：单位、阈值与"未设阈值 ≠ 达标"。
 *
 * 输入用的是账本出口的**真实形状**（中性键名 + unit），不是页面自己希望的形状：
 * 后端少发一个 unit，这里必须炸给开发看，而不是渲染出一行看不出错的字。
 */

import { describe, expect, it } from 'vitest'

import type { LatencyStats } from '@/api/types'
import { latencyRows } from './rows'

function stats(overrides: Partial<LatencyStats> = {}): LatencyStats {
  return {
    count: 10,
    unit: 'ms',
    p50: 3,
    p95: 12,
    p99: 20,
    max: 30,
    mean: 5,
    ...overrides,
  }
}

describe('latencyRows', () => {
  it('秒制指标按 s 出行，阈值同单位相比', () => {
    const [row] = latencyRows(
      { ingest_end_to_end_seconds: stats({ unit: 's', p95: 0.019, budget: 300 }) },
      { ingest_end_to_end_seconds: '数据接入端到端时延' },
    )
    expect(row?.unit).toBe('s')
    expect(row?.label).toBe('数据接入端到端时延')
    expect(row?.budget).toBe(300)
    expect(row?.pass, '0.019 秒 ≤ 300 秒：两侧同为秒才可比').toBe(true)
  })

  it('P95 超阈值的毫秒指标判超标，并带出越限样本数', () => {
    const [row] = latencyRows({ warning_generation_ms: stats({ p95: 200_000, budget: 180_000, breaches: 7 }) })
    expect(row?.pass).toBe(false)
    expect(row?.breaches).toBe(7)
  })

  it('没有阈值就是 null，不是"达标"', () => {
    const [row] = latencyRows({ ingest_publish_ms: stats() })
    expect(row?.budget).toBeNull()
    expect(row?.pass).toBeNull()
    expect(row?.breaches).toBeNull()
  })

  it('缺单位的出口直接抛错，不猜成毫秒', () => {
    const broken = { collab_txn: { ...stats(), unit: undefined as unknown as string } }
    expect(() => latencyRows(broken)).toThrow(/未声明单位/)
  })

  it('出口单位与指标名后缀矛盾时抛错，不挑一个渲染', () => {
    expect(() => latencyRows({ ingest_end_to_end_seconds: stats({ unit: 'ms' }) })).toThrow(/不一致/)
    expect(() => latencyRows({ warning_reach_ms: stats({ unit: 's' }) })).toThrow(/不一致/)
    // 无后缀的历史名字无可核对，照常出行
    expect(latencyRows({ collab_txn: stats({ unit: 's' }) })[0]?.unit).toBe('s')
  })

  it('缺分位数键的出口抛错：形状变了要被看见，而不是渲染成 undefined', () => {
    const broken = { stage_plan_ms: { ...(stats() as unknown as Record<string, unknown>), p95: undefined } }
    expect(() => latencyRows(broken as unknown as Record<string, LatencyStats>)).toThrow(/p95/)
  })

  it('没起中文标签的用埋点名，不留空', () => {
    const [row] = latencyRows({ brand_new_metric_ms: stats() })
    expect(row?.label).toBe('brand_new_metric_ms')
    expect(row?.name).toBe('brand_new_metric_ms')
  })
})
