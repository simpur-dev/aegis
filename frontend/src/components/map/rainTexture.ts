import { DAILY_RAIN_BANDS, gridBounds, type RainFrame, type RainGrid, type RainTexture } from './weather'

const palette = DAILY_RAIN_BANDS.map(({ from, color }) => {
  const rgb = Number.parseInt(color.slice(1), 16)
  return { from, red: rgb >> 16, green: (rgb >> 8) & 255, blue: rgb & 255 }
})

function smoothstep(value: number): number {
  const t = Math.max(0, Math.min(1, value))
  return t * t * (3 - 2 * t)
}

export function rasterizeRain(grid: RainGrid, frame: RainFrame, pixelsPerCell = 26) {
  const columns = grid.lons.length
  const rows = grid.lats.length
  const resolution = Math.min(pixelsPerCell, 384 / Math.max(columns, rows))
  const width = Math.ceil(columns * resolution)
  const height = Math.ceil(rows * resolution)
  const pixels = new Uint8ClampedArray(width * height * 4)
  const contourBands = new Uint8Array(width * height)
  const values = Array.from({ length: columns * rows }, (_, index) => {
    const value = frame.values[index]
    return value != null && Number.isFinite(value) && value >= 0 ? value : -1
  })

  for (let py = 0; py < height; py++) {
    const y = rows - (py + 0.5) * rows / height
    const south = Math.max(0, y - 0.5)
    const bottomRow = Math.floor(south)
    const topRow = Math.min(rows - 1, bottomRow + 1)
    const fy = south - bottomRow
    for (let px = 0; px < width; px++) {
      const x = (px + 0.5) * columns / width
      // 平滑只在有数据的原格点内生效，缺测格点不参与计算，也不会被涂色。
      if (values[Math.floor(y) * columns + Math.floor(x)] <= 0) continue

      const west = Math.max(0, x - 0.5)
      const leftColumn = Math.floor(west)
      const rightColumn = Math.min(columns - 1, leftColumn + 1)
      const fx = west - leftColumn
      const sw = values[bottomRow * columns + leftColumn]
      const se = values[bottomRow * columns + rightColumn]
      const nw = values[topRow * columns + leftColumn]
      const ne = values[topRow * columns + rightColumn]
      const weightSW = sw < 0 ? 0 : (1 - fx) * (1 - fy)
      const weightSE = se < 0 ? 0 : fx * (1 - fy)
      const weightNW = nw < 0 ? 0 : (1 - fx) * fy
      const weightNE = ne < 0 ? 0 : fx * fy
      const coverage = weightSW + weightSE + weightNW + weightNE
      const value = (sw * weightSW + se * weightSE + nw * weightNW + ne * weightNE) / coverage

      let band = 0
      while (band < palette.length - 2 && value >= palette[band + 1].from) band++
      const lower = palette[band]
      const upper = palette[band + 1]
      const blend = smoothstep((value - lower.from) / (upper.from - lower.from))
      const edge = Math.min(x, y, columns - x, rows - y)
      const mask = smoothstep((coverage - 0.53) / 0.47) * smoothstep(edge / 0.66)
      const offset = (py * width + px) * 4
      pixels[offset] = lower.red + (upper.red - lower.red) * blend
      pixels[offset + 1] = lower.green + (upper.green - lower.green) * blend
      pixels[offset + 2] = lower.blue + (upper.blue - lower.blue) * blend
      pixels[offset + 3] = mask * (68 + 86 * smoothstep(value / 38))
      contourBands[py * width + px] = pixels[offset + 3] >= 20 ? band + (value >= upper.from ? 2 : 1) : 0
    }
  }
  // 只在有色面的阈值交界提亮细线，不勾绘外框或缺测边界。
  for (let y = 1; y < height - 1; y++) {
    for (let x = 1; x < width - 1; x++) {
      const index = y * width + x
      const band = contourBands[index]
      if (!band || band >= Math.max(contourBands[index - 1], contourBands[index + 1], contourBands[index - width], contourBands[index + width])) continue
      const offset = index * 4
      for (let channel = 0; channel < 3; channel++) pixels[offset + channel] += (255 - pixels[offset + channel]) * 0.24
    }
  }
  return { pixels, width, height }
}

export function createRainTexture(grid: RainGrid, frame: RainFrame): RainTexture | null {
  if (!grid.lons.length || !grid.lats.length || !frame.values.some((value) => value != null && value > 0)) return null
  const canvas = document.createElement('canvas')
  const { pixels, width, height } = rasterizeRain(grid, frame)
  canvas.width = width
  canvas.height = height
  const context = canvas.getContext('2d')
  if (!context) return null
  const image = context.createImageData(width, height)
  image.data.set(pixels)
  context.putImageData(image, 0, 0)
  return { width, height, bounds: gridBounds(grid), url: canvas.toDataURL() }
}
