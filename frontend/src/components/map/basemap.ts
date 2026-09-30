/**
 * 底图通道：离线自托管的两种烘焙形态 + 「无影像」兜底。
 *
 * 形态 A `url-template`：同源 XYZ 瓦片金字塔（`/basemaps/{z}/{x}/{y}.png`），
 *   零依赖、Cesium 原生 `UrlTemplateImageryProvider` 直接吃，缺点是文件数爆炸；
 * 形态 B `pmtiles`：单文件 `.pmtiles`（BSD-3 格式），走 HTTP Range 取瓦片，
 *   弱网/断网最友好（一个文件、可整盘拷到笔记本现场跑），需要本文件里的 `PmtilesImageryProvider` 适配
 *   ——Cesium 1.145 已不提供 `TileProviderImageryProvider`，所以按 `ImageryProvider` 契约自己实现。
 *
 * 两条硬约束在这里落地成**可单测的代码**，而不是靠 review 人眼扫：
 * - 任何指向非同源的底图地址都被 `isLocalAssetUrl` 拒掉（第三方瓦片服务与公共镜像源都在这一步挡下）；
 * - 矢量 PMTiles（MVT）不能当影像用：`describePmtilesHead` 直接判不通过并说明原因，
 *   那种情况请由 Martin/TileServer 出 XYZ 后再走形态 A（见 public/basemaps/README.md）。
 *
 * 与 terrain.ts 同一分层：决策是纯函数（`chooseBasemapSource`/`describePmtilesHead`），
 * 装配才需要 Cesium（注入命名空间；本模块只有 `import type`，运行时不加载 cesium）。
 */

import type { Credit, Event, ImageryLayer, ImageryProvider, Proxy, Rectangle, TileDiscardPolicy, UrlTemplateImageryProvider, WebMercatorTilingScheme } from 'cesium'

import { isLocalAssetUrl, tileUrlForTemplate } from './offline'

// ---------- 配置与纯决策 ----------

export const DEFAULT_BASEMAP_TEMPLATE = '/basemaps/{z}/{x}/{y}.png'
export const DEFAULT_PMTILES_URL = '/basemaps/aegis.pmtiles'
export const BASEMAP_ATTRIBUTION = 'AEGIS 离线底图（自托管烘焙，无第三方瓦片服务）'

/** 高原 1:5 万级现场判读通常烘到 z12–z14；上限写死可避免 Cesium 无谓地探更深层。 */
export const DEFAULT_MAXIMUM_LEVEL = 14

export interface BasemapConfig {
  template: string
  pmtilesUrl: string
  attribution: string
  maximumLevel: number
  prefer: 'pmtiles' | 'template'
}

export const DEFAULT_BASEMAP_CONFIG: BasemapConfig = {
  template: DEFAULT_BASEMAP_TEMPLATE,
  pmtilesUrl: DEFAULT_PMTILES_URL,
  attribution: BASEMAP_ATTRIBUTION,
  maximumLevel: DEFAULT_MAXIMUM_LEVEL,
  prefer: 'pmtiles',
}

export interface BasemapReachability {
  templateReachable: boolean
  pmtilesReachable: boolean
}

export type BasemapSource =
  | { kind: 'url-template'; template: string; attribution: string; maximumLevel: number; reason: string }
  | { kind: 'pmtiles'; url: string; attribution: string; reason: string }
  | { kind: 'none'; reason: string }

/**
 * 选择底图源（纯函数）。顺序：外链一律先拒 → 偏好项可用则用 → 另一项可用则用 → 无影像。
 * "无影像"是合法结果：地形 + 矢量要素 + 清单仍构成可用的「一张图」，白屏不是可接受的降级。
 */
export function chooseBasemapSource(
  reach: BasemapReachability,
  config: BasemapConfig = DEFAULT_BASEMAP_CONFIG,
  origin?: string,
): BasemapSource {
  const templateLocal = isLocalAssetUrl(config.template, origin)
  const pmtilesLocal = isLocalAssetUrl(config.pmtilesUrl, origin)
  if (!templateLocal || !pmtilesLocal) {
    return { kind: 'none', reason: '底图地址不是同源路径，已按"无外链"约束拒绝（禁止第三方瓦片服务）' }
  }

  const attribution = config.attribution || BASEMAP_ATTRIBUTION
  const pmtilesFirst = config.prefer === 'pmtiles'
  const first = pmtilesFirst
    ? { ok: reach.pmtilesReachable, source: { kind: 'pmtiles' as const, url: config.pmtilesUrl, attribution } }
    : {
        ok: reach.templateReachable,
        source: {
          kind: 'url-template' as const,
          template: config.template,
          attribution,
          maximumLevel: config.maximumLevel,
        },
      }
  const second = pmtilesFirst
    ? {
        ok: reach.templateReachable,
        source: {
          kind: 'url-template' as const,
          template: config.template,
          attribution,
          maximumLevel: config.maximumLevel,
        },
      }
    : { ok: reach.pmtilesReachable, source: { kind: 'pmtiles' as const, url: config.pmtilesUrl, attribution } }

  if (first.ok) {
    return { ...first.source, reason: `${describeSource(first.source)}：探针可用` }
  }
  if (second.ok) {
    return { ...second.source, reason: `首选源不可用，改用 ${describeSource(second.source)}` }
  }
  return {
    kind: 'none',
    reason: `未烘焙底图资产（${tileUrlForTemplate(config.template)} 与 ${config.pmtilesUrl} 均取不到），仅显示地形与矢量`,
  }
}

function describeSource(source: { kind: string; url?: string; template?: string }): string {
  if (source.kind === 'pmtiles') return `PMTiles 单文件 ${source.url}`
  return `XYZ 模板 ${source.template}`
}

// ---------- PMTiles 头部：纯判定 ----------

/**
 * `pmtiles.TileType` 的取值本地常量化，让纯逻辑层不产生对 pmtiles 的运行时依赖
 * （离线单测里 `offline.spec.ts` 用库里的枚举反向核对，防止口径漂移）。
 */
export const PMTILES_TILE_TYPE = {
  unknown: 0,
  mvt: 1,
  png: 2,
  jpeg: 3,
  webp: 4,
  avif: 5,
  mlt: 6,
} as const

export interface PmtilesHead {
  tileType: number
  minZoom: number
  maxZoom: number
  minLon: number
  minLat: number
  maxLon: number
  maxLat: number
}

export interface PmtilesVerdict {
  raster: boolean
  contentType: string
  reason: string
  /** 覆盖范围（度）；头部边界退化时为全世界。 */
  bounds: { west: number; south: number; east: number; north: number }
  minimumLevel: number
  maximumLevel: number
}

const CONTENT_TYPES: Record<number, string> = {
  [PMTILES_TILE_TYPE.png]: 'image/png',
  [PMTILES_TILE_TYPE.jpeg]: 'image/jpeg',
  [PMTILES_TILE_TYPE.webp]: 'image/webp',
  [PMTILES_TILE_TYPE.avif]: 'image/avif',
}

/** 头部边界退化（全 0）时用全世界：PMTiles 允许不写边界，此时不该把图裁成空。 */
export function boundsOfHead(head: Pick<PmtilesHead, 'minLon' | 'minLat' | 'maxLon' | 'maxLat'>): PmtilesVerdict['bounds'] {
  const sane =
    Number.isFinite(head.minLon) &&
    Number.isFinite(head.maxLon) &&
    Number.isFinite(head.minLat) &&
    Number.isFinite(head.maxLat) &&
    head.maxLon > head.minLon &&
    head.maxLat > head.minLat
  if (!sane) return { west: -180, south: -90, east: 180, north: 90 }
  return { west: head.minLon, south: head.minLat, east: head.maxLon, north: head.maxLat }
}

/** 判定归档是不是**栅格**、内容类型、层级与边界。MVT/未知类型在这里被挡，不进入渲染路径。 */
export function describePmtilesHead(head: PmtilesHead): PmtilesVerdict {
  const contentType = CONTENT_TYPES[head.tileType] ?? ''
  const bounds = boundsOfHead(head)
  const minimumLevel = Number.isFinite(head.minZoom) && head.minZoom >= 0 ? head.minZoom : 0
  const maximumLevel =
    Number.isFinite(head.maxZoom) && head.maxZoom >= minimumLevel ? head.maxZoom : Math.max(minimumLevel, DEFAULT_MAXIMUM_LEVEL)
  if (!contentType) {
    return {
      raster: false,
      contentType: '',
      reason:
        head.tileType === PMTILES_TILE_TYPE.mvt
          ? 'PMTiles 内是矢量 MVT：影像层画不了矢量瓦片，请由 Martin/瓦片服务出 XYZ 后走 url-template'
          : `未知的 PMTiles tileType=${head.tileType}`,
      bounds,
      minimumLevel,
      maximumLevel,
    }
  }
  return { raster: true, contentType, reason: '栅格归档，可作为影像层', bounds, minimumLevel, maximumLevel }
}

// ---------- 装配（需要 Cesium 命名空间，由 viewer.ts 注入）----------

/** 1×1 全透明 PNG（base64）：缺瓦的替身，避免成片黑块与错误日志刷屏。 */
const BLANK_PNG_BASE64 = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII='

export interface BasemapCesium {
  ImageryLayer: typeof ImageryLayer
  UrlTemplateImageryProvider: typeof UrlTemplateImageryProvider
  Rectangle: typeof Rectangle
  WebMercatorTilingScheme: typeof WebMercatorTilingScheme
  Credit: typeof Credit
  Event: typeof Event
}

export interface BasemapSetup {
  layer: ImageryLayer | false
  message: string
}

/** `ImageryProvider` 的最小契约实现所需的一切依赖：一个 PMTiles 读取句柄。 */
export interface TileReader {
  getZxy(z: number, x: number, y: number, signal?: AbortSignal): Promise<{ data: ArrayBuffer } | undefined>
}

/**
 * 栅格 PMTiles → Cesium 影像 provider。
 * 只做三件事：报边界与层级、按 z/x/y 取 Range、把字节解成位图。
 * 缺瓦返回透明瓦而不是抛错：一张图在野外最怕成片黑块与错误日志刷屏。
 */
export class PmtilesImageryProvider {
  readonly tileWidth = 256
  readonly tileHeight = 256
  readonly hasAlphaChannel = true
  readonly minimumLevel: number
  readonly maximumLevel: number
  readonly rectangle: InstanceType<BasemapCesium['Rectangle']>
  readonly tilingScheme: InstanceType<BasemapCesium['WebMercatorTilingScheme']>
  readonly credit: InstanceType<BasemapCesium['Credit']>
  readonly errorEvent: InstanceType<BasemapCesium['Event']>
  /** 官方各 provider 运行时会留 undefined（Cesium 的 d.ts 把它标成了非可选）。 */
  readonly tileDiscardPolicy?: TileDiscardPolicy
  readonly proxy?: Proxy

  private readonly reader: TileReader
  private readonly contentType: string
  private blankPromise: Promise<ImageBitmap> | null = null

  constructor(options: {
    cesium: BasemapCesium
    reader: TileReader
    verdict: PmtilesVerdict
    attribution: string
  }) {
    const { cesium, verdict } = options
    this.reader = options.reader
    this.contentType = verdict.contentType
    this.minimumLevel = verdict.minimumLevel
    this.maximumLevel = Math.max(verdict.maximumLevel, verdict.minimumLevel)
    this.rectangle = cesium.Rectangle.fromDegrees(verdict.bounds.west, verdict.bounds.south, verdict.bounds.east, verdict.bounds.north)
    this.tilingScheme = new cesium.WebMercatorTilingScheme()
    this.credit = new cesium.Credit(options.attribution)
    this.errorEvent = new cesium.Event()
  }

  /** 单文件归档没有逐瓦署名（署名在图层级），也不支持拾取属性（栅格底图无需 pickFeatures）。 */
  getTileCredits(_x: number, _y: number, _level: number): InstanceType<BasemapCesium['Credit']>[] {
    return []
  }

  pickFeatures(_x: number, _y: number, _level: number, _longitude: number, _latitude: number): undefined {
    return undefined
  }

  hasTile(x: number, y: number, level: number): boolean {
    if (level < this.minimumLevel || level > this.maximumLevel) return false
    const perSide = 2 ** level
    return x >= 0 && x < perSide && y >= 0 && y < perSide
  }

  async requestImage(x: number, y: number, level: number): Promise<ImageBitmap> {
    if (!this.hasTile(x, y, level)) return this.blank()
    const tile = await this.reader.getZxy(level, x, y)
    if (!tile || tile.data.byteLength === 0) return this.blank()
    return createImageBitmap(new Blob([tile.data], { type: this.contentType }))
  }

  /** 1×1 全透明位图：懒建一次并复用（Cesium 会并发问很多缺口的瓦片）。 */
  private blank(): Promise<ImageBitmap> {
    if (!this.blankPromise) {
      const bytes = Uint8Array.from(atob(BLANK_PNG_BASE64), (char) => char.charCodeAt(0))
      this.blankPromise = createImageBitmap(new Blob([bytes], { type: 'image/png' }))
    }
    return this.blankPromise
  }
}

/** 动态取 pmtiles（保持本模块运行时零依赖，单测因此完全不需要 pmtiles/cesium）。 */
async function openPmtiles(url: string): Promise<{ reader: TileReader; head: PmtilesHead }> {
  const module = await import('pmtiles')
  const archive = new module.PMTiles(url)
  const header = await archive.getHeader()
  return {
    reader: { getZxy: (z, x, y, signal) => archive.getZxy(z, x, y, signal) },
    head: {
      tileType: header.tileType,
      minZoom: header.minZoom,
      maxZoom: header.maxZoom,
      minLon: header.minLon,
      minLat: header.minLat,
      maxLon: header.maxLon,
      maxLat: header.maxLat,
    },
  }
}

/**
 * Cesium 的 `ImageryProvider` 声明把 `tileDiscardPolicy`/`proxy` 标为非可选，
 * 而官方各 provider 运行时都留 undefined —— 本类按运行时口径实现，故只在这一处做类型桥接。
 */
async function buildPmtilesProvider(cesium: BasemapCesium, url: string, attribution: string): Promise<ImageryProvider> {
  const { reader, head } = await openPmtiles(url)
  const verdict = describePmtilesHead(head)
  if (!verdict.raster) throw new Error(verdict.reason)
  const provider = new PmtilesImageryProvider({ cesium, reader, verdict, attribution })
  return provider as unknown as ImageryProvider
}

/** 装配影像层；`false` 表示不挂任何底图（Viewer 的 `baseLayer: false` 口径）。 */
export async function createBasemapSetup(cesium: BasemapCesium, source: BasemapSource): Promise<BasemapSetup> {
  if (source.kind === 'none') return { layer: false, message: source.reason }

  if (source.kind === 'url-template') {
    const provider = new cesium.UrlTemplateImageryProvider({
      url: source.template,
      credit: source.attribution,
      maximumLevel: source.maximumLevel,
    })
    return { layer: new cesium.ImageryLayer(provider), message: source.reason }
  }

  try {
    // 先 await 头部再建层：矢量 MVT / 打不开的归档会在这里被捕获并降级为"无影像"，
    // 而不是把 rejection 留给 Cesium 的异步图层内部吞掉。
    const provider = await buildPmtilesProvider(cesium, source.url, source.attribution)
    return { layer: new cesium.ImageryLayer(provider), message: source.reason }
  } catch (error) {
    const reason = error instanceof Error ? error.message : String(error)
    return { layer: false, message: `PMTiles 打不开（${reason}），本次不挂影像` }
  }
}
