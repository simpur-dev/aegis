import { describe, expect, it } from 'vitest'
import { currentRainReading, type CurrentWeatherPoint } from './weather'

function point(weather_code: number | null, rain_mm: number | null): CurrentWeatherPoint {
  return {
    id: 'port', valid_at: '2026-11-27T09:15:00Z', interval_minutes: 15,
    temperature_c: 6, wind_kmh: 8, rain_mm, weather_code, is_day: true, forecast: [],
  }
}

describe('地图当前雨况和时段雨量', () => {
  it('当前雨况使用天气码，累计量单独显示', () => {
    expect(currentRainReading(point(61, 0))).toMatchObject({ label: '有雨', amountText: '0 mm', period: '近 15 分钟' })
    expect(currentRainReading(point(0, 2.4))).toMatchObject({ label: '无雨', amountText: '2.4 mm' })
    expect(currentRainReading(point(71, 0))).toMatchObject({ label: '降雪', amountText: '0 mm' })
  })

  it('没有天气码时只说明统计时段，缺测保留为缺测', () => {
    expect(currentRainReading(point(null, 2.4)).label).toBe('时段有雨')
    expect(currentRainReading(point(null, 0)).label).toBe('时段无雨')
    expect(currentRainReading(point(61, null))).toMatchObject({ label: '有雨', amountText: '雨量缺测' })
    expect(currentRainReading()).toMatchObject({ label: '暂无数据', amountText: '雨量缺测' })
  })
})
