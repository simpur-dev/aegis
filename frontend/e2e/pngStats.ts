/**
 * 截图像素统计：把 Playwright 给的 PNG 解出来，判断"画布到底画没画出东西"。
 *
 * 为什么不直接 `canvas.toDataURL()`：Cesium 默认 `preserveDrawingBuffer: false`，
 * 在渲染循环外读 canvas 只会拿到清过的缓冲；而 GL 上下文丢了的时候，场景 API 里的
 * 高程与瓦计数可以一切正常，屏幕却是整片纯色。所以这一路必须看**合成后的真实画面**，
 * 也就是 Playwright 的截图。
 *
 * 为什么自己解 PNG 而不装图像库：只需要"取像素、数颜色"，`node:zlib` 的 inflate 就够；
 * 为此引入一个原生依赖不值得，而且它会成为这个离线项目又多一个要下载的构建物。
 */

import { inflateSync } from 'node:zlib'

export interface CanvasPixelStats {
  readonly width: number
  readonly height: number
  readonly channels: number
  /** 参与统计的不透明像素数。 */
  readonly samples: number
  readonly distinctColors: number
  /** 占比最高的单一颜色占全部样本的比例（0..1）。接近 1 就是"整片一个色"。 */
  readonly dominantShare: number
  readonly darkestLuminance: number
  readonly brightestLuminance: number
}

const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])

export function pngPixelStats(png: Buffer): CanvasPixelStats {
  if (png.subarray(0, 8).equals(PNG_SIGNATURE) === false) throw new Error('拿到的不是 PNG 截图，像素门禁没法做')

  let width = 0
  let height = 0
  let bitDepth = 0
  let colorType = 0
  let interlace = 0
  const idat: Buffer[] = []

  for (let offset = 8; offset + 8 <= png.length; ) {
    const length = png.readUInt32BE(offset)
    const type = png.subarray(offset + 4, offset + 8).toString('latin1')
    const data = png.subarray(offset + 8, offset + 8 + length)
    if (type === 'IHDR') {
      width = data.readUInt32BE(0)
      height = data.readUInt32BE(4)
      bitDepth = data[8]
      colorType = data[9]
      interlace = data[12]
    } else if (type === 'IDAT') {
      idat.push(Buffer.from(data))
    } else if (type === 'IEND') {
      break
    }
    offset += 12 + length
  }

  if (width === 0 || height === 0) throw new Error('PNG 里没有 IHDR 或尺寸为 0')
  if (bitDepth !== 8) throw new Error(`像素门禁只处理 8bit 深度的截图，实际 ${bitDepth}`)
  if (interlace !== 0) throw new Error(`不支持隔行扫描的 PNG（interlace=${interlace}）`)
  const channels = colorType === 6 ? 4 : colorType === 2 ? 3 : 0
  if (channels === 0) throw new Error(`截图不是 RGB/RGBA（colorType=${colorType}）`)

  const pixels = unfilters(inflateSync(Buffer.concat(idat)), width, height, channels)
  return summarize(pixels, width, height, channels)
}

/** PNG 每行开头是过滤类型字节；还原后才能拿到真像素。 */
function unfilters(raw: Buffer, width: number, height: number, channels: number): Buffer {
  const rowBytes = width * channels
  const out = Buffer.alloc(rowBytes * height)
  const bpp = channels // 8bit × 通道数
  for (let y = 0; y < height; y += 1) {
    const filter = raw[y * (rowBytes + 1)]
    const line = raw.subarray(y * (rowBytes + 1) + 1, y * (rowBytes + 1) + 1 + rowBytes)
    const target = out.subarray(y * rowBytes, (y + 1) * rowBytes)
    const above = y > 0 ? out.subarray((y - 1) * rowBytes, y * rowBytes) : null
    for (let x = 0; x < rowBytes; x += 1) {
      const left = x >= bpp ? target[x - bpp] : 0
      const up = above ? above[x] : 0
      const upperLeft = above && x >= bpp ? above[x - bpp] : 0
      const value = line[x]
      let restored: number
      switch (filter) {
        case 0:
          restored = value
          break
        case 1:
          restored = value + left
          break
        case 2:
          restored = value + up
          break
        case 3:
          restored = value + ((left + up) >> 1)
          break
        case 4:
          restored = value + paeth(left, up, upperLeft)
          break
        default:
          throw new Error(`未知的 PNG 行过滤类型 ${filter}`)
      }
      target[x] = restored & 0xff
    }
  }
  return out
}

function paeth(a: number, b: number, c: number): number {
  const p = a + b - 2 * c
  const pa = Math.abs(p - a)
  const pb = Math.abs(p - b)
  const pc = Math.abs(p - c)
  if (pa <= pb && pa <= pc) return a
  return pb <= pc ? b : c
}

function summarize(pixels: Buffer, width: number, height: number, channels: number): CanvasPixelStats {
  const counts = new Map<number, number>()
  let samples = 0
  let darkest = Number.POSITIVE_INFINITY
  let brightest = Number.NEGATIVE_INFINITY

  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const i = (y * width + x) * channels
      if (channels === 4 && pixels[i + 3] < 8) continue // 全透明像素不参与"是不是平色"的判断
      const r = pixels[i]
      const g = pixels[i + 1]
      const b = pixels[i + 2]
      samples += 1
      const key = (r << 16) | (g << 8) | b
      counts.set(key, (counts.get(key) ?? 0) + 1)
      const luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
      if (luminance < darkest) darkest = luminance
      if (luminance > brightest) brightest = luminance
    }
  }

  let dominant = 0
  for (const count of counts.values()) if (count > dominant) dominant = count
  return {
    width,
    height,
    channels,
    samples,
    distinctColors: counts.size,
    dominantShare: samples === 0 ? 1 : dominant / samples,
    darkestLuminance: samples === 0 ? 0 : darkest,
    brightestLuminance: samples === 0 ? 0 : brightest,
  }
}
