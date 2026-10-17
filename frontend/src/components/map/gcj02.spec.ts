/**
 * WGS84 → GCJ-02 纠偏的判据。
 *
 * 刻意**不钉"某个点的精确输出坐标"**：那需要一个可信的权威对照表，而本机没有离线可查的
 * 国测局基准。把猜出来的数写进断言，等于用一条假绿掩盖"纠偏方向反了/量级不对"这种真错。
 * 所以这里钉的是能独立判定的性质：境外不动、境内必动、量级在公开已知的百米档、
 * 且**不可重复施加**（重复纠偏是最容易犯的错，症状恰恰是"看着挺对"）。
 */
import { describe, expect, it } from 'vitest'

import { approxMeters, wgs84ToGcj02 } from './gcj02'

describe('WGS84 → GCJ-02', () => {
  it('境外点原样返回（该体系只对境内扰动）', () => {
    const london = { lon: -0.1278, lat: 51.5074 }
    expect(wgs84ToGcj02(london)).toEqual(london)
  })

  it('境内点必须偏移，且偏移量落在公开已知的百米量级', () => {
    for (const point of [
      { lon: 91.117, lat: 29.652 }, // 拉萨
      { lon: 87.617, lat: 43.793 }, // 乌鲁木齐（境内东北角，验证不是只在西藏生效）
      { lon: 116.397, lat: 39.909 }, // 北京（公开案例最多的点）
    ]) {
      const shifted = wgs84ToGcj02(point)
      const meters = approxMeters(point, shifted)
      expect(meters, `${JSON.stringify(point)} 纠偏后位移 ${meters.toFixed(1)}m，不在百米档`).toBeGreaterThan(50)
      expect(meters).toBeLessThan(1_200)
      expect(shifted).not.toEqual(point)
    }
  })

  it('同区域内偏移方向一致（整体平移的特征，不是逐点乱抖）', () => {
    const lhasaArea = [
      { lon: 91.117, lat: 29.652 },
      { lon: 91.75, lat: 29.26 },
      { lon: 90.6, lat: 29.85 },
    ]
    const deltas = lhasaArea.map((point) => {
      const shifted = wgs84ToGcj02(point)
      return { dLon: shifted.lon - point.lon, dLat: shifted.lat - point.lat }
    })
    for (const axis of ['dLon', 'dLat'] as const) {
      const signs = new Set(deltas.map((delta) => Math.sign(delta[axis])))
      expect(signs.size, `${axis} 方向在同城区域内翻转，算法或参数不一致`).toBe(1)
    }
  })

  /**
   * 幂等性反例：纠偏只能施加一次。
   * 若哪天有人既在数据入口纠、又在 graphics 里纠，症状是"点整体又偏了一段"，
   * 而两次结果都"看起来像真坐标"——所以这条要能红。
   */
  it('重复施加会再偏一段：调用点只能有一处纠偏', () => {
    const point = { lon: 91.117, lat: 29.652 }
    const once = wgs84ToGcj02(point)
    const twice = wgs84ToGcj02(once)
    expect(approxMeters(once, twice), '两次纠偏结果一样——那第一次就没生效').toBeGreaterThan(20)
  })

  it('输出仍是合法经纬度（不 NaN、不越界）', () => {
    for (const point of [{ lon: 78.4, lat: 26.5 }, { lon: 99.1, lat: 36.5 }]) {
      const shifted = wgs84ToGcj02(point)
      expect(Number.isFinite(shifted.lon)).toBe(true)
      expect(Number.isFinite(shifted.lat)).toBe(true)
      expect(Math.abs(shifted.lat)).toBeLessThan(86)
    }
  })
})
