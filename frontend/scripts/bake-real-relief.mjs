/**
 * 烘焙真实高程资产：public/terrain-real（quantized-mesh）+ public/basemaps/relief.pmtiles（山体阴影设色影像）。
 *
 * 高程源：Mapzen / AWS Terrain Tiles 公开数据集（terrarium 编码，SRTM / GEBCO / GMTED 等合成），
 * https://s3.amazonaws.com/elevation-tiles-prod/v2/terrarium/{z}/{x}/{y}.png —— 下载一次后全程离线复烘。
 * 缓存落在 frontend/.terrain-cache/（不入库）；产物入库，现场断网可跑。
 *
 * 为什么另开 terrain-real 而不覆盖 public/terrain：合成样本被 offline-assets.spec.ts /
 * terrain-parse.spec.ts 与 backend/tests/unit/test_offline_tile_format.py 逐字节钉住，
 * 它的职责是"离线链路可验"，不是作战底图；真实高程走新路径，两条腿互不污染。
 *
 * 用法：node scripts/bake-real-relief.mjs [--offline] [--gyirong | --basemap-only]
 * --gyirong 生成口岸和周边影像，--basemap-only 只生成全域影像，--offline 只读缓存。
 */
import { mkdirSync, readFileSync, writeFileSync, existsSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import zlib from 'node:zlib'

const here = fileURLToPath(new URL('.', import.meta.url))
const pub = join(here, '..', 'public')
const cache = join(here, '..', '.terrain-cache')
const offline = process.argv.includes('--offline')
mkdirSync(cache, { recursive: true })

const SRC = 'https://s3.amazonaws.com/elevation-tiles-prod/v2/terrarium'
const MAX_SHORT = 32767
const PNG = 256
const gyirong = JSON.parse(readFileSync(join(here, '../src/components/map/gyirong-relief.json'), 'utf8'))

// ---------- Web Mercator 源瓦片清单：全球 z0–2 + 西藏辖区 z3–6 ----------
const MERC_TILES = []
for (let z = 0; z <= 2; z += 1) for (let x = 0; x < 1 << z; x += 1) for (let y = 0; y < 1 << z; y += 1) MERC_TILES.push([z, x, y])
// 影像覆盖必须包住地形加密区（约 lon 80–100°E、lat 20–45°N），否则拉近相机时
// 地形有瓦、影像缺瓦 → Cesium 取到透明瓦 → 画面退回地球底色（真机踩过的"拉近变平"）。
for (const [z, xs, ys] of [
  [3, [5, 6], [2, 3]],
  [4, [11, 12], [5, 6, 7]],
  [5, [22, 23, 24, 25], [11, 12, 13, 14]],
  [6, [45, 46, 47, 48, 49, 50], [22, 23, 24, 25, 26, 27, 28]],
  [7, [92, 93, 94, 95, 96, 97, 98, 99], [46, 47, 48, 49, 50, 51, 52, 53, 54]],
])
  for (const x of xs) for (const y of ys) MERC_TILES.push([z, x, y])

// ---------- Geographic 地形瓦片清单（与合成样本同一覆盖口径，行号自北向南） ----------
// 每一级的范围都必须覆盖下一级的父瓦片，否则 Cesium 会请求缺失的祖先瓦片。
// Geographic 父瓦片：x_parent=floor(x/2)，y_parent=floor(y/2)（行列均自北/西起算）。
const GEO_RANGES = [
  [0, 0, 1, 0, 0],
  [1, 0, 3, 0, 1],
  [2, 0, 7, 0, 3],
  [3, 11, 12, 2, 3],
  [4, 22, 25, 4, 6],
  [5, 45, 50, 8, 12],
  [6, 90, 100, 16, 24],
  [7, 186, 196, 38, 45],
]

// ---------- PNG 解码（terrarium 是 RGB8，五种 scanline filter 都要认） ----------
function crc32(buf) {
  let c
  const table = crc32.table || (crc32.table = (() => {
    const t = new Int32Array(256)
    for (let n = 0; n < 256; n += 1) {
      c = n
      for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
      t[n] = c
    }
    return t
  })())
  let crc = -1
  for (let i = 0; i < buf.length; i += 1) crc = (crc >>> 8) ^ table[(crc ^ buf[i]) & 0xff]
  return (crc ^ -1) >>> 0
}

function decodePng(buf) {
  let o = 8
  let width = 0
  let height = 0
  let colorType = 0
  const idat = []
  while (o < buf.length) {
    const len = buf.readUInt32BE(o)
    const tag = buf.toString('ascii', o + 4, o + 8)
    const data = buf.subarray(o + 8, o + 8 + len)
    if (tag === 'IHDR') {
      width = data.readUInt32BE(0)
      height = data.readUInt32BE(4)
      colorType = data[9]
    } else if (tag === 'IDAT') idat.push(data)
    o += 12 + len
  }
  if (colorType !== 2) throw new Error(`terrarium 应为 RGB8，实际 colorType=${colorType}`)
  const raw = zlib.inflateSync(Buffer.concat(idat))
  const stride = width * 3
  const out = Buffer.alloc(height * stride)
  let p = 0
  for (let y = 0; y < height; y += 1) {
    const filter = raw[p]
    p += 1
    for (let x = 0; x < stride; x += 1) {
      const cur = raw[p + x]
      const a = x >= 3 ? out[y * stride + x - 3] : 0
      const b = y > 0 ? out[(y - 1) * stride + x] : 0
      const c = x >= 3 && y > 0 ? out[(y - 1) * stride + x - 3] : 0
      let v
      switch (filter) {
        case 0: v = cur; break
        case 1: v = cur + a; break
        case 2: v = cur + b; break
        case 3: v = cur + ((a + b) >> 1); break
        case 4: {
          const pp = a + b - c
          const pa = Math.abs(pp - a)
          const pb = Math.abs(pp - b)
          const pc = Math.abs(pp - c)
          v = cur + (pa <= pb && pa <= pc ? a : pb <= pc ? b : c)
          break
        }
        default: throw new Error(`未知 PNG filter ${filter}`)
      }
      out[y * stride + x] = v & 0xff
    }
    p += stride
  }
  return { width, height, pixels: out }
}

function encodePng(width, height, pixels, channels = 3) {
  const stride = width * channels
  const rows = Buffer.alloc(height * (stride + 1))
  const withAlpha = channels === 4
  for (let y = 0; y < height; y += 1) {
    const row = y * (stride + 1)
    const from = y * stride
    rows[row] = withAlpha ? 1 : 0
    pixels.copy(rows, row + 1, from, from + stride)
    if (withAlpha) {
      for (let x = channels; x < stride; x += 1) {
        rows[row + 1 + x] = pixels[from + x] - pixels[from + x - channels]
      }
    }
  }
  const chunk = (tag, data) => {
    const head = Buffer.alloc(8)
    head.writeUInt32BE(data.length, 0)
    head.write(tag, 4, 'ascii')
    const crc = Buffer.alloc(4)
    crc.writeUInt32BE(crc32(Buffer.concat([Buffer.from(tag, 'ascii'), data])), 0)
    return Buffer.concat([head, data, crc])
  }
  const ihdr = Buffer.alloc(13)
  ihdr.writeUInt32BE(width, 0)
  ihdr.writeUInt32BE(height, 4)
  ihdr[8] = 8
  ihdr[9] = withAlpha ? 6 : 2
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr),
    chunk('IDAT', zlib.deflateSync(rows, { level: 6 })),
    chunk('IEND', Buffer.alloc(0)),
  ])
}

// ---------- 源瓦片加载 + 双线性高程采样 ----------
const tiles = new Map()
async function fetchMercTile(key) {
  const url = `${SRC}/${key}.png`
  let failure
  for (let attempt = 0; attempt < 4; attempt += 1) {
    try {
      const response = await fetch(url, {
        headers: { connection: 'close' },
        signal: AbortSignal.timeout(12_000),
      })
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      return Buffer.from(await response.arrayBuffer())
    } catch (error) {
      failure = error
    }
  }
  throw new Error(`源瓦片 ${key} 下载失败`, { cause: failure })
}

async function loadMerc(z, x, y) {
  const key = `${z}/${x}/${y}`
  if (tiles.has(key)) return tiles.get(key)
  const file = join(cache, `${z}-${x}-${y}.png`)
  let buf
  if (existsSync(file)) buf = readFileSync(file)
  else {
    if (offline) throw new Error(`--offline 缺缓存瓦片 ${key}`)
    buf = await fetchMercTile(key)
    writeFileSync(file, buf)
  }
  const { pixels } = decodePng(buf)
  const h = new Float32Array(PNG * PNG)
  for (let i = 0; i < h.length; i += 1) h[i] = pixels[i * 3] * 256 + pixels[i * 3 + 1] + pixels[i * 3 + 2] / 256 - 32768
  tiles.set(key, h)
  return h
}

const mercX = (lon, z) => ((lon + 180) / 360) * (1 << z)
const mercY = (lat, z) => {
  const s = Math.sin((lat * Math.PI) / 180)
  return (0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)) * (1 << z)
}
const mercLat = (row, z) => (Math.atan(Math.sinh(Math.PI * (1 - (2 * row) / (1 << z)))) * 180) / Math.PI
const BASEMAP_MAX_ZOOM = 8

function mercTilesForBounds({ west, south, east, north }, z, margin = 0) {
  const result = []
  for (let x = Math.floor(mercX(west, z)) - margin; x <= Math.floor(mercX(east, z)) + margin; x += 1) {
    for (let y = Math.floor(mercY(north, z)) - margin; y <= Math.floor(mercY(south, z)) + margin; y += 1) {
      result.push([z, x, y])
    }
  }
  return result
}

async function loadMercTiles(requests) {
  for (let start = 0; start < requests.length; start += 6) {
    await Promise.all(requests.slice(start, start + 6).map(([z, x, y]) => loadMerc(z, x, y)))
  }
}

function sampleAt(lon, lat, z) {
  const clamped = Math.max(-85, Math.min(85, lat))
  const fx = mercX(lon, z)
  const fy = mercY(clamped, z)
  const x = Math.floor(fx)
  const y = Math.floor(fy)
  const h = tiles.get(`${z}/${x}/${y}`)
  if (!h) return null
  const gx = (fx - x) * PNG - 0.5
  const gy = (fy - y) * PNG - 0.5
  const x0 = Math.floor(gx)
  const y0 = Math.floor(gy)
  const tx = gx - x0
  const ty = gy - y0
  const at = (xx, yy) => {
    if (xx >= 0 && xx < PNG && yy >= 0 && yy < PNG) return h[yy * PNG + xx]
    const dx = Math.floor(xx / PNG)
    const dy = Math.floor(yy / PNG)
    const neighbor = tiles.get(`${z}/${x + dx}/${y + dy}`)
    if (neighbor) return neighbor[(yy - dy * PNG) * PNG + xx - dx * PNG]
    return h[Math.max(0, Math.min(PNG - 1, yy)) * PNG + Math.max(0, Math.min(PNG - 1, xx))]
  }
  return (
    at(x0, y0) * (1 - tx) * (1 - ty) +
    at(x0 + 1, y0) * tx * (1 - ty) +
    at(x0, y0 + 1) * (1 - tx) * ty +
    at(x0 + 1, y0 + 1) * tx * ty
  )
}
function sample(lon, lat) {
  for (let z = 7; z >= 0; z -= 1) {
    const v = sampleAt(lon, lat, z)
    if (v !== null) return v
  }
  return 0
}
/** 影像设色/求导一律优先取加密层：粗层瓦片也画出加密层的坡面细节，层与层之间才不会出现亮暗缝。 */
function sampleDeep(lon, lat) {
  for (let z = BASEMAP_MAX_ZOOM; z >= 0; z -= 1) {
    const v = sampleAt(lon, lat, z)
    if (v !== null) return v
  }
  return 0
}

// ---------- quantized-mesh 编码 ----------
const zigzag = (v) => (v << 1) ^ (v >> 31)
function deltaEncode(values) {
  let prev = 0
  return values.map((v) => {
    const c = zigzag(v - prev)
    prev = v
    return c
  })
}
function hwmEncode(indices) {
  let highest = 0
  return indices.map((v) => {
    if (v === highest) {
      highest += 1
      return 0
    }
    if (v < highest) return highest - v
    throw new Error(`索引 ${v} 超前 high-water mark ${highest}`)
  })
}
const rad = (d) => (d * Math.PI) / 180
function ecef(lon, lat, h) {
  const a = 6378137
  const b = 6356752.314245
  const e2 = 1 - (b * b) / (a * a)
  const sl = Math.sin(rad(lat))
  const cl = Math.cos(rad(lat))
  const n = a / Math.sqrt(1 - e2 * sl * sl)
  return [(n + h) * cl * Math.cos(rad(lon)), (n + h) * cl * Math.sin(rad(lon)), (n * (1 - e2) + h) * sl]
}
const stabilize = (v) => Math.round(v * 1e4) / 1e4
function octEncode(nx, ny, nz) {
  const sum = Math.abs(nx) + Math.abs(ny) + Math.abs(nz) || 1
  let u = nx / sum
  let v = ny / sum
  if (nz < 0) {
    const ou = u
    u = (1 - Math.abs(v)) * (ou >= 0 ? 1 : -1)
    v = (1 - Math.abs(ou)) * (v >= 0 ? 1 : -1)
  }
  const byte = (x) => Math.max(-127, Math.min(127, Math.round(x * 127))) & 0xff
  return [byte(u), byte(v)]
}

function bakeGeoTile(z, x, y) {
  const n = 1 << z
  const cols = 1 << (z + 1) // Geographic 方案：level z 是 2^(z+1) × 2^z
  const west = (x / cols) * 360 - 180
  const east = ((x + 1) / cols) * 360 - 180
  const north = 90 - (y / n) * 180
  const south = 90 - ((y + 1) / n) * 180
  const grid = z >= 5 ? 65 : 33
  const stepLon = (east - west) / (grid - 1)
  const stepLat = (north - south) / (grid - 1)
  const skirt = 8000 / n
  const pos = []
  for (let row = 0; row < grid; row += 1) {
    for (let col = 0; col < grid; col += 1) {
      const lon = west + col * stepLon
      const lat = north - row * stepLat
      pos.push([col / (grid - 1), 1 - row / (grid - 1), sample(lon, lat)])
    }
  }
  const gi = (r, c) => r * grid + c
  const sides = {
    west: Array.from({ length: grid }, (_, r) => gi(r, 0)),
    east: Array.from({ length: grid }, (_, r) => gi(r, grid - 1)),
    north: Array.from({ length: grid }, (_, c) => gi(0, c)),
    south: Array.from({ length: grid }, (_, c) => gi(grid - 1, c)),
  }
  let cursor = grid * grid
  const skirtStart = {}
  for (const side of ['west', 'south', 'east', 'north']) {
    skirtStart[side] = cursor
    for (const vid of sides[side]) {
      const [u, v, h] = pos[vid]
      pos.push([u, v, h - skirt])
      cursor += 1
    }
  }
  const raw = []
  for (let r = 0; r < grid - 1; r += 1) {
    for (let c = 0; c < grid - 1; c += 1) {
      const a = gi(r, c)
      const b = gi(r, c + 1)
      const cc = gi(r + 1, c)
      const d = gi(r + 1, c + 1)
      // 逆时针（u 东 / v 北参数面）：Cesium 球面背面剔除只认这一支，顺时针整片顶面会被剔掉。
      raw.push([a, cc, b], [b, cc, d])
    }
  }
  for (const [side, ids] of Object.entries(sides)) {
    const base = skirtStart[side]
    for (let k = 0; k < ids.length - 1; k += 1) {
      raw.push([ids[k], ids[k + 1], base + k], [ids[k + 1], base + k + 1, base + k])
    }
  }
  const remap = new Map()
  const tris = []
  for (const tri of raw) {
    for (const vid of tri) {
      if (!remap.has(vid)) remap.set(vid, remap.size)
      tris.push(remap.get(vid))
    }
  }
  const ordered = [...remap.keys()].sort((a, b) => remap.get(a) - remap.get(b)).map((vid) => pos[vid])
  const hs = ordered.map((p) => p[2])
  const minH = Math.min(...hs)
  const maxH = Math.max(...hs)
  const span = maxH - minH || 1
  const uq = ordered.map((p) => Math.max(0, Math.min(MAX_SHORT, Math.round(p[0] * MAX_SHORT))))
  const vq = ordered.map((p) => Math.max(0, Math.min(MAX_SHORT, Math.round(p[1] * MAX_SHORT))))
  const hq = ordered.map((p) => Math.max(0, Math.min(MAX_SHORT, Math.round((p[2] - minH) * (MAX_SHORT / span)))))
  const vc = ordered.length
  const centerLon = (west + east) / 2
  const centerLat = (south + north) / 2
  const mPerDegLon = 111320 * Math.cos(rad(centerLat))
  const normals = []
  const cl = Math.cos(rad(centerLat))
  const sl = Math.sin(rad(centerLat))
  const co = Math.cos(rad(centerLon))
  const so = Math.sin(rad(centerLon))
  const eastV = [-so, co, 0]
  const northV = [-sl * co, -sl * so, cl]
  const upV = [cl * co, cl * so, sl]
  const dStep = Math.max(1e-6, stepLon / 2)
  for (const [u, v] of ordered) {
    const lon = west + u * (east - west)
    const lat = south + v * (north - south)
    const dhdx = (sample(lon + dStep, lat) - sample(lon - dStep, lat)) / (2 * dStep * mPerDegLon)
    const dhdy = (sample(lon, lat + dStep) - sample(lon, lat - dStep)) / (2 * dStep * 110574)
    const len = Math.sqrt(dhdx * dhdx + dhdy * dhdy + 1)
    const enu = [-dhdx / len, -dhdy / len, 1 / len]
    normals.push(octEncode(
      enu[0] * eastV[0] + enu[1] * northV[0] + enu[2] * upV[0],
      enu[0] * eastV[1] + enu[1] * northV[1] + enu[2] * upV[1],
      enu[0] * eastV[2] + enu[1] * northV[2] + enu[2] * upV[2],
    ))
  }
  const cart = ordered.map((p) => ecef(west + p[0] * (east - west), south + p[1] * (north - south), p[2]))
  const center = ecef(centerLon, centerLat, (minH + maxH) / 2)
  const radius = Math.max(...cart.map((p) => Math.hypot(p[0] - center[0], p[1] - center[1], p[2] - center[2])))
  const horizon = ecef(centerLon, centerLat, maxH)
  const edgeIds = (ids) => [...new Set(ids.map((vid) => remap.get(vid)))].sort((a, b) => a - b)

  const parts = []
  const f64 = (v) => {
    const b = Buffer.alloc(8)
    b.writeDoubleLE(stabilize(v), 0)
    return b
  }
  const f32 = (v) => {
    const b = Buffer.alloc(4)
    b.writeFloatLE(v, 0)
    return b
  }
  const u32 = (v) => {
    const b = Buffer.alloc(4)
    b.writeUInt32LE(v, 0)
    return b
  }
  const u16 = (values) => {
    const b = Buffer.alloc(values.length * 2)
    values.forEach((v, i) => b.writeUInt16LE(v, i * 2))
    return b
  }
  parts.push(f64(center[0]), f64(center[1]), f64(center[2]), f32(minH), f32(maxH))
  parts.push(f64(center[0]), f64(center[1]), f64(center[2]), f64(radius))
  parts.push(f64(horizon[0]), f64(horizon[1]), f64(horizon[2]))
  parts.push(u32(vc), u16([...deltaEncode(uq), ...deltaEncode(vq), ...deltaEncode(hq)]))
  parts.push(u32(tris.length / 3), u16(hwmEncode(tris)))
  for (const ids of [sides.west, sides.south, sides.east, sides.north]) {
    const e = edgeIds(ids)
    parts.push(u32(e.length), u16(e))
  }
  const nrm = Buffer.alloc(vc * 2)
  normals.forEach(([a, b], i) => {
    nrm[i * 2] = a
    nrm[i * 2 + 1] = b
  })
  parts.push(Buffer.from([1]), u32(nrm.length), nrm)
  return { bytes: Buffer.concat(parts), minH, maxH }
}

// ---------- 影像：分层设色 × 山体阴影 ----------
const localStops = gyirong.stops.map(([elevation, hex]) => [elevation, Buffer.from(hex.slice(1), 'hex')])
const STOPS = [
  [-6000, [25, 43, 57]],
  [-1, [46, 65, 75]],
  [0, [47, 79, 69]],
  ...localStops,
]
function hypsometric(h, stops = STOPS) {
  if (h <= stops[0][0]) return stops[0][1]
  for (let i = 1; i < stops.length; i += 1) {
    if (h <= stops[i][0]) {
      const [h0, c0] = stops[i - 1]
      const [h1, c1] = stops[i]
      const t = (h - h0) / (h1 - h0)
      return [0, 1, 2].map((k) => Math.round(c0[k] + (c1[k] - c0[k]) * t))
    }
  }
  return stops[stops.length - 1][1]
}

function terrainShade(slopeX, slopeY) {
  const length = Math.hypot(slopeX, slopeY, 1)
  const primary = Math.max(0, (0.55 * slopeX - 0.48 * slopeY + 0.68) / length)
  const fill = Math.max(0, (-0.46 * slopeX - 0.30 * slopeY + 0.84) / length)
  return 0.54 + 0.52 * primary + 0.12 * fill
}

async function bakeGyirongRelief(config, label) {
  const { bounds: { west, south, east, north }, width, height, sourceZoom: z, contourIntervalM } = config
  const downloads = mercTilesForBounds(config.bounds, z, 1)
  await loadMercTiles(downloads)

  // 图像按等间距经纬度采样，与 Cesium GeographicTilingScheme 对齐。
  const dLon = (east - west) / width
  const dLat = (north - south) / height
  const rows = height + 2
  const columns = width + 2
  const dem = new Float32Array(rows * columns)
  for (let y = 0; y < rows; y += 1) {
    const lat = north - (y - 0.5) * dLat
    for (let x = 0; x < columns; x += 1) {
      const elevation = sampleAt(west + (x - 0.5) * dLon, lat, z)
      if (elevation === null) throw new Error('吉隆高程源瓦片覆盖不完整')
      dem[y * columns + x] = elevation
    }
  }

  const pixels = Buffer.alloc(width * height * 4)
  for (let y = 1; y <= height; y += 1) {
    const lat = north - (y - 0.5) * dLat
    const dx = 2 * dLon * 111_320 * Math.cos(rad(lat))
    const dy = 2 * dLat * 110_574
    for (let x = 1; x <= width; x += 1) {
      const i = y * columns + x
      const elevation = dem[i]
      const dxH = dem[i + 1] - dem[i - 1]
      const dyH = dem[i - columns] - dem[i + columns]
      let shaded = terrainShade(dxH / dx, dyH / dy)
      if (contourIntervalM) {
        const index = Math.round(elevation / contourIntervalM)
        const distance = Math.abs(elevation - index * contourIntervalM)
        const thickness = Math.max(1, Math.hypot(dxH, dyH) * (index % 2 === 0 ? 0.45 : 0.28))
        const contour = Math.max(0, 1 - distance / thickness)
        shaded *= 1 - contour * (index % 2 === 0 ? 0.26 : 0.13)
      }
      const base = hypsometric(elevation)
      const pixel = ((y - 1) * width + x - 1) * 4
      for (let c = 0; c < 3; c += 1) pixels[pixel + c] = Math.min(255, Math.round(base[c] * shaded))
      // 离开局部覆盖区时与全域底图平滑衔接。
      const edge = Math.min(1, Math.min(x - 1, width - x, y - 1, height - y) / 92)
      pixels[pixel + 3] = Math.round(255 * edge * edge * (3 - 2 * edge))
    }
  }
  const png = encodePng(width, height, pixels, 4)
  writeFileSync(join(pub, config.url), png)
  console.log(`${label}：${width}×${height}，${downloads.length} 张高程源瓦片，${png.length} 字节`)
}

function renderMercTile(z, x, y) {
  const n = 1 << z
  const west = (x / n) * 360 - 180
  const span = 360 / n
  const rgb = Buffer.alloc(PNG * PNG * 3)
  const dLon = span / PNG
  // 各层使用同一坡面采样距离，切换层级时保持光向和平均亮度一致。
  const sLon = 0.018
  const sLat = sLon
  for (let py = 0; py < PNG; py += 1) {
    const lat = mercLat(y + (py + 0.5) / PNG, z)
    for (let px = 0; px < PNG; px += 1) {
      const lon = west + (px + 0.5) * dLon
      const h = sampleDeep(lon, lat)
      const dhdx = (sampleDeep(lon + sLon, lat) - sampleDeep(lon - sLon, lat)) / (2 * sLon * 111320 * Math.cos(rad(lat)))
      const dhdy = (sampleDeep(lon, lat + sLat) - sampleDeep(lon, lat - sLat)) / (2 * sLat * 110574)
      const base = hypsometric(h)
      const k = h <= 0 ? 0.92 : terrainShade(dhdx, dhdy)
      const i = (py * PNG + px) * 3
      rgb[i] = Math.min(255, Math.round(base[0] * k))
      rgb[i + 1] = Math.min(255, Math.round(base[1] * k))
      rgb[i + 2] = Math.min(255, Math.round(base[2] * k))
    }
  }
  return encodePng(PNG, PNG, rgb)
}

// ---------- PMTiles v3 ----------
function varint(value) {
  const out = []
  for (;;) {
    const byte = value & 0x7f
    value >>= 7
    if (value) out.push(byte | 0x80)
    else {
      out.push(byte)
      return Buffer.from(out)
    }
  }
}
function hilbertRotate(n, x, y, rx, ry) {
  if (ry === 0) return rx ? [n - 1 - y, n - 1 - x] : [y, x]
  return [x, y]
}
function tileId(z, x, y) {
  let acc = ((1 << z) * (1 << z) - 1) / 3
  let tx = x
  let ty = y
  for (let a = z - 1; a >= 0; a -= 1) {
    const s = 1 << a
    const rx = tx & s
    const ry = ty & s
    acc += ((3 * rx) ^ ry) * (1 << a)
    ;[tx, ty] = hilbertRotate(s, tx, ty, rx, ry)
  }
  return acc
}
function encodeDirectory(entries) {
  const ordered = [...entries].sort((a, b) => a.id - b.id)
  const parts = [varint(ordered.length)]
  let last = 0
  for (const e of ordered) {
    parts.push(varint(e.id - last))
    last = e.id
  }
  for (const e of ordered) parts.push(varint(e.run))
  for (const e of ordered) parts.push(varint(e.length))
  let prevEnd = 0
  ordered.forEach((e, i) => {
    parts.push(varint(i > 0 && e.offset === prevEnd ? 0 : e.offset + 1))
    prevEnd = e.offset + e.length
  })
  return Buffer.concat(parts)
}
function pmtilesHeader(o) {
  const h = Buffer.alloc(127)
  h.write('PMTiles', 0, 'ascii')
  h[7] = 3
  h.writeBigUInt64LE(BigInt(o.rootOffset), 8)
  h.writeBigUInt64LE(BigInt(o.rootLength), 16)
  h.writeBigUInt64LE(BigInt(o.metadataOffset), 24)
  h.writeBigUInt64LE(BigInt(o.metadataLength), 32)
  h.writeBigUInt64LE(0n, 40)
  h.writeBigUInt64LE(0n, 48)
  h.writeBigUInt64LE(BigInt(o.dataOffset), 56)
  h.writeBigUInt64LE(BigInt(o.dataLength), 64)
  h.writeBigUInt64LE(BigInt(o.numAddressed), 72)
  h.writeBigUInt64LE(BigInt(o.numEntries), 80)
  h.writeBigUInt64LE(BigInt(o.numContents), 88)
  h[96] = 1
  h[97] = 0
  h[98] = 0
  h[99] = 2
  h[100] = o.minZoom
  h[101] = o.maxZoom
  h.writeInt32LE(Math.ceil(-180 * 1e7), 102)
  h.writeInt32LE(Math.ceil(-85.05112877980659 * 1e7), 106)
  h.writeInt32LE(Math.floor(180 * 1e7), 110)
  h.writeInt32LE(Math.floor(85.05112877980659 * 1e7), 114)
  h[118] = o.centerZoom
  h.writeInt32LE(Math.round(o.centerLon * 1e7), 119)
  h.writeInt32LE(Math.round(o.centerLat * 1e7), 123)
  return h
}

// ---------- 主流程 ----------
if (process.argv.includes('--gyirong')) {
  await bakeGyirongRelief(gyirong, '吉隆案例地形')
  await bakeGyirongRelief(gyirong.surroundings, '吉隆周边地形')
  process.exit(0)
}

MERC_TILES.push(...mercTilesForBounds({ west: 76, south: 24, east: 101, north: 39 }, BASEMAP_MAX_ZOOM))
console.log(`源瓦片 ${MERC_TILES.length} 张（缓存 ${cache}）`)
await loadMercTiles(MERC_TILES)
console.log('源瓦片就绪')

if (!process.argv.includes('--basemap-only')) bakeTerrain()
bakeBasemap()

function bakeTerrain() {
  const terrainDir = join(pub, 'terrain-real')
  let terrainBytes = 0
  let terrainCount = 0
  let lo = Infinity
  let hi = -Infinity
  for (const [z, x0, x1, y0, y1] of GEO_RANGES) {
    for (let x = x0; x <= x1; x += 1) {
      for (let y = y0; y <= y1; y += 1) {
        const { bytes, minH, maxH } = bakeGeoTile(z, x, y)
        mkdirSync(join(terrainDir, String(z), String(x)), { recursive: true })
        writeFileSync(join(terrainDir, String(z), String(x), `${y}.terrain`), bytes)
        terrainBytes += bytes.length
        terrainCount += 1
        if (z >= 5) {
          lo = Math.min(lo, minH)
          hi = Math.max(hi, maxH)
        }
      }
    }
  }
  const available = GEO_RANGES.map(([z, x0, x1, y0, y1]) => {
    const per = 1 << z
    return [{ startX: x0, endX: x1, startY: per - 1 - y1, endY: per - 1 - y0 }]
  })
  writeFileSync(
    join(terrainDir, 'layer.json'),
    `${JSON.stringify(
      {
        format: 'quantized-mesh-1.0',
        version: '1.0',
        scheme: 'slippyMap',
        projection: 'EPSG:4326',
        tiles: ['{z}/{x}/{y}.terrain'],
        extensions: ['octvertexnormals'],
        minzoom: 0,
        maxzoom: 7,
        bounds: [-180, -90, 180, 90],
        attribution: '高程 © Mapzen / AWS Terrain Tiles（SRTM、GEBCO、GMTED 等）· 离线烘焙 quantized-mesh',
        available,
        generator: 'frontend/scripts/bake-real-relief.mjs',
      },
      null,
      2,
    )}\n`,
  )
  console.log(`terrain-real: ${terrainCount} 瓦 ${terrainBytes} 字节，加密区高程 ${lo.toFixed(0)}..${hi.toFixed(0)} m`)
}

function bakeBasemap() {
  const entries = []
  const blobs = []
  let offset = 0
  for (const [z, x, y] of [...MERC_TILES].sort((a, b) => tileId(...a) - tileId(...b))) {
    const png = renderMercTile(z, x, y)
    entries.push({ id: tileId(z, x, y), offset, length: png.length, run: 1 })
    blobs.push(png)
    offset += png.length
}
const dir = encodeDirectory(entries)
const metadata = Buffer.from(
  JSON.stringify({
    name: 'AEGIS 真实高程山体阴影底图',
    attribution: '高程 © Mapzen / AWS Terrain Tiles（SRTM、GEBCO、GMTED 等）',
    description: 'terrarium 真实 DEM → 分层设色 × 山体阴影，离线烘焙 PMTiles v3 栅格归档',
  }),
  'utf8',
)
const dataOffset = 127 + dir.length + metadata.length
const header = pmtilesHeader({
  rootOffset: 127,
  rootLength: dir.length,
  metadataOffset: 127 + dir.length,
  metadataLength: metadata.length,
  dataOffset,
  dataLength: offset,
  numAddressed: entries.length,
  numEntries: entries.length,
  numContents: entries.length,
  minZoom: 0,
  maxZoom: BASEMAP_MAX_ZOOM,
  centerZoom: 4,
  centerLon: 88.5,
  centerLat: 31,
})
writeFileSync(join(pub, 'basemaps', 'relief.pmtiles'), Buffer.concat([header, dir, metadata, ...blobs]))
console.log(`relief.pmtiles: ${entries.length} 瓦 ${127 + dir.length + metadata.length + offset} 字节`)
}
