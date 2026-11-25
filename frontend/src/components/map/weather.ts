import type { BBox, LngLat } from './entities'
import { weatherColors } from '@/theme/tokens'

export type WeatherMode = 'current' | 'daily' | 'hourly'

export interface ForecastHour {
  end: string
  rain_mm: number | null
}

export interface CurrentWeatherPoint {
  id: string
  valid_at: string
  interval_minutes: number
  temperature_c: number | null
  wind_kmh: number | null
  rain_mm: number | null
  weather_code: number | null
  is_day: boolean | null
  forecast: ForecastHour[]
}

export interface CurrentMapWeather {
  status: 'available' | 'stale' | 'unavailable' | 'disabled'
  fetched_at: string | null
  points: CurrentWeatherPoint[]
}

export interface RainCatalog {
  daily_years: number[]
  hourly_available: boolean
}

export interface RainGrid {
  lats: number[]
  lons: number[]
  step_lat: number
  step_lon: number
}

export interface RainFrame {
  end: string
  values: (number | null)[]
  peak: number | null
}

export interface RainArchive {
  source: string
  file: string
  grid: RainGrid | null
  points: { id: string; name: string; lon: number; lat: number }[]
  frames: RainFrame[]
}

export interface WeatherMapPoint {
  id: string
  title: string
  label: string
  lonlat: LngLat
  mainlandChina: boolean
  color: string
}

export interface WeatherMapCell {
  id: string
  bounds: BBox
  value: number
  color: string
}

export interface WeatherMapLayer {
  points: WeatherMapPoint[]
  cells: WeatherMapCell[]
  subdued?: boolean
}

export function rainCellOpacity(value: number, subdued = false): number {
  return subdued ? (value === 0 ? 0.03 : 0.14) : (value === 0 ? 0.10 : 0.53)
}

export const EMPTY_WEATHER_LAYER: WeatherMapLayer = { points: [], cells: [] }
export const DAILY_RAIN_BANDS = [
  { from: 0, label: '0–<10', color: weatherColors['rain-low'] },
  { from: 10, label: '10–<25', color: weatherColors['rain-mid'] },
  { from: 25, label: '25–<50', color: weatherColors['rain-high'] },
  { from: 50, label: '≥50', color: weatherColors['rain-extreme'] },
] as const

export const HOUR_RAIN_BANDS = [
  { from: 0, label: '0–<1', color: weatherColors['rain-low'] },
  { from: 1, label: '1–<3', color: weatherColors['rain-mid'] },
  { from: 3, label: '3–<10', color: weatherColors['rain-high'] },
  { from: 10, label: '≥10', color: weatherColors['rain-extreme'] },
] as const

export function rainColor(value: number, mode: 'daily' | 'hourly'): string {
  const bands = mode === 'daily' ? DAILY_RAIN_BANDS : HOUR_RAIN_BANDS
  return [...bands].reverse().find((band) => value >= band.from)?.color ?? bands[0].color
}

export function plotRainCells(grid: RainGrid, frame: RainFrame): WeatherMapCell[] {
  const cells: WeatherMapCell[] = []
  const dx = grid.step_lon / 2
  const dy = grid.step_lat / 2
  grid.lats.forEach((lat, row) => grid.lons.forEach((lon, col) => {
    const value = frame.values[row * grid.lons.length + col]
    if (value === null || value === undefined) return
    cells.push({
      id: `weather-cell-${row}-${col}`,
      bounds: { west: lon - dx, east: lon + dx, south: lat - dy, north: lat + dy },
      value,
      color: rainColor(value, 'daily'),
    })
  }))
  return cells
}

export function gridBounds(grid: RainGrid): BBox {
  const dx = grid.step_lon / 2
  const dy = grid.step_lat / 2
  return {
    west: grid.lons[0] - dx,
    east: grid.lons[grid.lons.length - 1] + dx,
    south: grid.lats[0] - dy,
    north: grid.lats[grid.lats.length - 1] + dy,
  }
}

export function weatherCondition(code: number | null, isDay?: boolean | null): { label: string; icon: string } {
  if (code === null) return { label: '天气未知', icon: 'unknown' }
  if (code === 0) return { label: '晴', icon: isDay === false ? 'moon' : 'sun' }
  if (code <= 3) return { label: code === 3 ? '阴' : '多云', icon: 'cloud' }
  if (code <= 48) return { label: '雾', icon: 'cloud' }
  if (code === 56 || code === 57 || code === 66 || code === 67) return { label: '冻雨', icon: 'rain' }
  if ((code >= 71 && code <= 77) || code === 85 || code === 86) return { label: '降雪', icon: 'snow' }
  if (code >= 95) return { label: '雷雨', icon: 'storm' }
  return { label: code <= 55 ? '毛毛雨' : code >= 80 ? '阵雨' : '降雨', icon: 'rain' }
}

export function rainText(value: number | null): string {
  return value === null ? '—' : value.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}
