/**
 * 离线资产的**真读取**用例：Python 生成的字节，交给仓库里真正安装的 pmtiles 库与前端自己的
 * 底图/地形装配去读，中间走一次真的 HTTP（含 Range）。
 *
 * 为什么必须有这一条：`basemap.spec.ts`/`terrain.spec.ts` 里的探针与 reader 都是替身，
 * 它们能证明决策逻辑，证不了"我们写的字节能被读"。格式假设错了的时候（Hilbert 序号、
 * varint 目录、首个 16KB 块里必须放得下根目录），只有真读取器会拒绝，而替身会照单全收。
 * 这一轮就是把两端接起来：`scripts/build_offline_tiles.py` 写 → 这里读。
 *
 * 服务器只监听 127.0.0.1 且只挂 `frontend/public`（`@/testing/localAssetServer`，与
 * `terrain-parse.spec.ts` 同一份实现），所以"无外链"这条约束也是可断言的事实：
 *  fixture 记录每一次请求的 host，出现任何非本机主机就直接红。
 *  缺件应答刻意选 `spa-html` 那一档：`vite preview` 对不存在的瓦回的就是 `200 text/html`
 *  （浏览器实测），比干净的 404 更靠近现场，也更能把"只看状态码"的探针打死。
 *
 * 环境用 node 而不是这个套件默认的 jsdom：jsdom 造出的 `AbortSignal` 不是 undici `fetch`
 * 认可的那个实例（`Expected signal to be an instance of AbortSignal`），探针会在传输层就死掉，
 * 于是"取不到"被读成"资产不在"。这里不需要 DOM，需要的是和浏览器同一套的 fetch/Range 语义。
 */
// @vitest-environment node

import { readFile } from 'node:fs/promises'
import { join } from 'node:path'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'

import { PMTiles } from 'pmtiles'

import { insideWebMercator, FakeWebMercatorTilingScheme, fakeRectangle, WEB_MERCATOR_MAX_LATITUDE_DEG } from '@/testing/cesiumBasemapStub'
import { publicAssetDir, startLocalAssetServer, type LocalAssetServer } from '@/testing/localAssetServer'

import {
  DEFAULT_BASEMAP_CONFIG,
  PmtilesImageryProvider,
  chooseBasemapSource,
  createBasemapSetup,
  type BasemapCesium,
  type PmtilesHead,
} from './basemap'
import { chooseTerrainMode, terrainProbeUrl } from './terrain'
import { probeSource, tileUrlForTemplate } from './offline'

const PUBLIC_DIR = publicAssetDir()

let assets: LocalAssetServer
let origin = ''

/** 只看 host 的那条约束用 fixture 的请求日志来证，而不是靠"我们没写外链"这种自述。 */
function requestedHosts(): string[] {
  return [...new Set(assets.requests.map((request) => request.host))]
}

beforeAll(async () => {
  assets = await startLocalAssetServer({ missing: 'spa-html', publicDir: PUBLIC_DIR })
  origin = assets.origin
})

afterAll(async () => {
  await assets.close()
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

    assets.requests.length = 0
    const setup = await createBasemapSetup(stubCesium(), source)
    expect(setup.layer).not.toBe(false)
    const provider = (setup.layer as unknown as { provider: PmtilesImageryProvider }).provider
    expect(provider.minimumLevel).toBe(0)
    expect(provider.maximumLevel).toBe(2)
    expect(provider.hasTile(1, 1, 1)).toBe(true)
    expect(provider.hasTile(3, 3, 3)).toBe(false)
    // 装配出来的 provider 矩形是渲染循环真正拿去算瓦号的东西：界内才算这条链没埋雷。
    expect(insideWebMercator(provider.rectangle)).toBe(true)
    expect(requestedHosts()).toEqual([new URL(origin).host])
  })

  it('SPA 兜底页（200 text/html）不被当成瓦片源：真归档仍是首选，只剩兜底页时判"没有底图"', async () => {
    const fetchLike = (input: RequestInfo | URL, init?: RequestInit) => fetch(String(input), init)
    // 这条模板故意指向没烘的目录：fixture 在 spa-html 档下会把它答成 `200 text/html`，
    // 与真 `vite preview` 的 SPA 兜底同形。
    const fallbackTemplate = `${origin}/basemaps/none/{z}/{x}/{y}.png`
    const config = { ...DEFAULT_BASEMAP_CONFIG, template: fallbackTemplate, pmtilesUrl: `${origin}/basemaps/aegis.pmtiles` }

    // 两条路都是 200，差别只在 content-type：探针必须把 HTML 那路判死。
    expect(await probeSource(fetchLike, tileUrlForTemplate(fallbackTemplate))).toBe(false)
    expect(await probeSource(fetchLike, config.pmtilesUrl)).toBe(true)
    expect(chooseBasemapSource({ templateReachable: false, pmtilesReachable: true }, config, origin).kind).toBe('pmtiles')
    expect(chooseBasemapSource({ templateReachable: await probeSource(fetchLike, tileUrlForTemplate(fallbackTemplate)), pmtilesReachable: false }, config, origin).kind).toBe('none')
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
    const hosts = requestedHosts()
    for (const host of hosts) {
      expect(host).toBe(new URL(origin).host)
    }
    expect(hosts.some((host) => /ion\.cesium|google|mapbox|tile\.openstreetmap/i.test(host))).toBe(false)
  })
})
