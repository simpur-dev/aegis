/**
 * 程序化「能量星球」纹理（等距圆柱 2:1），给「一张图」的深空视图当覆盖层用。
 *
 * 为什么不用 ThreeUI 那个 `<GlobeCollection />`：它是 React + 独立的 WebGL 球，
 * 和本页的 Cesium 地球（影像/地形/要素都在上面）不是一套渲染管线；把它盖上来就等于
 * 换掉地图。这里换一条路——只在"看得到地球弧线"的高度，给地球叠一张程序化纹理，
 * 拉到区域视图时透明度归零，地图原样可读。
 *
 * 也不用图片资源：一张 1024×512 的 PNG 会进构建产物，而这张纹理是确定性的噪声，
 * 运行时画一次（一两百毫秒）即可，离线交付不多个资产。
 *
 * 画法：域扭曲（domain warping）的多层 FBM——先把采样坐标用另一层噪声推开，
 * 再取 FBM，云才会是"丝缕状"而不是一团团棉花。配色照参考图：靛蓝底 + 紫蓝云 + 近白高光。
 */

const WIDTH = 1024
const HEIGHT = 512
const SEED = 20261025

/** 确定性 PRNG（mulberry32）：同一份代码在任何机器上画出同一张纹理。 */
function mulberry32(seed: number): () => number {
  let state = seed >>> 0
  return () => {
    state = (state + 0x6d2b79f5) | 0
    let t = Math.imul(state ^ (state >>> 15), 1 | state)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

const smooth = (t: number): number => t * t * (3 - 2 * t)
const clamp01 = (t: number): number => Math.max(0, Math.min(1, t))
const wrap = (value: number, period: number): number => ((value % period) + period) % period

/**
 * 可横向无缝的值噪声：x/y 方向都按周期取模，接缝落在 ±180° 经线或极点上都不显。
 * 每个周期一张随机格点表，读的时候做双线性插值。
 *
 * 注意 `wrap`：域扭曲会把采样坐标推到负数，JS 的 `%` 对负数返回负值，
 * 直接拿来索引就是 undefined → NaN → 画面上出现黑色矩形缺口（真机踩过）。
 */
function makeNoise(seed: number): (u: number, v: number, period: number) => number {
  const random = mulberry32(seed)
  const grids = new Map<number, Float32Array>()
  function grid(period: number): Float32Array {
    const cached = grids.get(period)
    if (cached) return cached
    const values = new Float32Array(period * period)
    for (let i = 0; i < values.length; i += 1) values[i] = random()
    grids.set(period, values)
    return values
  }
  return (u, v, period) => {
    const values = grid(period)
    const x = u * period
    const y = v * period
    const x0 = wrap(Math.floor(x), period)
    const y0 = wrap(Math.floor(y), period)
    const x1 = (x0 + 1) % period
    const y1 = (y0 + 1) % period
    const fx = smooth(x - Math.floor(x))
    const fy = smooth(y - Math.floor(y))
    const top = values[y0 * period + x0] * (1 - fx) + values[y0 * period + x1] * fx
    const bottom = values[y1 * period + x0] * (1 - fx) + values[y1 * period + x1] * fx
    return top * (1 - fy) + bottom * fy
  }
}

/** 多倍频叠加：period 逐层翻倍、振幅减半，结果归一化到 0..1。 */
function fbm(
  sample: (u: number, v: number, period: number) => number,
  u: number,
  v: number,
  basePeriod: number,
  octaves: number,
): number {
  let sum = 0
  let amplitude = 1
  let weight = 0
  let period = basePeriod
  for (let i = 0; i < octaves; i += 1) {
    sum += sample(u, v, period) * amplitude
    weight += amplitude
    amplitude *= 0.5
    period *= 2
  }
  return sum / weight
}

/** 画一张深蓝能量球纹理：靛蓝底 + 丝缕状云团 + 近白高光，两极压暗避免极点拉伸。 */
export function createEnergyPlanetCanvas(): HTMLCanvasElement {
  const canvas = document.createElement('canvas')
  canvas.width = WIDTH
  canvas.height = HEIGHT
  const context = canvas.getContext('2d')
  if (!context) return canvas

  const warpX = makeNoise(SEED + 11)
  const warpY = makeNoise(SEED + 23)
  const clouds = makeNoise(SEED + 37)
  const wisps = makeNoise(SEED + 53)

  const image = context.createImageData(WIDTH, HEIGHT)
  const data = image.data

  for (let y = 0; y < HEIGHT; y += 1) {
    const v = y / (HEIGHT - 1)
    // 纬度权重：赤道最亮，极点收暗（等距圆柱在极点会拉长，亮着会糊成一片）
    const polar = 0.5 + 0.5 * Math.sin(Math.PI * v)
    for (let x = 0; x < WIDTH; x += 1) {
      const u = x / WIDTH

      // 域扭曲：用两层低频噪声把采样坐标推开，云才会卷成丝
      const offsetU = (fbm(warpX, u, v, 2, 3) - 0.5) * 0.45
      const offsetV = (fbm(warpY, u, v, 2, 3) - 0.5) * 0.3
      const wu = u + offsetU
      const wv = v + offsetV

      // 主云层 + 一层高频丝缕：乘积让云有厚薄、有撕裂感
      const base = fbm(clouds, wu, wv, 3, 5)
      const wisp = fbm(wisps, wu * 2.3, wv * 2.3, 6, 4)
      const density = clamp01(base * 0.72 + wisp * 0.28)
      const cloud = clamp01((density - 0.32) / 0.5) * polar
      const highlight = clamp01((cloud - 0.62) / 0.38)

      // 靛蓝底 → 紫蓝云 → 近白高光
      const r = 10 + cloud * 62 + highlight * 150
      const g = 14 + cloud * 72 + highlight * 162
      const b = 42 + cloud * 138 + highlight * 120

      const index = (y * WIDTH + x) * 4
      data[index] = Math.min(255, r)
      data[index + 1] = Math.min(255, g)
      data[index + 2] = Math.min(255, b)
      data[index + 3] = 255
    }
  }

  context.putImageData(image, 0, 0)
  return canvas
}
