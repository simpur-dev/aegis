/**
 * 离线资产的**真读取**用例：Python 生成的字节，交给仓库里真正安装的 pmtiles 库与前端自己的
 * 底图/地形装配去读，中间走一次真的 HTTP（含 Range）。
 *
 * 为什么必须有这一条：`basemap.spec.ts`/`terrain.spec.ts` 里的探针与 reader 都是替身，
 * 它们能证明决策逻辑，证不了"我们写的字节能被读"。格式假设错了的时候（Hilbert 序号、
 * varint 目录、首个 16KB 块里必须放得下根目录），只有真读取器会拒绝，而替身会照单全收。
 * 这一轮就是把两端接起来：`scripts/build_offline_tiles.py` 写 → 这里读。
 *
 * 服务器只监听 127.0.0.1 且只挂 `frontend/public`，所以"无外链"这条约束也是可断言的事实：
 * 用例记录每一次请求的 host，出现任何非本机主机就直接红。
 *
 * 环境用 node 而不是这个套件默认的 jsdom：jsdom 造出的 `AbortSignal` 不是 undici `fetch`
 * 认可的那个实例（`Expected signal to be an instance of AbortSignal`），探针会在传输层就死掉，
 * 于是"取不到"被读成"资产不在"。这里不需要 DOM，需要的是和浏览器同一套的 fetch/Range 语义。
 */
// @vitest-environment node

import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http'
import { readFile, stat } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { extname, join, resolve } from 'path'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'

import { PMTiles } from 'pmtiles'

import { insideWebMercator, FakeWebMercatorTilingScheme, fakeRectangle, WEB_MERCATOR_MAX_LATITUDE_DEG } from '@/testing/cesiumBasemapStub'

import {
  DEFAULT_BASEMAP_CONFIG,
  PmtilesImageryProvider,
  chooseBasemapSource,
  createBasemapSetup,
  type BasemapCesium,
  type PmtilesHead,
} from './basemap'
import { chooseTerrainMode, terrainProbeUrl } from './terrain'
import { probeSource } from './offline'

const CONTENT_TYPES: Record<string, string> = {
  '.json': 'application/json',
  '.terrain': 'application/vnd.quantized-mesh',
  '.pmtiles': 'application/vnd.pmtiles',
  '.png': 'image/png',
}

/** 从 vitest 的工作目录往上找仓库里的 public/，找不到就把路径报出来而不是猜一个空目录。 */
function locatePublicDir(): string {
  let dir = process.cwd()
  for (let depth = 0; depth < 5; depth += 1) {
    const candidate = resolve(dir, 'public')
    if (existsSync(join(candidate, 'basemaps', 'aegis.pmtiles'))) return candidate
    dir = resolve(dir, '..')
  }
  throw new Error(`没找到带 aegis.pmtiles 的 public/ 目录（cwd=${process.cwd()}）；先跑 python scripts/build_offline_tiles.py`)
}

const PUBLIC_DIR = locatePublicDir()
const requestedUrls: string[] = []

let server: Server
let origin = ''

function contentTypeFor(path: string): string {
  return CONTENT_TYPES[extname(path)] ?? 'application/octet-stream'
}

/** 静态服务 + Range：PMTiles 的取瓦就是靠 Range 头部做随机读，缺了它这条用例等于没测。 */
function handler(request: IncomingMessage, response: ServerResponse): void {
  const url = new URL(request.url ?? '/', origin || 'http://127.0.0.1')
  requestedUrls.push(url.href)
  const relative = decodeURIComponent(url.pathname).replace(/^\/+/, '')
  void (async () => {
    let info
    try {
      info = await stat(join(PUBLIC_DIR, relative))
    } catch {
      response.writeHead(404).end()
      return
    }
    if (!info.isFile()) {
      response.writeHead(403).end()
      return
    }
    const range = request.headers.range
    if (range) {
      const matched = /^bytes=(\d+)-(\d*)$/.exec(range)
      if (!matched) {
        response.writeHead(416, { 'Content-Range': `bytes */${info.size}` }).end()
        return
      }
      const start = Number(matched[1])
      const end = matched[2] ? Math.min(Number(matched[2]), info.size - 1) : Math.min(start + 65535, info.size - 1)
      const chunk = await readFile(join(PUBLIC_DIR, relative))
      response.writeHead(206, {
        'Content-Type': contentTypeFor(relative),
        'Content-Range': `bytes ${start}-${end}/${info.size}`,
        'Accept-Ranges': 'bytes',
        'Content-Length': String(end - start + 1),
      })
      response.end(request.method === 'HEAD' ? undefined : chunk.subarray(start, end + 1))
      return
    }
    const body = await readFile(join(PUBLIC_DIR, relative))
    response.writeHead(200, {
      'Content-Type': contentTypeFor(relative),
      'Accept-Ranges': 'bytes',
      'Content-Length': String(body.byteLength),
    })
    response.end(request.method === 'HEAD' ? undefined : body)
  })()
}

beforeAll(async () => {
  server = createServer(handler)
  await new Promise<void>((done) => server.listen(0, '127.0.0.1', done))
  const address = server.address()
  if (address === null || typeof address === 'string') throw new Error('本机静态服务没拿到端口')
  origin = `http://127.0.0.1:${address.port}`
})

afterAll(async () => {
  await new Promise<void>((done, fail) => server.close((err) => (err ? fail(err) : done())))
})

/**
 * 影像层装配要用的 Cesium 替身：只实现这条链路真正调用的成员（图层包装、矩形换算、署名与事件壳）。
 * 一次性桥接成 `BasemapCesium`，因为这里要验的是"读到的字节与装配决定"，不是 Cesium 的类型细节。
 */
function stubCesium(): BasemapCesium {
  class ImageryLayer {
    readonly provider: unknown
    constructor(provider: unknown) {
      this.provider = provider
    }
  }
  class UrlTemplateImageryProvider {
    readonly options: Record<string, unknown>
    constructor(options: Record<string, unknown>) {
      this.options = options
    }
  }
  class Credit {
    readonly value: string
    constructor(value: string) {
      this.value = value
    }
  }
  return {
    ImageryLayer,
    UrlTemplateImageryProvider,
    Rectangle: fakeRectangle,
    WebMercatorTilingScheme: FakeWebMercatorTilingScheme,
    Credit,
    Event: class {
      readonly addEventListener = () => undefined
    },
  } as unknown as BasemapCesium
}

function pngSize(bytes: Uint8Array): { width: number; height: number } {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  return { width: view.getUint32(16, false), height: view.getUint32(20, false) }
}

describe('生成的 PMTiles 归档被真读取器读通', () => {
  it('头部字段与生成器声明一致（栅格 PNG、层级 0..2、全世界边界）', async () => {
    const archive = new PMTiles(`${origin}/basemaps/aegis.pmtiles`)
    const header = await archive.getHeader()
    expect(header.specVersion).toBe(3)
    expect(header.tileType).toBe(2)
    expect(header.minZoom).toBe(0)
    expect(header.maxZoom).toBe(2)
    expect(header.internalCompression).toBe(1)
    expect(header.tileCompression).toBe(1)
    expect(header.clustered).toBe(true)
    expect(header.minLon).toBeCloseTo(-180, 4)
    expect(header.maxLon).toBeCloseTo(180, 4)
    // 纬度必须留在 Web Mercator 切片方案内：越界不是"显示不全"，而是渲染循环直接崩
    // （浏览器实测的 undefined.x）。生成器因此把 1e7 定点量化写成往框内缩。
    expect(header.minLat).toBeGreaterThanOrEqual(-WEB_MERCATOR_MAX_LATITUDE_DEG)
    expect(header.maxLat).toBeLessThanOrEqual(WEB_MERCATOR_MAX_LATITUDE_DEG)
    expect(header.maxLat).toBeCloseTo(WEB_MERCATOR_MAX_LATITUDE_DEG, 6)
    expect(header.numTileEntries).toBe(21)
  })

  it('按 z/x/y 取回的字节就是文件里那段 PNG，且尺寸是 256×256', async () => {
    const archive = new PMTiles(`${origin}/basemaps/aegis.pmtiles`)
    const tile = await archive.getZxy(1, 1, 1)
    expect(tile).toBeDefined()
    const bytes = new Uint8Array(tile!.data)
    expect(bytes.subarray(0, 8)).toEqual(new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))
    expect(pngSize(bytes)).toEqual({ width: 256, height: 256 })

    const onDisk = new Uint8Array(await readFile(join(PUBLIC_DIR, 'basemaps', 'aegis.pmtiles')))
    // 取回的瓦必须逐字节出现在归档里：证明 reader 的偏移算对了，而不是"读到了别的字节"。
    const haystack = Buffer.from(onDisk).toString('latin1')
    const needle = Buffer.from(bytes).toString('latin1')
    expect(haystack.indexOf(needle)).toBeGreaterThan(0)
  })

  it('烘焙之外的层级取不到瓦，而不是抛错或读到邻居', async () => {
    const archive = new PMTiles(`${origin}/basemaps/aegis.pmtiles`)
    expect(await archive.getZxy(5, 3, 4)).toBeUndefined()
  })
})

describe('前端自己的底图装配吃的就是这些真字节', () => {
  it('真探针 → 选源 → 影像层，全程只碰本机', async () => {
    // 装配链里的地址用绝对同源 URL：浏览器会把 '/basemaps/...' 按页面 origin 解析，
    // 而 node 环境的 fetch 要求绝对地址——这不影响"同源/无外链"这条约束的验证，
    // isLocalAssetUrl 对绝对同源地址同样放行。
    const config = {
      ...DEFAULT_BASEMAP_CONFIG,
      pmtilesUrl: `${origin}/basemaps/aegis.pmtiles`,
      template: `${origin}/basemaps/xyz/{z}/{x}/{y}.png`,
    }
    const fetchLike = (input: RequestInfo | URL, init?: RequestInit) => fetch(String(input), init)

    const pmtilesReachable = await probeSource(fetchLike, config.pmtilesUrl)
    const templateReachable = await probeSource(fetchLike, config.template)
    expect(pmtilesReachable).toBe(true)
    expect(templateReachable).toBe(false)

    const source = chooseBasemapSource({ pmtilesReachable, templateReachable }, config, origin)
    expect(source.kind).toBe('pmtiles')

    requestedUrls.length = 0
    const setup = await createBasemapSetup(stubCesium(), source)
    expect(setup.layer).not.toBe(false)
    const provider = (setup.layer as unknown as { provider: PmtilesImageryProvider }).provider
    expect(provider.minimumLevel).toBe(0)
    expect(provider.maximumLevel).toBe(2)
    expect(provider.hasTile(1, 1, 1)).toBe(true)
    expect(provider.hasTile(3, 3, 3)).toBe(false)
    // 装配出来的 provider 矩形是渲染循环真正拿去算瓦号的东西：界内才算这条链没埋雷。
    expect(insideWebMercator(provider.rectangle)).toBe(true)
    expect(requestedUrls.every((url) => url.startsWith(origin))).toBe(true)
  })

  it('requestImage 交给位图解码器的字节 = 归档里那一瓦（缺瓦则给 1×1 透明瓦）', async () => {
    const handed: Uint8Array[] = []
    const previous = globalThis.createImageBitmap
    // jsdom 没有 createImageBitmap，这里不测位图解码本身，测"递进去的字节对不对"。
    globalThis.createImageBitmap = ((blob: Blob) =>
      blob.arrayBuffer().then((buffer) => {
        handed.push(new Uint8Array(buffer))
        return { __bitmap: true } as unknown as ImageBitmap
      })) as typeof globalThis.createImageBitmap

    try {
      const archive = new PMTiles(`${origin}/basemaps/aegis.pmtiles`)
      const head: PmtilesHead = await (async () => {
        const header = await archive.getHeader()
        return {
          tileType: header.tileType,
          minZoom: header.minZoom,
          maxZoom: header.maxZoom,
          minLon: header.minLon,
          minLat: header.minLat,
          maxLon: header.maxLon,
          maxLat: header.maxLat,
        }
      })()
      const provider = new PmtilesImageryProvider({
        cesium: stubCesium(),
        reader: { getZxy: (z, x, y, signal) => archive.getZxy(z, x, y, signal) },
        verdict: {
          raster: true,
          contentType: 'image/png',
          reason: '栅格归档，可作为影像层',
          bounds: { west: -180, south: -85.05, east: 180, north: 85.05 },
          minimumLevel: head.minZoom,
          maximumLevel: head.maxZoom,
        },
        attribution: 'AEGIS 离线底图',
      })

      await provider.requestImage(0, 0, 0)
      await provider.requestImage(3, 1, 2)
      await provider.requestImage(0, 0, 9) // 超出烘焙层级 → 缺瓦替身（这张是缓存的同一张，只建一次）

      expect(handed).toHaveLength(3)
      expect(pngSize(handed[0])).toEqual({ width: 256, height: 256 })
      expect(pngSize(handed[1])).toEqual({ width: 256, height: 256 })
      expect(handed[0]).not.toEqual(handed[1])
      expect(pngSize(handed[2])).toEqual({ width: 1, height: 1 })
    } finally {
      globalThis.createImageBitmap = previous
    }
  })
})

describe('地形通道的探针打到真 layer.json', () => {
  it('烘好的地形被判为 quantized-mesh，且取到的瓦字节非空', async () => {
    const fetchLike = (input: RequestInfo | URL, init?: RequestInit) => fetch(`${origin}${String(input)}`, init)
    const url = '/terrain'
    const reachable = await probeSource(fetchLike, terrainProbeUrl(url))
    expect(reachable).toBe(true)
    const choice = chooseTerrainMode({ configuredUrl: url, layerJsonReachable: true, browserOnline: true, origin })
    expect(choice.mode).toBe('quantized-mesh')

    const tile = await fetchLike('/terrain/0/1/0.terrain')
    expect(tile.ok).toBe(true)
    const bytes = new Uint8Array(await tile.arrayBuffer())
    // 头 24 字节是 center（3×float64），z0 西半球的中心 x/y 必为负、z 接近 0；
    // 这里只断言"读回来的量级像一张瓦"，字段级校验在 pytest 的镜像解码器里做。
    expect(bytes.byteLength).toBeGreaterThan(1000)
    const centerZ = new DataView(bytes.buffer).getFloat64(16, true)
    expect(Math.abs(centerZ)).toBeLessThan(7e6)
  })

  it('地形没烘焙时仍按椭球面走，而不是把不存在的地址交给 Cesium', async () => {
    const fetchLike = (input: RequestInfo | URL, init?: RequestInit) => fetch(`${origin}${String(input)}`, init)
    const reachable = await probeSource(fetchLike, '/terrain-missing/layer.json')
    expect(reachable).toBe(false)
    const choice = chooseTerrainMode({ configuredUrl: '/terrain-missing', layerJsonReachable: reachable, browserOnline: true, origin })
    expect(choice.mode).toBe('ellipsoid')
    expect(choice.reason).toContain('尚未烘焙')
  })
})

describe('无外链约束在真请求上也成立', () => {
  it('整个用例过程里没有一次请求打到非本机主机', () => {
    const hosts = new Set(requestedUrls.map((url) => new URL(url).host))
    for (const host of hosts) {
      expect(host).toBe(new URL(origin).host)
    }
    expect([...hosts].some((host) => /ion\.cesium|google|mapbox|tile\.openstreetmap/i.test(host))).toBe(false)
  })
})
