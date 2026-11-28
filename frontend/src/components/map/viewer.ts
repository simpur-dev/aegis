/**
 * viewer.ts —— 「一张图」唯一接触 Cesium DOM / WebGL 表面的文件。
 *
 * 分层约定（其余模块都靠这条边界划清）：
 * - 纯逻辑（entities.ts / offline.ts 与 terrain.ts / basemap.ts 的决策函数）产出 plain data 与结论；
 * - 本文件把 plain data 变成 entity / imagery / terrain，并把相机事件回吐成 plain BBox + 缩放级；
 *   只回答"怎么画"，不回答"画什么、什么颜色"（那些在 entities.ts，已单测）。
 * - cesium 的**运行时**导入只有一处：`await import('cesium')`（本文件内），
 *   其余全是 `import type`（编译期抹掉）。于是地图页不进首屏包，单测（jsdom 无 WebGL）永不触发它。
 *
 * 零外部服务口径（可 grep 自证）：
 * - `baseLayer` 必须显式给出：不显式给就会落到官方默认底图（那是需要令牌的托管资产）；
 * - `geocoder: false`：默认地理编码同样走第三方服务；
 * - `baseLayerPicker: false`：默认图层选择器里全是托管源；
 * - 因此本文件不出现任何令牌、资产 ID 或外部主机名。
 */

import 'cesium/Source/Widgets/widgets.css'

import type {
  Cartesian2,
  ClassificationType,
  CustomDataSource,
  Entity,
  EntityCollection,
  ImageryLayer,
  ScreenSpaceEventHandler,
  TerrainProvider,
  Viewer as CesiumViewer,
} from 'cesium'

import type { BasemapConfig, BasemapSource } from './basemap'
import { BASEMAP_ATTRIBUTION, chooseBasemapSource, createBasemapSetup, DEFAULT_BASEMAP_CONFIG } from './basemap'
import { mapPalette, type MapPalette } from '@/theme/map'
import type { ThemeMode } from '@/theme/tokens'
import { clearMapDebugHandle, publishMapDebugHandle } from './debugHandle'
import type {
  BBox,
  HazardZoneFeature,
  LngLat,
  PlanLayer,
  PointCluster,
  ReachFeature,
  RenderPlan,
  StationFeature,
  WarningFeature,
} from './entities'
import { PLAN_LAYERS, TIBET_RECTANGLE, zoomForCameraHeight } from './entities'
import type { HistoricalSite } from './gyirongCase'
import type { ProbeFetch } from './offline'
import { collectFacts, tileUrlForTemplate } from './offline'
import { chooseTerrainMode, createTerrainSetup, DEFAULT_TERRAIN_URL, terrainProbeUrl } from './terrain'
import { EMPTY_WEATHER_LAYER, rainCellAt, rainSelectionPoint, weatherMarkerText, type WeatherMapLayer } from './weather'

/** Cesium 命名空间的类型别名（type-only，不产生运行时导入）。 */
type CesiumModule = typeof import('cesium')

/** 图层词汇表定义在 entities.ts（视图与渲染共用一份，避免两处枚举漂移），此处只做别名。 */
export type LayerName = PlanLayer

const LAYER_NAMES: readonly LayerName[] = PLAN_LAYERS

/** 无影像时的地球底色等画面内部的颜色，全部由 `@/theme/map` 按当前主题派生（本文件不写字面色）。 */

/** 渲染端的显隐状态形状（值由视图持有，本文件只施加）。 */
export type LayerVisibility = Record<LayerName, boolean>

/** 渲染载荷：契约定义在 entities.ts（纯逻辑层），本文件只按它画，不再另立形状。 */
export type RenderData = RenderPlan

export interface MapScale {
  meters: number
  width: number
}

export type HistoricalReliefStatus = 'visible' | 'closeup' | 'street' | 'surroundings' | 'outside' | 'unavailable'

interface ReliefImage {
  url: string
  bounds: BBox
}

interface CloseupImage extends ReliefImage {
  attribution: string
  status: 'closeup' | 'street'
  maxHeightM: number
}

interface CloseupLayer {
  image: CloseupImage
  layer: ImageryLayer | null
  state: 'idle' | 'loading' | 'ready' | 'unavailable'
}

interface ReferenceLine {
  name: string
  labelAt: readonly number[]
  coordinates: readonly (readonly number[])[]
}

interface MappedDetail {
  osmId: number
  coordinates: readonly (readonly number[])[]
}

export interface MapSceneOptions {
  terrainUrl?: string
  basemap?: Partial<BasemapConfig>
  worldReliefUrl?: string
  initialRectangle?: BBox
  historicalSites?: readonly HistoricalSite[]
  historicalRelief?: ReliefImage & { surroundings?: ReliefImage; closeups?: readonly CloseupImage[] }
  historicalGeography?: {
    waterways: readonly ReferenceLine[]
    roads: readonly ReferenceLine[]
    closeup?: { localRoads: readonly MappedDetail[]; buildings: readonly MappedDetail[]; riverBanks: readonly MappedDetail[] }
  }
  onHistoricalRelief?: (status: HistoricalReliefStatus | null) => void
  onScaleChanged?: (scale: MapScale | null) => void
  onGlobalViewChanged?: (global: boolean) => void
  /** 探针用 fetch：生产传 window.fetch，特殊环境可注入假实现。 */
  fetchLike?: ProbeFetch
  browserOnline?: () => boolean
  origin?: string
  onViewChanged?: (bbox: BBox | null, zoom: number) => void
  onSelect?: (featureId: string | null) => void
  /** 建场时所处主题；之后的翻转由 `setTheme` 推进。缺省按浅色。 */
  themeMode?: ThemeMode
}

export interface MapSceneInfo {
  basemapMessage: string
  terrainMessage: string
  basemapSource: BasemapSource['kind']
  terrainMode: 'quantized-mesh' | 'ellipsoid'
}

export interface MapScene {
  /** 幂等：要素 id 集合不变时跳过重建（相机每挪一点就重画会掉帧）。 */
  render(data: RenderData): void
  setWeather(layer: WeatherMapLayer): void
  /**
   * 换主题：把地球底色/图元色换到新档，并**重画全部图层**。
   * 必须重画——Cesium 的图元色是在 `entities.add()` 那一刻烤进去的，
   * 只换 `globe.baseColor` 会留下"底翻面了、标记还是上一档的白字黑边"这种半翻画面。
   */
  setTheme(mode: ThemeMode): void
  /** 当前生效的调色板（只读，供真机门禁核对画面确实按主题派生）。 */
  readonly palette: MapPalette
  focusAt(point: LngLat): void
  fitBBox(box: BBox): void
  zoomBy(direction: 1 | -1): void
  basemapSwitchBounds(): BBox | null
  currentZoom(): number
  setLayerVisible(layer: LayerName, visible: boolean): void
  setHistoryVisible(visible: boolean): void
  destroy(): void
  readonly info: MapSceneInfo
}

declare global {
  interface Window {
    /** Cesium 定位 Workers/Assets/Widgets 的基址，必须指向自托管副本。 */
    CESIUM_BASE_URL?: string
  }
}

/**
 * 指向 `public/cesium/`（由 `node_modules/cesium/Build/Cesium/{Workers,Assets,ThirdParty,Widgets}` 拷来，
 * 清单见 public/basemaps/README.md）。不设置则退回按 `import.meta.url` 猜路径，
 * 打包后必猜错，现场表现是"开发环境正常、构建产物白屏"。
 */
function ensureCesiumBaseUrl(): void {
  if (typeof window === 'undefined' || window.CESIUM_BASE_URL) return
  const base = typeof import.meta.env?.BASE_URL === 'string' ? import.meta.env.BASE_URL : '/'
  window.CESIUM_BASE_URL = `${base}cesium/`
}

function rectArray(box: BBox): [number, number, number, number] {
  return [box.west, box.south, box.east, box.north]
}

/**
 * 把 Cesium 默认的厂商 logo 署名换成本项目自己的说明。
 *
 * 默认那条署名是一段带外链的 `<a target="_blank">` + 厂商图片（Playwright 截图实测画在左下角）。
 * 本项目一张托管资产都不取（底图与地形都是自托管烘焙），留着它既与"零托管服务、零令牌"的
 * 对外口径自相矛盾，又往一个宣称离线可用的页面里塞外链表单。
 *
 * 换掉而不是把整个署名容器隐藏：出处说明必须仍然可见（合成高程那句随瓦片信用进容器，
 * 这里再补一条"自托管、无第三方瓦片服务"），去掉的只是厂商品牌。
 */
export function applyOfflineCredits(cesium: CesiumModule): void {
  cesium.CreditDisplay.cesiumCredit = new cesium.Credit(BASEMAP_ATTRIBUTION)
}

/** 站点：有台账高程时用绝对高度（地形缺失也不贴地），否则钳地。高危等级才出文字标签。 */
function stationGraphics(
  cesium: CesiumModule,
  feature: StationFeature & { lonlat: LngLat },
  palette: MapPalette,
): Entity.ConstructorOptions {
  const severe = feature.riskLevel !== null && feature.riskLevel <= 2
  return {
    id: feature.id,
    name: feature.title,
    position: cesium.Cartesian3.fromDegrees(feature.lonlat.lon, feature.lonlat.lat, feature.elevationM ?? 0),
    point: {
      pixelSize: severe ? 15 : 10,
      color: cesium.Color.fromCssColorString(palette.fillFor(feature.colorCss)),
      outlineColor: cesium.Color.fromCssColorString(palette.markerOutline),
      outlineWidth: 2,
      heightReference: feature.elevationM === null ? cesium.HeightReference.CLAMP_TO_GROUND : undefined,
      disableDepthTestDistance: Number.POSITIVE_INFINITY,
    },
    label: severe
      ? {
          text: feature.nameZh,
          font: '12px sans-serif',
          fillColor: cesium.Color.fromCssColorString(palette.labelText),
          outlineColor: cesium.Color.fromCssColorString(palette.labelHalo),
          outlineWidth: 2,
          pixelOffset: new cesium.Cartesian2(0, -18),
          showBackground: true,
          backgroundColor: cesium.Color.fromCssColorString(palette.labelBackground).withAlpha(palette.labelBackgroundAlpha),
        }
      : undefined,
  }
}

/** 预警：区域级 marker，空心环形色带，等级由颜色承担，标题留在详情面板。 */
function warningGraphics(
  cesium: CesiumModule,
  feature: WarningFeature & { lonlat: LngLat },
  palette: MapPalette,
): Entity.ConstructorOptions {
  const color = cesium.Color.fromCssColorString(palette.fillFor(feature.colorCss))
  return {
    id: feature.id,
    name: feature.title,
    position: cesium.Cartesian3.fromDegrees(feature.lonlat.lon, feature.lonlat.lat),
    point: {
      pixelSize: 18,
      color: color.withAlpha(0.25),
      outlineColor: color,
      outlineWidth: 3,
      heightReference: cesium.HeightReference.CLAMP_TO_GROUND,
      disableDepthTestDistance: Number.POSITIVE_INFINITY,
    },
  }
}

/** 触达 marker：语义是"通道是否打通"，故配红色只给劣化项（见 entities.buildReachLayer）。 */
function reachGraphics(cesium: CesiumModule, feature: ReachFeature & { lonlat: LngLat }, palette: MapPalette): Entity.ConstructorOptions {
  return {
    id: feature.id,
    name: feature.title,
    position: cesium.Cartesian3.fromDegrees(feature.lonlat.lon, feature.lonlat.lat),
    point: {
      pixelSize: 8,
      color: cesium.Color.fromCssColorString(palette.fillFor(feature.colorCss)),
      outlineColor: cesium.Color.fromCssColorString(palette.markerOutline),
      outlineWidth: 1,
      heightReference: cesium.HeightReference.CLAMP_TO_GROUND,
      disableDepthTestDistance: Number.POSITIVE_INFINITY,
    },
    label: {
      text: feature.title,
      font: '11px sans-serif',
      fillColor: cesium.Color.fromCssColorString(palette.labelText),
      pixelOffset: new cesium.Cartesian2(0, 14),
      showBackground: true,
      backgroundColor: cesium.Color.fromCssColorString(palette.labelBackground).withAlpha(palette.labelBackgroundAlpha),
    },
  }
}

/** 灾害面：贴地分类（TERRAIN）→ 地形没烘焙时自动贴在椭球面上，同一份数据两种形态都对齐。 */
function hazardZoneGraphics(
  cesium: CesiumModule,
  feature: HazardZoneFeature,
  classificationType: ClassificationType,
  palette: MapPalette,
): Entity.ConstructorOptions {
  const outer = feature.outer.map((point) => cesium.Cartesian3.fromDegrees(point.lon, point.lat))
  const holes = feature.holes.map(
    (ring) => new cesium.PolygonHierarchy(ring.map((point) => cesium.Cartesian3.fromDegrees(point.lon, point.lat))),
  )
  const color = cesium.Color.fromCssColorString(palette.fillFor(feature.colorCss))
  return {
    id: feature.id,
    name: feature.title,
    polygon: {
      hierarchy: new cesium.PolygonHierarchy(outer, holes),
      material: color.withAlpha(0.3),
      outline: true,
      outlineColor: color,
      outlineWidth: 2,
      classificationType,
    },
  }
}

/** 聚合簇：尺寸按成员数对数增长，标签就是计数（"多少个要素在这一格里"）。 */
function clusterGraphics(cesium: CesiumModule, cluster: PointCluster, palette: MapPalette): Entity.ConstructorOptions {
  return {
    id: cluster.id,
    position: cesium.Cartesian3.fromDegrees(cluster.lonlat.lon, cluster.lonlat.lat),
    point: {
      pixelSize: Math.min(44, 18 + Math.log2(Math.max(2, cluster.count)) * 6),
      color: cesium.Color.fromCssColorString(palette.fillFor(cluster.colorCss)).withAlpha(0.85),
      outlineColor: cesium.Color.fromCssColorString(palette.markerOutline),
      outlineWidth: 2,
      heightReference: cesium.HeightReference.CLAMP_TO_GROUND,
      disableDepthTestDistance: Number.POSITIVE_INFINITY,
    },
    label: {
      text: String(cluster.count),
      font: 'bold 12px sans-serif',
      fillColor: cesium.Color.fromCssColorString(palette.labelText),
      outlineColor: cesium.Color.fromCssColorString(palette.labelHalo),
      outlineWidth: 2,
    },
  }
}

interface LayerHandle {
  dataSource: CustomDataSource
  entities: EntityCollection
}

type LayerHandles = Record<LayerName, LayerHandle>

/** 一层一个 CustomDataSource：显隐是 O(1) 开关，销毁随 Viewer 一起走。 */
function createLayerHandles(cesium: CesiumModule, viewer: CesiumViewer): LayerHandles {
  const handles = {} as LayerHandles
  for (const name of LAYER_NAMES) {
    const dataSource = new cesium.CustomDataSource(`aegis-${name}`)
    viewer.dataSources.add(dataSource)
    handles[name] = { dataSource, entities: dataSource.entities }
  }
  return handles
}

/**
 * 建场：探针 → 决策 → 装配 Viewer/影像/地形 → 绑定相机与点击事件。
 * 任何一步失败都抛给调用方，由 MapView 显示"三维图不可用 + 文字清单仍在"，页面不崩。
 */
/**
 * 等容器拿到非零尺寸（最多 `timeoutMs`）。
 *
 * 没有 ResizeObserver、或拿到的是测试替身（没有 clientWidth 这个数）时直接放行：
 * 建场本身不该因为"等不到尺寸"而永久挂起——超时后照建，后面还有 ResizeObserver 兜底。
 */
async function waitForContainerSize(element: HTMLElement, timeoutMs = 3_000): Promise<void> {
  if (typeof element?.clientWidth !== 'number' || typeof element?.clientHeight !== 'number') return
  if (element.clientWidth > 0 && element.clientHeight > 0) return
  if (typeof ResizeObserver === 'undefined') return
  await new Promise<void>((resolve) => {
    const observer = new ResizeObserver(() => {
      if (element.clientWidth > 0 && element.clientHeight > 0) finish()
    })
    let timer: ReturnType<typeof setTimeout> | null = null
    function finish(): void {
      if (timer !== null) clearTimeout(timer)
      observer.disconnect()
      resolve()
    }
    timer = setTimeout(finish, timeoutMs)
    observer.observe(element)
  })
}

const WORLD_RELIEF_TRANSITION_START_M = 3_200_000
const WORLD_RELIEF_TRANSITION_END_M = 6_200_000
const GLOBAL_ZOOM_DISTANCE_M = 40_000_000
const CASE_ZOOM_DISTANCE_M = 75_000
const CASE_RELIEF_FADE_START_M = 78_000
const CASE_RELIEF_FADE_END_M = 120_000

export async function createMapScene(el: HTMLElement, options: MapSceneOptions = {}): Promise<MapScene> {
  ensureCesiumBaseUrl()
  const cesium = await import('cesium')
  applyOfflineCredits(cesium)

  const basemapConfig: BasemapConfig = { ...DEFAULT_BASEMAP_CONFIG, ...options.basemap }
  const terrainUrl = options.terrainUrl ?? DEFAULT_TERRAIN_URL
  const fetchLike: ProbeFetch =
    options.fetchLike ??
    ((url, init) => {
      if (typeof fetch === 'undefined') return Promise.reject(new Error('当前环境没有 fetch'))
      return fetch(url, init)
    })
  const browserOnline = options.browserOnline ?? (() => globalThis.navigator?.onLine !== false)

  /**
   * 建场前等容器有尺寸。
   *
   * Cesium 在 `new Viewer(el, ...)` 那一刻按 `el.clientWidth/Height` 定画布，之后**不会**自己
   * 跟随容器变化；而 `v-if` 切档后宿主刚插入、或首屏布局尚未完成时，宽度会是 0 —— 于是画布
   * 0×420、相机 `setView` 算成全球视野（真机实测：宿主 811×490，画面一片空白且无报错）。
   * 等不到（超时）也照常建场：后面还有 ResizeObserver 兜底，不能因此让页面永远停在 Loading。
   */
  await waitForContainerSize(el)

  const facts = await collectFacts({
    fetch: fetchLike,
    browserOnline: browserOnline(),
    targets: {
      api: '/healthz',
      basemapTemplate: tileUrlForTemplate(basemapConfig.template),
      terrain: terrainProbeUrl(terrainUrl),
      localBasemap: basemapConfig.pmtilesUrl,
    },
  })

  const terrainSetup = await createTerrainSetup(
    cesium,
    chooseTerrainMode({
      configuredUrl: terrainUrl,
      layerJsonReachable: facts.terrainReachable,
      browserOnline: facts.browserOnline,
      origin: options.origin,
    }),
  )

  // 探针两路瓦片可达性 → 底图决策入参：basemapReachable=XYZ 模板可取，localBasemapAvailable=PMTiles 文件在。
  const source = chooseBasemapSource(
    { templateReachable: facts.basemapReachable, pmtilesReachable: facts.localBasemapAvailable },
    basemapConfig,
    options.origin,
  )
  const basemapSetup = await createBasemapSetup(cesium, source)
  let worldLayer: ImageryLayer | null = null
  if (options.worldReliefUrl) {
    try {
      const provider = await cesium.TileMapServiceImageryProvider.fromUrl(options.worldReliefUrl, {
        credit: '全球地形 · Natural Earth II',
      })
      worldLayer = new cesium.ImageryLayer(provider)
    } catch {
      // 全球影像读取失败时保留自托管全域瓦片。
    }
  }

  const viewer: CesiumViewer = new cesium.Viewer(el, {
    baseLayer: worldLayer ?? basemapSetup.layer,
    baseLayerPicker: false,
    geocoder: false,
    homeButton: false,
    sceneModePicker: false,
    projectionPicker: false,
    navigationHelpButton: false,
    fullscreenButton: false,
    animation: false,
    timeline: false,
    infoBox: false,
    selectionIndicator: false,
    shouldAnimate: false,
    // 高原现场多为轻薄本/三防机：只在场景变化时重绘，静置几乎不吃 CPU。
    requestRenderMode: true,
    maximumRenderTimeChange: Number.POSITIVE_INFINITY,
    terrainProvider: terrainSetup.provider as TerrainProvider,
  })
  if (worldLayer && basemapSetup.layer) viewer.imageryLayers.add(basemapSetup.layer)

  let palette = mapPalette(options.themeMode ?? 'light')
  const initialRectangle = options.initialRectangle ?? TIBET_RECTANGLE
  /**
   * 建场那一刻容器若还没有宽度，`setView` 会算出全球视野（真机：宽度 0 时视野 -180..180、缩放 0）；
   * 拿到尺寸后必须补一次，否则画布恢复成 811×490 了、画面却停在星空里。
   */
  let viewApplied = el.clientWidth > 0
  let destroyed = false
  let rainImagery: ImageryLayer | null = null
  /**
   * 容器尺寸变化 → 让 Cesium 跟着重排。
   *
   * 建场那一刻容器可能还没有宽度（`v-if` 切档后宿主刚插入、或布局尚未完成），Cesium 会按 0 宽
   * 建出画布，而它**不会自动跟随**容器变化——真机实测：宿主 811×490、canvas 0×420，地图一片空白
   * 且没有任何报错。`requestRenderMode` 下 resize 之后还要显式 requestRender，否则不重绘。
   */
  const resizeObserver =
    typeof ResizeObserver === 'undefined'
      ? null
      : new ResizeObserver(() => {
          if (viewer.isDestroyed()) return
          viewer.resize()
          if (!viewApplied && el.clientWidth > 0) {
            viewer.camera.setView({ destination: cesium.Rectangle.fromDegrees(...rectArray(initialRectangle)) })
            viewApplied = true
          }
          viewer.scene.requestRender()
        })
  resizeObserver?.observe(el)

  function applyPalette(): void {
    const scene = viewer.scene
    scene.globe.baseColor = cesium.Color.fromCssColorString(palette.globeBase)
    scene.backgroundColor = cesium.Color.fromCssColorString(palette.spaceBackdrop)
    if (scene.skyAtmosphere) scene.skyAtmosphere.show = false
    if (scene.sun) scene.sun.show = false
    if (scene.moon) scene.moon.show = false
  }

  let globalView = false
  function updateWorldView(): void {
    if (!worldLayer) return
    const height = viewer.camera.positionCartographic.height
    const fine = basemapSetup.layer
    if (fine) {
      const range = WORLD_RELIEF_TRANSITION_END_M - WORLD_RELIEF_TRANSITION_START_M
      fine.alpha = cesium.Math.clamp((WORLD_RELIEF_TRANSITION_END_M - height) / range, 0, 1)
      fine.show = fine.alpha > 0
    }
    const next = height >= WORLD_RELIEF_TRANSITION_END_M
    if (globalView === next) return
    globalView = next
    options.onGlobalViewChanged?.(next)
    viewer.scene.requestRender()
  }

  applyPalette()
  viewer.scene.globe.enableLighting = false
  /**
   * 瓦片队列有变化就重绘，而不是只在"排空"那一刻重绘。
   *
   * 这个页面为了在轻薄本上省电开了 `requestRenderMode`，又把 `maximumRenderTimeChange`
   * 设成 Infinity——于是**异步到达的影像/地形瓦不会触发任何一帧**：请求全 200、图层 show=true、
   * alpha=1，画面却一直是地球底色。此前只在 `queued === 0` 时补一帧，真机踩到另一种形态：
   * 建场首帧没能排出请求，队列一直"空"，`0` 事件再也不来 ⇒ 一张瓦片都不请求、画面停在星空里。
   * 现在队列每变一次就要一帧（进队/出队都算），静置时仍然一帧都不多画。
   */
  viewer.scene.globe.tileLoadProgressEvent.addEventListener((remaining) => {
    viewer.scene.requestRender()
    if (remaining === 0 && options.onScaleChanged) options.onScaleChanged(mapScale())
  })
  viewer.scene.screenSpaceCameraController.maximumZoomDistance = GLOBAL_ZOOM_DISTANCE_M
  viewer.scene.screenSpaceCameraController.minimumZoomDistance = 200
  viewer.camera.setView({ destination: cesium.Rectangle.fromDegrees(...rectArray(initialRectangle)) })
  /**
   * 建场末尾必须显式要一帧：`requestRenderMode` 下没有这一下，首帧可能永远不来，
   * 影像/地形的瓦片请求链就整条不启动——真机实测：canvas/视野/图层都对，`performance` 里
   * 只有探针的两条 HEAD，一张瓦片都没请求，画面停在空白。
   * 再补两帧：globe 的 surface 初始化不止一次 `endFrame`，只渲染一帧时它可能还没排出请求。
   */
  viewer.scene.requestRender()
  if (typeof requestAnimationFrame === 'function') {
    requestAnimationFrame(() => {
      if (destroyed) return
      viewer.scene.requestRender()
      requestAnimationFrame(() => {
        if (!destroyed) viewer.scene.requestRender()
      })
    })
  }
  // `?mapDebug` 时才挂只读句柄：视觉烟雾门禁要读 globe.getHeight() 这类真场景状态（见 debugHandle.ts）。
  publishMapDebugHandle(viewer, location.search)

  const handles = createLayerHandles(cesium, viewer)
  const history = options.historicalSites?.length ? new cesium.CustomDataSource('historical-case') : null
  if (history) {
    history.show = false
    void viewer.dataSources.add(history)
  }
  const geography = options.historicalGeography ? new cesium.CustomDataSource('gyirong-geography') : null
  const geographyCredit = geography ? new cesium.Credit('© OpenStreetMap', true) : null
  if (geography) {
    geography.show = false
    void viewer.dataSources.add(geography)
  }
  const closeupDetails = options.historicalGeography?.closeup
  const detail = closeupDetails ? new cesium.CustomDataSource('gyirong-closeup') : null
  if (detail) {
    detail.show = false
    void viewer.dataSources.add(detail)
  }
  let localRelief: ImageryLayer | null = null
  let loadingRelief = false
  let surroundingRelief: ImageryLayer | null = null
  let loadingSurroundings = false
  const closeups: CloseupLayer[] = (options.historicalRelief?.closeups ?? []).map((image) => ({ image, layer: null, state: 'idle' }))
  let historicalView = false
  let reliefFailed = false
  let reliefStatus: HistoricalReliefStatus | null = null

  function centerInReliefBounds(bounds: BBox): boolean {
    const { canvas, globe } = viewer.scene
    const center = new cesium.Cartesian2(canvas.clientWidth / 2, canvas.clientHeight / 2)
    const position = viewer.camera.pickEllipsoid(center, globe.ellipsoid)
    if (!position) return false
    const { longitude, latitude } = globe.ellipsoid.cartesianToCartographic(position)
    const lon = cesium.Math.toDegrees(longitude)
    const lat = cesium.Math.toDegrees(latitude)
    return bounds.west <= lon && lon <= bounds.east && bounds.south <= lat && lat <= bounds.north
  }

  function updateHistoricalView(): void {
    if (!history) return
    if (rainImagery && viewer.imageryLayers.indexOf(rainImagery) < viewer.imageryLayers.length - 1) {
      viewer.imageryLayers.raiseToTop(rainImagery)
    }
    const height = viewer.camera.positionCartographic.height
    const inCaseScale = historicalView && height < CASE_RELIEF_FADE_END_M
    const bounds = options.historicalRelief?.bounds
    const inCaseArea = inCaseScale && (!bounds || centerInReliefBounds(bounds))
    history.show = inCaseArea
    if (geography && geographyCredit && geography.show !== inCaseArea) {
      geography.show = inCaseArea
      if (inCaseArea) viewer.creditDisplay.addStaticCredit(geographyCredit)
      else viewer.creditDisplay.removeStaticCredit(geographyCredit)
    }

    const surroundings = options.historicalRelief?.surroundings
    if (surroundingRelief) {
      surroundingRelief.alpha = cesium.Math.clamp((360_000 - height) / 140_000, 0, 1)
      surroundingRelief.show = historicalView && surroundingRelief.alpha > 0
    }
    if (localRelief) {
      const fadeRange = CASE_RELIEF_FADE_END_M - CASE_RELIEF_FADE_START_M
      localRelief.alpha = cesium.Math.clamp((CASE_RELIEF_FADE_END_M - height) / fadeRange, 0, 1)
      localRelief.show = inCaseScale
    }
    let closeupStatus: CloseupImage['status'] | null = null
    for (const entry of closeups) {
      const { image } = entry
      const show = inCaseScale && height < image.maxHeightM && centerInReliefBounds(image.bounds)
      if (show && entry.state === 'idle') void loadCloseup(entry)
      if (!entry.layer) continue
      entry.layer.show = show
      entry.layer.alpha = cesium.Math.clamp((image.maxHeightM - height) / (image.maxHeightM * 0.22), 0, 1)
      if (show) closeupStatus = image.status
    }
    const detailBounds = closeups[0]?.image.bounds
    if (detail) detail.show = inCaseScale && height < 8_500 && !!detailBounds && centerInReliefBounds(detailBounds)
    viewer.scene.screenSpaceCameraController.minimumZoomDistance = detail?.show ? 90 : 200

    let nextStatus: HistoricalReliefStatus | null = null
    if (historicalView) {
      if (closeupStatus) nextStatus = closeupStatus
      else if (localRelief && inCaseArea) nextStatus = 'visible'
      else if (surroundingRelief?.show && surroundings && centerInReliefBounds(surroundings.bounds)) nextStatus = 'surroundings'
      else if (localRelief) nextStatus = 'outside'
      else if (reliefFailed) nextStatus = 'unavailable'
    }
    if (reliefStatus !== nextStatus) {
      reliefStatus = nextStatus
      options.onHistoricalRelief?.(nextStatus)
    }
  }

  async function loadCloseup(entry: CloseupLayer): Promise<void> {
    entry.state = 'loading'
    try {
      const { image } = entry
      const provider = await cesium.SingleTileImageryProvider.fromUrl(image.url, {
        rectangle: cesium.Rectangle.fromDegrees(...rectArray(image.bounds)),
        credit: new cesium.Credit(image.attribution),
      })
      if (destroyed) return
      const above = closeups.slice(closeups.indexOf(entry) + 1).find((item) => item.layer)?.layer
      entry.layer = viewer.imageryLayers.addImageryProvider(provider, above ? viewer.imageryLayers.indexOf(above) : undefined)
      entry.state = 'ready'
      updateHistoricalView()
      viewer.scene.requestRender()
    } catch {
      entry.state = 'unavailable'
    }
  }

  async function loadHistoricalRelief(): Promise<void> {
    const relief = options.historicalRelief
    if (!relief || localRelief || loadingRelief) return
    loadingRelief = true
    reliefFailed = false
    try {
      const provider = await cesium.SingleTileImageryProvider.fromUrl(relief.url, {
        rectangle: cesium.Rectangle.fromDegrees(...rectArray(relief.bounds)),
        credit: new cesium.Credit('吉隆高程 © Mapzen / AWS Terrain Tiles'),
      })
      if (destroyed) return
      const layerAbove = closeups.find((item) => item.layer)?.layer
      const index = layerAbove ? viewer.imageryLayers.indexOf(layerAbove) : undefined
      localRelief = viewer.imageryLayers.addImageryProvider(provider, index)
      updateHistoricalView()
      viewer.scene.requestRender()
    } catch {
      if (!destroyed) {
        reliefFailed = true
        updateHistoricalView()
      }
    } finally {
      loadingRelief = false
    }
  }

  async function loadSurroundingRelief(): Promise<void> {
    const relief = options.historicalRelief?.surroundings
    if (!relief || surroundingRelief || loadingSurroundings) return
    loadingSurroundings = true
    try {
      const provider = await cesium.SingleTileImageryProvider.fromUrl(relief.url, {
        rectangle: cesium.Rectangle.fromDegrees(...rectArray(relief.bounds)),
        credit: new cesium.Credit('周边高程 © Mapzen / AWS Terrain Tiles'),
      })
      if (destroyed) return
      // 请求并行，周边影像始终垫在案例影像下方。
      const layerAbove = localRelief ?? closeups.find((item) => item.layer)?.layer
      const index = layerAbove ? viewer.imageryLayers.indexOf(layerAbove) : undefined
      surroundingRelief = viewer.imageryLayers.addImageryProvider(provider, index)
      updateHistoricalView()
      viewer.scene.requestRender()
    } catch {
      // 周边影像缺失时继续使用全域瓦片和已加载的案例影像。
    } finally {
      loadingSurroundings = false
    }
  }

  const weather = new cesium.CustomDataSource('map-weather')
  void viewer.dataSources.add(weather)
  let weatherLayer = EMPTY_WEATHER_LAYER
  let rainRequest = 0

  function updateRainOpacity(): void {
    if (rainImagery) rainImagery.alpha = weatherLayer.subdued ? 0.32 : 1
  }

  function removeRainImagery(): void {
    if (rainImagery) {
      viewer.imageryLayers.remove(rainImagery)
      rainImagery = null
    }
  }

  async function drawRain(rain: WeatherMapLayer['rain']): Promise<void> {
    const request = ++rainRequest
    if (!rain) {
      removeRainImagery()
      return
    }
    try {
      const provider = await cesium.SingleTileImageryProvider.fromUrl(rain.url, {
        rectangle: cesium.Rectangle.fromDegrees(...rectArray(rain.bounds)),
      })
      if (destroyed || request !== rainRequest) return
      removeRainImagery()
      rainImagery = viewer.imageryLayers.addImageryProvider(provider)
      updateRainOpacity()
      viewer.scene.requestRender()
    } catch {
      if (!destroyed && request === rainRequest) {
        // 失败时清掉过期色面，当前帧仍可查格点读数。
        removeRainImagery()
        viewer.scene.requestRender()
      }
    }
  }

  function drawWeather(): void {
    const { points, selected } = weatherLayer
    const markers = selected ? [...points, rainSelectionPoint(selected)] : points
    const ground = cesium.HeightReference.CLAMP_TO_GROUND
    const known = new Set(markers.map((item) => item.id))
    weather.entities.suspendEvents()
    try {
      weather.entities.removeAll()
      if (selected) {
        const { west, south, east, north } = selected.bounds
        weather.entities.add({ id: `outline-${selected.id}`, polyline: {
          positions: cesium.Cartesian3.fromDegreesArray([west, south, east, south, east, north, west, north, west, south]),
          material: new cesium.PolylineDashMaterialProperty({ color: cesium.Color.fromCssColorString(palette.labelText), dashLength: 10 }),
          clampToGround: true,
          width: 1.5,
        } })
      }
      for (const point of markers) {
        const color = cesium.Color.fromCssColorString(point.color)
        const centered = point.labelPlacement !== undefined || point.mainlandChina
        const below = point.labelPlacement === 'below'
        weather.entities.add({
          id: point.id,
          position: cesium.Cartesian3.fromDegrees(point.lonlat.lon, point.lonlat.lat),
          point: {
            pixelSize: 13, color, outlineColor: cesium.Color.fromCssColorString(palette.labelBackground), outlineWidth: 3,
            heightReference: ground, disableDepthTestDistance: Number.POSITIVE_INFINITY,
            distanceDisplayCondition: point.id.startsWith('weather-history-') ? new cesium.DistanceDisplayCondition(0, 160_000) : undefined,
          },
          label: {
            text: weatherMarkerText(point),
            font: point.labelPlacement ? '600 13px "Microsoft YaHei", sans-serif' : '600 14px "Microsoft YaHei", sans-serif',
            fillColor: cesium.Color.fromCssColorString(palette.labelText),
            showBackground: true,
            backgroundColor: cesium.Color.fromCssColorString(palette.labelBackground).withAlpha(0.94),
            backgroundPadding: point.labelPlacement ? new cesium.Cartesian2(7, 5) : new cesium.Cartesian2(9, 6),
            pixelOffset: new cesium.Cartesian2(centered ? 0 : -8, below ? 12 : -20),
            horizontalOrigin: centered ? cesium.HorizontalOrigin.CENTER : cesium.HorizontalOrigin.RIGHT,
            verticalOrigin: below ? cesium.VerticalOrigin.TOP : cesium.VerticalOrigin.BOTTOM,
            heightReference: ground, disableDepthTestDistance: Number.POSITIVE_INFINITY,
            distanceDisplayCondition: point.id.startsWith('weather-history-') ? new cesium.DistanceDisplayCondition(0, 160_000) : undefined,
          },
        })
      }
      for (const entity of history?.entities.values ?? []) {
        entity.show = !known.has(`weather-${entity.id}`) && !(entity.id === 'history-gyirong-port' && known.has('weather-gyirong-grid-reference'))
      }
    } finally {
      weather.entities.resumeEvents()
    }
    viewer.scene.requestRender()
  }

  function drawHistory(): void {
    if (!history || !options.historicalSites) return
    history.entities.removeAll()
    const accent = cesium.Color.fromCssColorString(palette.historicalMarker)
    const ground = cesium.HeightReference.CLAMP_TO_GROUND
    for (const site of options.historicalSites) {
      history.entities.add({
        id: site.id,
        position: cesium.Cartesian3.fromDegrees(site.lonlat.lon, site.lonlat.lat),
        point: {
          pixelSize: 14,
          color: accent,
          outlineColor: cesium.Color.fromCssColorString(palette.labelBackground),
          outlineWidth: 3,
          heightReference: ground,
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
        },
        label: {
          text: site.label,
          font: '600 15px "Microsoft YaHei", sans-serif',
          fillColor: cesium.Color.fromCssColorString(palette.labelText),
          showBackground: true,
          backgroundColor: cesium.Color.fromCssColorString(palette.labelBackground).withAlpha(0.95),
          backgroundPadding: new cesium.Cartesian2(9, 6),
          pixelOffset: new cesium.Cartesian2(0, -23),
          horizontalOrigin: cesium.HorizontalOrigin.CENTER,
          verticalOrigin: cesium.VerticalOrigin.BOTTOM,
          heightReference: ground,
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
        },
      })
    }
    if (!geography || !options.historicalGeography) return
    geography.entities.removeAll()
    const lineStyles = [
      { lines: options.historicalGeography.waterways, color: accent.withAlpha(0.72), zIndex: 0 },
      { lines: options.historicalGeography.roads, color: cesium.Color.fromCssColorString(palette.historicalRoad), zIndex: 1 },
    ]
    for (const { lines, color, zIndex } of lineStyles) {
      for (const line of lines) geography.entities.add({
        name: line.name,
        position: cesium.Cartesian3.fromDegrees(line.labelAt[0], line.labelAt[1]),
        polyline: {
          positions: cesium.Cartesian3.fromDegreesArray(line.coordinates.flat()),
          width: 3,
          clampToGround: true,
          zIndex,
          material: new cesium.PolylineOutlineMaterialProperty({
            color,
            outlineColor: cesium.Color.fromCssColorString(palette.labelBackground).withAlpha(0.72),
            outlineWidth: 1,
          }),
        },
        label: {
          text: line.name,
          font: '500 13px "Microsoft YaHei", sans-serif',
          fillColor: cesium.Color.fromCssColorString(palette.labelText),
          outlineColor: cesium.Color.fromCssColorString(palette.labelBackground),
          outlineWidth: 3,
          style: cesium.LabelStyle.FILL_AND_OUTLINE,
          pixelOffset: new cesium.Cartesian2(5, 10),
          heightReference: ground,
          distanceDisplayCondition: new cesium.DistanceDisplayCondition(0, 52_000),
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
        },
      })
    }
  }
  drawHistory()

  function drawCloseup(): void {
    if (!detail || !closeupDetails) return
    detail.entities.removeAll()
    const outline = cesium.Color.fromCssColorString(palette.labelBackground).withAlpha(0.8)
    const road = cesium.Color.fromCssColorString(palette.closeupRoad)
    const building = cesium.Color.fromCssColorString(palette.closeupBuilding)
    const water = cesium.Color.fromCssColorString(palette.historicalMarker)
    for (const way of closeupDetails.localRoads) {
      detail.entities.add({
        id: `local-road-${way.osmId}`,
        polyline: {
          positions: cesium.Cartesian3.fromDegreesArray(way.coordinates.flat()),
          material: new cesium.PolylineOutlineMaterialProperty({ color: road, outlineColor: outline, outlineWidth: 1 }),
          width: 4,
          zIndex: 0,
          clampToGround: true,
        },
      })
    }
    for (const [kind, features, color] of [
      ['building', closeupDetails.buildings, building.withAlpha(0.76)],
      ['water', closeupDetails.riverBanks, water.withAlpha(0.20)],
    ] as const) {
      for (const way of features) {
        detail.entities.add({
          id: `${kind}-${way.osmId}`,
          polygon: {
            hierarchy: cesium.Cartesian3.fromDegreesArray(way.coordinates.slice(0, -1).flat()),
            material: color,
            classificationType: cesium.ClassificationType.TERRAIN,
            zIndex: kind === 'building' ? 2 : 1,
          },
        })
      }
    }
  }
  drawCloseup()

  const signatures = {} as Record<LayerName, string>
  for (const name of LAYER_NAMES) signatures[name] = ''

  function rebuild<T extends { id: string }>(name: LayerName, features: readonly T[], add: (feature: T) => void): void {
    const signature = features.map((feature) => feature.id).join('|')
    if (signatures[name] === signature) return
    signatures[name] = signature
    handles[name].entities.removeAll()
    for (const feature of features) add(feature)
    viewer.scene.requestRender()
  }

  let lastData: RenderData | null = null

  function render(data: RenderData): void {
    lastData = data
    // 本场景只连同源自托管资产（WGS84 烘焙瓦），坐标不做任何变换；'amap' 档是页面级的
    // 另一套渲染面（AmapMap.vue），它的纠偏在那边做，与这里无关。
    rebuild('hazards', data.hazards, (zone) => handles.hazards.entities.add(hazardZoneGraphics(cesium, zone, cesium.ClassificationType.TERRAIN, palette)))
    rebuild('stations', data.stations, (feature) => handles.stations.entities.add(stationGraphics(cesium, feature, palette)))
    rebuild('warnings', data.warnings, (feature) => handles.warnings.entities.add(warningGraphics(cesium, feature, palette)))
    rebuild('reach', data.reach, (feature) => handles.reach.entities.add(reachGraphics(cesium, feature, palette)))
    rebuild('clusters', data.clusters, (cluster) => handles.clusters.entities.add(clusterGraphics(cesium, cluster, palette)))
  }

  /**
   * 换主题：换调色板 → 重画。
   * 签名要逐层抹成一个不可能等于真实签名的哨兵值：图元色是 `entities.add()` 时烤进去的，
   * 而"要素集合没变"这条幂等判据正好会把它挡掉——不抹掉的话，翻主题后底翻面、标记不翻面。
   */
  function setTheme(mode: ThemeMode): void {
    if (palette.mode === mode) return
    palette = mapPalette(mode)
    applyPalette()
    for (const name of LAYER_NAMES) signatures[name] = '#aegis-rebuild-for-theme#'
    if (lastData) render(lastData)
    drawHistory()
    drawCloseup()
    if (weatherLayer.points.length) drawWeather()
    viewer.scene.requestRender()
  }

  const handler = new cesium.ScreenSpaceEventHandler(viewer.scene.canvas)
  handler.setInputAction((event: ScreenSpaceEventHandler.PositionedEvent) => {
    const picked = viewer.scene.pick(event.position) as { id?: unknown } | undefined
    const feature = picked?.id
    const id = typeof feature === 'object' && feature !== null && 'id' in feature ? feature.id : feature
    if (typeof id === 'string' && !id.startsWith('outline-weather-cell-')) {
      options.onSelect?.(id)
      return
    }
    const point = weatherLayer.cells.length ? pickGround(event.position) : undefined
    if (point) {
      const { latitude, longitude } = viewer.scene.globe.ellipsoid.cartesianToCartographic(point)
      const cell = rainCellAt(weatherLayer.cells, { lon: cesium.Math.toDegrees(longitude), lat: cesium.Math.toDegrees(latitude) })
      options.onSelect?.(cell?.id ?? null)
    } else options.onSelect?.(null)
  }, cesium.ScreenSpaceEventType.LEFT_CLICK)

  const onCameraChanged = (): void => {
    updateWorldView()
    updateHistoricalView()
    options.onViewChanged?.(currentBBox(), currentZoom())
    if (options.onScaleChanged) options.onScaleChanged(mapScale())
  }
  viewer.camera.percentageChanged = options.onScaleChanged ? 0.08 : 0.25
  viewer.camera.changed.addEventListener(onCameraChanged)
  if (options.onScaleChanged || history) viewer.camera.moveEnd.addEventListener(onCameraChanged)
  // 装配完先推一次：`camera.changed` 只在**变化**时发，不推的话面板一直停在"视野：未就绪"、
  // 缩放报 0，而且按视野过滤图层的那条路一直拿不到 bbox（浏览器实测：不动鼠标就一直是未就绪）。
  onCameraChanged()

  function currentBBox(): BBox | null {
    const rectangle = viewer.camera.computeViewRectangle()
    if (!rectangle) return null
    return {
      west: cesium.Math.toDegrees(rectangle.west),
      south: cesium.Math.toDegrees(rectangle.south),
      east: cesium.Math.toDegrees(rectangle.east),
      north: cesium.Math.toDegrees(rectangle.north),
    }
  }

  function currentZoom(): number {
    const position = viewer.camera.positionCartographic
    return zoomForCameraHeight(position ? position.height : Number.POSITIVE_INFINITY)
  }

  function pickGround(pixel: Cartesian2) {
    const ray = viewer.camera.getPickRay(pixel)
    return (ray && viewer.scene.globe.pick(ray, viewer.scene)) ?? viewer.camera.pickEllipsoid(pixel, viewer.scene.globe.ellipsoid)
  }

  function mapScale(): MapScale | null {
    const canvas = viewer.scene.canvas
    const x = canvas.clientWidth / 2
    const y = canvas.clientHeight / 2
    const ellipsoid = viewer.scene.globe.ellipsoid
    const groundAt = (screenX: number) => pickGround(new cesium.Cartesian2(screenX, y))
    const left = groundAt(x - 50)
    const right = groundAt(x + 50)
    if (!left || !right) return null
    const distance = new cesium.EllipsoidGeodesic(
      ellipsoid.cartesianToCartographic(left),
      ellipsoid.cartesianToCartographic(right),
      ellipsoid,
    ).surfaceDistance
    if (!Number.isFinite(distance) || distance <= 0) return null
    const unit = 10 ** Math.floor(Math.log10(distance))
    const step = distance / unit
    const meters = unit * (step >= 5 ? 5 : step >= 2 ? 2 : 1)
    return { meters, width: (100 * meters) / distance }
  }

  let pendingFitBounds: BBox | null = null
  const clearPendingFit = (): void => { pendingFitBounds = null }

  return {
    render,
    setWeather: (layer) => {
      if (weatherLayer === layer) return
      const rainChanged = weatherLayer.rain !== layer.rain
      weatherLayer = layer
      if (rainChanged) void drawRain(layer.rain)
      else updateRainOpacity()
      drawWeather()
    },
    setTheme,
    get palette() {
      return palette
    },
    focusAt: (point) => {
      viewer.camera.flyTo({ destination: cesium.Cartesian3.fromDegrees(point.lon, point.lat, 20_000), duration: 1.2 })
    },
    fitBBox: (box) => {
      viewer.camera.cancelFlight()
      pendingFitBounds = box
      viewer.camera.flyTo({
        destination: cesium.Rectangle.fromDegrees(...rectArray(box)),
        duration: 1.2,
        complete: clearPendingFit,
        cancel: clearPendingFit,
      })
    },
    zoomBy: (direction) => {
      const position = viewer.camera.positionCartographic
      const ground = viewer.scene.globe.getHeight(position) ?? 0
      const height = Math.max(0, position.height - ground)
      const { minimumZoomDistance, maximumZoomDistance: limit } = viewer.scene.screenSpaceCameraController
      if (direction === -1 && height >= limit) return
      const next = cesium.Math.clamp(height * (direction === 1 ? 0.66 : 1.5), minimumZoomDistance * 1.3, limit)
      viewer.camera.cancelFlight()
      viewer.camera.zoomIn(height - next)
      viewer.scene.requestRender()
    },
    basemapSwitchBounds: () => pendingFitBounds ?? currentBBox(),
    currentZoom,
    setLayerVisible: (layer, visible) => {
      handles[layer].dataSource.show = visible
      viewer.scene.requestRender()
    },
    setHistoryVisible: (visible) => {
      if (!history) return
      historicalView = visible
      viewer.scene.screenSpaceCameraController.maximumZoomDistance = visible ? CASE_ZOOM_DISTANCE_M : GLOBAL_ZOOM_DISTANCE_M
      updateHistoricalView()
      if (visible) {
        void loadHistoricalRelief()
        void loadSurroundingRelief()
      }
      viewer.scene.requestRender()
    },
    destroy: () => {
      if (destroyed) return
      destroyed = true
      resizeObserver?.disconnect()
      viewer.camera.changed.removeEventListener(onCameraChanged)
      if (options.onScaleChanged || history) viewer.camera.moveEnd.removeEventListener(onCameraChanged)
      handler.removeInputAction(cesium.ScreenSpaceEventType.LEFT_CLICK)
      handler.destroy()
      viewer.dataSources.removeAll(true)
      viewer.destroy()
      // 句柄必须跟着销毁：SPA 里切走再切回来，旧 Viewer 已经没了，留着只会读到一堆 destroyed 报错。
      clearMapDebugHandle()
    },
    info: {
      basemapMessage: basemapSetup.message,
      terrainMessage: terrainSetup.message,
      basemapSource: source.kind,
      terrainMode: terrainSetup.mode,
    },
  }
}
