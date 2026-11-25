import { describe, expect, it } from 'vitest'

import { rasterizeRain } from './rainTexture'
import { plotRainCells, rainCellAt, rainSelectionPoint, type RainFrame, type RainGrid } from './weather'

const grid: RainGrid = { lons: [85.65, 85.75, 85.85], lats: [28.05, 28.15], step_lon: 0.1, step_lat: 0.1 }
const frame: RainFrame = { end: '2024-07-06', values: [0, null, 65.09, 10, 25, 38], peak: 65.09 }

describe('历史雨带', () => {
  it('按北朝上绘制，并让缺测格和零雨量格保持透明', () => {
    const { pixels, width, height } = rasterizeRain(grid, frame, 16)
    expect(width).toBe(48)
    expect(height).toBe(32)
    const alpha = (x: number, y: number) => pixels[(y * width + x) * 4 + 3]
    for (let y = 16; y < height; y++) {
      for (let x = 0; x < 32; x++) expect(alpha(x, y)).toBe(0)
    }
    expect(alpha(7, 7)).toBeGreaterThan(0)
    expect(alpha(39, 22)).toBeGreaterThan(0)
    expect(alpha(47, 22)).toBeLessThan(alpha(39, 22))
  })

  it('点选用原始格点，保留缺测及覆盖边界', () => {
    const cells = plotRainCells(grid, frame)
    const highest = rainCellAt(cells, { lon: 85.85, lat: 28.05 })!
    expect(highest.value).toBe(65.09)
    expect(rainSelectionPoint(highest).label).toBe('65.09 mm')
    expect(rainCellAt(cells, { lon: 85.75, lat: 28.05 })?.value).toBeNull()
    expect(rainCellAt(cells, { lon: 85.65, lat: 28.05 })?.value).toBe(0)
    expect(rainCellAt(cells, { lon: 86.05, lat: 28.05 })).toBeUndefined()
  })
})
