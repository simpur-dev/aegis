/**
 * basemap.ts 单测：底图源选择、PMTiles 头部判定、栅格 provider 的取瓦逻辑。
 * 全部用假 Cesium 命名空间 + 假 reader + 桩 createImageBitmap 完成（无 WebGL、无网络）。
 */

import { TileType } from 'pmtiles'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { readRepoFile } from '@/testing/repoSource'
import { FakeWebMercatorTilingScheme, fakeRectangle, insideWebMercator, WEB_MERCATOR_MAX_LATITUDE_DEG } from '@/testing/cesiumBasemapStub'

import type { BasemapCesium, TileReader } from './basemap'
import {
  BASEMAP_ATTRIBUTION,
  boundsOfHead,
  chooseBasemapSource,
  PmtilesImageryProvider,
  createBasemapSetup,
  DEFAULT_BASEMAP_CONFIG,
  DEFAULT_MAXIMUM_LEVEL,
  describePmtilesHead,
  PMTILES_TILE_TYPE,
} from './basemap'
import type { PmtilesHead } from './basemap'

function head(overrides: Partial<PmtilesHead> = {}): PmtilesHead {
  return {
    tileType: PMTILES_TILE_TYPE.png,
    minZoom: 0,
    maxZoom: 12,
    minLon: 78,
    minLat: 26,
    maxLon: 99,
    maxLat: 37,
    ...overrides,
  }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('本地常量的 tileType 与 pmtiles 库一致', () => {
  /** 纯逻辑层刻意不依赖 pmtiles 运行时，所以这里反向核对一次，防止常量漂移。 */
  it('PNG/JPEG/WebP/AVIF/MVT 取值与库枚举相同', () => {
    expect(PMTILES_TILE_TYPE.unknown).toBe(TileType.Unknown)
    expect(PMTILES_TILE_TYPE.mvt).toBe(TileType.Mvt)
    expect(PMTILES_TILE_TYPE.png).toBe(TileType.Png)
    expect(PMTILES_TILE_TYPE.jpeg).toBe(TileType.Jpeg)
    expect(PMTILES_TILE_TYPE.webp).toBe(TileType.Webp)
    expect(PMTILES_TILE_TYPE.avif).toBe(TileType.Avif)
  })
})

describe('底图源选择', () => {
  it('默认偏好 PMTiles（单文件 + Range，弱网友好）', () => {
    const source = chooseBasemapSource({ templateReachable: true, pmtilesReachable: true })
    expect(source.kind).toBe('pmtiles')
    if (source.kind === 'pmtiles') expect(source.url).toBe(DEFAULT_BASEMAP_CONFIG.pmtilesUrl)
  })

  it('PMTiles 没烘焙时退到 XYZ 模板，并说明是退让', () => {
    const source = chooseBasemapSource({ templateReachable: true, pmtilesReachable: false })
    expect(source.kind).toBe('url-template')
    expect(source.reason).toContain('改用')
  })

  it('偏好可切换为 template', () => {
    const source = chooseBasemapSource({ templateReachable: true, pmtilesReachable: true }, { ...DEFAULT_BASEMAP_CONFIG, prefer: 'template' })
    expect(source.kind).toBe('url-template')
  })

  it('两路都没有 → 无影像（不是白屏，而是只画地形与矢量）', () => {
    const source = chooseBasemapSource({ templateReachable: false, pmtilesReachable: false })
    expect(source.kind).toBe('none')
    if (source.kind === 'none') expect(source.reason).toContain('未烘焙')
  })

  it('配置指向第三方主机 → 直接无影像并给出拒绝理由', () => {
    const source = chooseBasemapSource(
      { templateReachable: true, pmtilesReachable: true },
      { ...DEFAULT_BASEMAP_CONFIG, template: 'https://tiles.example.com/{z}/{x}/{y}.png' },
      'http://localhost',
    )
    expect(source.kind).toBe('none')
    expect(source.reason).toContain('同源路径')
  })

  it('缺省署名回落到内置署名', () => {
    const source = chooseBasemapSource({ templateReachable: true, pmtilesReachable: true }, { ...DEFAULT_BASEMAP_CONFIG, attribution: '', prefer: 'template' })
    if (source.kind === 'url-template') expect(source.attribution).toBe(BASEMAP_ATTRIBUTION)
    else throw new Error('应命中 url-template')
  })
})

describe('PMTiles 头部判定', () => {
  it('栅格类型给出 MIME 与层级', () => {
    expect(describePmtilesHead(head())).toMatchObject({ raster: true, contentType: 'image/png', minimumLevel: 0, maximumLevel: 12 })
    expect(describePmtilesHead(head({ tileType: PMTILES_TILE_TYPE.webp })).contentType).toBe('image/webp')
    expect(describePmtilesHead(head({ tileType: PMTILES_TILE_TYPE.jpeg })).contentType).toBe('image/jpeg')
  })

  it('矢量 MVT 不能当影像：理由里指出要经马丁出 XYZ', () => {
    const verdict = describePmtilesHead(head({ tileType: PMTILES_TILE_TYPE.mvt }))
    expect(verdict.raster).toBe(false)
    expect(verdict.contentType).toBe('')
    expect(verdict.reason).toContain('Martin')
  })

  it('未知类型也被挡下', () => {
    expect(describePmtilesHead(head({ tileType: 99 })).raster).toBe(false)
  })

  it('退化边界（全 0）按全世界处理，避免把图裁成空', () => {
    expect(boundsOfHead({ minLon: 0, minLat: 0, maxLon: 0, maxLat: 0 })).toEqual({ west: -180, south: -90, east: 180, north: 90 })
    expect(boundsOfHead(head({ minLon: 99, minLat: 37, maxLon: 78, maxLat: 26 }))).toEqual({ west: -180, south: -90, east: 180, north: 90 })
    expect(describePmtilesHead(head({ minLon: 0, minLat: 0, maxLon: 0, maxLat: 0 })).bounds).toEqual({ west: -180, south: -90, east: 180, north: 90 })
  })

  it('maxZoom 小于 minZoom 时不崩，层级被夹紧', () => {
    const verdict = describePmtilesHead(head({ minZoom: 5, maxZoom: 2 }))
    expect(verdict.minimumLevel).toBe(5)
    expect(verdict.maximumLevel).toBeGreaterThanOrEqual(5)
    expect(describePmtilesHead(head({ minZoom: Number.NaN, maxZoom: Number.NaN })).minimumLevel).toBe(0)
    expect(describePmtilesHead(head({ maxZoom: Number.NaN })).maximumLevel).toBe(DEFAULT_MAXIMUM_LEVEL)
  })
})

/** 假 Cesium：只实现被用到的成员；用 `as unknown as BasemapCesium` 跨过类型（测试替身刻意不完整）。 */
function fakeCesium() {
  const log: string[] = []
  const cesium = {
    ImageryLayer: class {
      constructor(public provider: unknown, public options?: unknown) {
        log.push('ImageryLayer')
      }
    },
    UrlTemplateImageryProvider: class {
      constructor(public options: unknown) {
        log.push('UrlTemplateImageryProvider')
      }
    },
    Rectangle: fakeRectangle,
    WebMercatorTilingScheme: FakeWebMercatorTilingScheme,
    Credit: class {
      constructor(public html: string) {}
    },
    Event: class {},
  }
  return { log, cesium: cesium as unknown as BasemapCesium }
}

describe('影像层装配', () => {
  it('url-template → 用同源模板建 UrlTemplate provider', async () => {
    const { log, cesium } = fakeCesium()
    const setup = await createBasemapSetup(cesium, {
      kind: 'url-template',
      template: '/basemaps/{z}/{x}/{y}.png',
      attribution: 'AEGIS',
      maximumLevel: 12,
      reason: 'r',
    })
    expect(log).toEqual(['UrlTemplateImageryProvider', 'ImageryLayer'])
    expect(setup.layer).toBeTruthy()
    expect(setup.message).toBe('r')
  })

  it('none → layer=false，让 Viewer 不挂任何影像', async () => {
    const { cesium } = fakeCesium()
    const setup = await createBasemapSetup(cesium, { kind: 'none', reason: '未烘焙' })
    expect(setup.layer).toBe(false)
    expect(setup.message).toBe('未烘焙')
  })
})

describe('栅格 PMTiles provider', () => {
  function reader(tiles: Record<string, Uint8Array>): TileReader & { calls: string[] } {
    const calls: string[] = []
    return {
      calls,
      getZxy: async (z, x, y) => {
        const key = `${z}/${x}/${y}`
        calls.push(key)
        const data = tiles[key]
        return data ? { data: data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength) as ArrayBuffer } : undefined
      },
    }
  }

  function provider(headInput = head(), attribution = 'AEGIS 离线底图') {
    const { cesium } = fakeCesium()
    const tiles: Record<string, Uint8Array> = { '6/3/2': new Uint8Array([1, 2, 3]) }
    const source = reader(tiles)
    const instance = new PmtilesImageryProvider({ cesium, reader: source, verdict: describePmtilesHead(headInput), attribution })
    return { instance, source }
  }

  it('层级与瓦片号在范围内才算有瓦', () => {
    const { instance } = provider()
    expect(instance.hasTile(3, 2, 6)).toBe(true)
    expect(instance.hasTile(64, 0, 6)).toBe(false)
    expect(instance.hasTile(0, -1, 6)).toBe(false)
    expect(instance.hasTile(0, 0, 13)).toBe(false)
    expect(instance.minimumLevel).toBe(0)
    expect(instance.maximumLevel).toBe(12)
    expect(instance.tileWidth).toBe(256)
    expect(instance.rectangle).toEqual({ west: 78, south: 26, east: 99, north: 37 })
    expect(instance.credit.html).toBe('AEGIS 离线底图')
    expect(instance.getTileCredits(0, 0, 1)).toEqual([])
    expect(instance.pickFeatures(0, 0, 1, 0, 0)).toBeUndefined()
  })

  it('取到瓦片时按头部 MIME 解成位图', async () => {
    const bitmap = vi.fn((_input: unknown) => ({ kind: 'bitmap' }))
    vi.stubGlobal('createImageBitmap', bitmap)
    const { instance, source } = provider()
    const image = await instance.requestImage(3, 2, 6)
    expect(image).toEqual({ kind: 'bitmap' })
    expect(source.calls).toEqual(['6/3/2'])
    const blob = (bitmap.mock.calls[0] as unknown[])[0] as Blob
    expect(blob.type).toBe('image/png')
  })

  it('缺瓦 / 越界层级 → 透明瓦而不是抛错，且空白瓦复用同一实例', async () => {
    let created = 0
    vi.stubGlobal('createImageBitmap', () => {
      created += 1
      return Promise.resolve({ kind: `bitmap-${created}` })
    })
    const { instance, source } = provider()
    const missing = await instance.requestImage(9, 9, 6)
    const beyond = await instance.requestImage(0, 0, 20)
    expect(missing).toEqual({ kind: 'bitmap-1' })
    expect(beyond).toEqual({ kind: 'bitmap-1' })
    expect(source.calls).toEqual(['6/9/9'])
    expect(created).toBe(1)
  })

  it('零字节瓦片也走透明瓦', async () => {
    vi.stubGlobal('createImageBitmap', () => Promise.resolve({ kind: 'blank' }))
    const { cesium } = fakeCesium()
    const instance = new PmtilesImageryProvider({
      cesium,
      reader: { getZxy: async () => ({ data: new ArrayBuffer(0) }) },
      verdict: describePmtilesHead(head()),
      attribution: 'x',
    })
    expect(await instance.requestImage(1, 1, 6)).toEqual({ kind: 'blank' })
  })

  /** provider 矩形越出切片方案 = 浏览器里 `undefined.x` 打死渲染循环；这条前提写成断言。 */
  function providerFor(headInput: PmtilesHead) {
    const { cesium } = fakeCesium()
    return new PmtilesImageryProvider({
      cesium,
      reader: { getZxy: async () => undefined },
      verdict: describePmtilesHead(headInput),
      attribution: 'AEGIS 离线底图',
    })
  }

  it('退化边界（全世界 ±90）被裁进切片方案，不把越界矩形交给渲染循环', () => {
    const rect = providerFor(head({ minLon: 0, minLat: 0, maxLon: 0, maxLat: 0 })).rectangle
    expect(rect).toEqual({
      west: -180,
      south: -WEB_MERCATOR_MAX_LATITUDE_DEG,
      east: 180,
      north: WEB_MERCATOR_MAX_LATITUDE_DEG,
    })
    expect(insideWebMercator(rect)).toBe(true)
  })

  it('压在 Web Mercator 上界外侧的归档也被裁回来（PMTiles 边界是 1e7 定点数，取整必然越界）', () => {
    // 85.0511288 就是我们自己烘的 aegis.pmtiles 头部里的读数：
    // 生成器写的是 85.05112877980659，按 round(north*1e7) 存成 850511288 → 读回反而变大 2e-8°。
    // 浏览器实测的渲染崩溃就是这个数值造成的，断言用同一个数，防止改了代码却没改口径。
    const rect = providerFor(head({ minLon: -180, minLat: -85.0511288, maxLon: 180, maxLat: 85.0511288 })).rectangle
    expect(rect.north).toBe(WEB_MERCATOR_MAX_LATITUDE_DEG)
    expect(rect.south).toBe(-WEB_MERCATOR_MAX_LATITUDE_DEG)
    expect(insideWebMercator(rect)).toBe(true)
  })

  it('与切片方案无交集的归档 → 响亮抛错，由装配层降级为不挂影像', () => {
    const { cesium } = fakeCesium()
    expect(
      () =>
        new PmtilesImageryProvider({
          cesium,
          reader: { getZxy: async () => undefined },
          verdict: describePmtilesHead(head({ minLat: 86, maxLat: 89 })),
          attribution: 'x',
        }),
    ).toThrow('无交集')
  })
})

/**
 * 上面三条裁切用例依赖 Cesium 的两条内部事实（provider 矩形必须被切片方案完全包含、
 * 切片方案矩形来自 ±半长轴·π 米的反投影）。它们是私有实现细节，升级就可能变，
 * 所以读真源做门禁：前提没了要改代码，而不是把断言删掉。
 */
describe('Cesium 影像矩形前提没有漂移', () => {
  function cesiumSource(...parts: string[]): string {
    return readRepoFile('frontend', 'node_modules', '@cesium', 'engine', 'Source', ...parts)
  }

  /** 摊平成一行好做子串断言；行首的 `//` 必须一起去掉，否则注释里的句子被斜杠切成几段，断言会假红。 */
  function flatten(text: string): string {
    return text
      .split('\n')
      .map((line) => line.replace(/^\s*\/\/\s*/, ''))
      .join(' ')
      .replace(/\s+/g, ' ')
  }

  it('ImageryLayer 仍要求切片方案矩形完全包含 provider 矩形，且未判空就解引用瓦片坐标', () => {
    const flat = flatten(cesiumSource('Scene', 'ImageryLayer.js'))
    expect(flat).toContain(
      "The imagery TilingScheme's rectangle always fully contains the ImageryProvider's rectangle",
    )
    expect(flat).toContain('imageryTilingScheme.positionToTileXY( Rectangle.northwest(rectangle), imageryLevel, )')
    expect(flat).toContain('imageryTilingScheme.tileXYToRectangle( northwestTileCoordinates.x,')
  })

  it('WebMercatorTilingScheme 的矩形仍来自 ±(半长轴·π) 米反投影（即 85.05112877980659°）', () => {
    const flat = flatten(cesiumSource('Core', 'WebMercatorTilingScheme.js'))
    expect(flat).toContain('const semimajorAxisTimesPi = this._ellipsoid.maximumRadius * Math.PI;')
    expect(flat).toContain('this._projection.unproject(southwestCartesianScratch')
  })

  it('Rectangle.intersection 仍是「无交集返回 undefined」', () => {
    const flat = flatten(cesiumSource('Core', 'Rectangle.js'))
    expect(flat).toContain('static intersection(rectangle, otherRectangle, result) {')
    expect(flat).toContain('const north = Math.min(rectangle.north, otherRectangle.north);')
  })

  it('造假切片方案的纬度上界就是真实反投影的结果，不是抄来的数', () => {
    // 切片方案把 y = ±(椭球半长轴·π) 米交给 WebMercatorProjection.unproject，
    // 纬度 = atan(sinh(y/a)) = atan(sinh(π)) —— 逐位相同，所以这里敢用 toBe。
    // 顺序也要照写：先 `180 / Math.PI` 再乘会差出 1 ULP（85.0511287798066 ≠ …59），
    // 而这条界线的越界幅度本来就只有 2e-8°，一个 ULP 的量级差别正是要防的东西。
    expect(Math.atan(Math.sinh(Math.PI)) * 180 / Math.PI).toBe(WEB_MERCATOR_MAX_LATITUDE_DEG)
    expect(new FakeWebMercatorTilingScheme().rectangle.north).toBe(WEB_MERCATOR_MAX_LATITUDE_DEG)
  })
})
