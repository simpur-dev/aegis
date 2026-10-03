/**
 * 时间显示口径的单测。
 *
 * 关键的一条是跨日：`...T16:45Z` 在东八区已经是**次日** 00:45。只测"同一天的加八小时"
 * 是测不出漏加的——日期部分错了在界面上看着仍然像个正常时间戳。
 */

import { describe, expect, it } from 'vitest'

import { OPERATING_OFFSET_HOURS, formatOperatingTime, operatingClock } from './clock'

describe('formatOperatingTime：UTC 换东八区', () => {
  it('同一天内', () => {
    expect(formatOperatingTime('2026-10-03T02:15:30.000Z')).toBe('2026-10-03 10:15:30')
  })

  it('跨日：日期必须一起进位', () => {
    expect(formatOperatingTime('2026-10-03T16:45:13.840Z')).toBe('2026-10-04 00:45:13')
  })

  it('跨年：12 月 31 日 16:00Z 是次年 1 月 1 日', () => {
    expect(formatOperatingTime('2026-12-31T16:00:00.000Z')).toBe('2027-01-01 00:00:00')
  })

  it('毫秒被丢掉：界面到秒为止，但不改变秒值', () => {
    expect(formatOperatingTime('2026-01-01T00:00:00.999Z')).toBe('2026-01-01 08:00:00')
  })

  it('空值给破折号，坏值原样退回而不是编一个时间', () => {
    expect(formatOperatingTime(null)).toBe('—')
    expect(formatOperatingTime(undefined)).toBe('—')
    expect(formatOperatingTime('昨天下午')).toBe('昨天下午')
    // 没有 Z 后缀 = 时区未知，猜一个正好错八小时，所以原样显示
    expect(formatOperatingTime('2026-10-03T16:45:13')).toBe('2026-10-03T16:45:13')
  })

  it('偏移量是 8 小时，且与函数行为一致（防止改了常量没改换算）', () => {
    expect(OPERATING_OFFSET_HOURS).toBe(8)
    const base = Date.parse('2026-06-01T00:00:00.000Z')
    const shifted = new Date(base + OPERATING_OFFSET_HOURS * 3_600_000)
    expect(formatOperatingTime('2026-06-01T00:00:00.000Z')).toBe(
      `2026-06-01 ${shifted.getUTCHours().toString().padStart(2, '0')}:00:00`,
    )
  })
})

describe('operatingClock：图表轴用的短形态', () => {
  it('只留时分秒，且与完整形态同一来源（不各算一遍）', () => {
    expect(operatingClock('2026-10-03T16:45:13.840Z')).toBe('00:45:13')
    expect(operatingClock('2026-10-03T02:15:30.000Z')).toBe('10:15:30')
  })

  it('坏值不炸：退回与完整形态一致的东西', () => {
    expect(operatingClock(null)).toBe('—')
    expect(operatingClock('nonsense')).toBe('nonsense')
  })
})
