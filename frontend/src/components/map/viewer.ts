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
  ClassificationType,
  CustomDataSource,
  Entity,
  EntityCollection,
  ScreenSpaceEventHandler,
  TerrainProvider,
  Viewer as CesiumViewer,
} from 'cesium'

import type { BasemapConfig, BasemapSource } from './basemap'
import { chooseBasemapSource, createBasemapSetup, DEFAULT_BASEMAP_CONFIG } from './basemap'
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
import type { ProbeFetch } from './offline'
import { collectFacts, tileUrlForTemplate } from './offline'
import { chooseTerrainMode, createTerrainSetup, DEFAULT_TERRAIN_URL, terrainProbeUrl } from './terrain'

/** Cesium 命名空间的类型别名（type-only，不产生运行时导入）。 */
type CesiumModule = typeof import('cesium')

/** 图层词汇表定义在 entities.ts（视图与渲染共用一份，避免两处枚举漂移），此处只做别名。 */
export type LayerName = PlanLayer

const LAYER_NAMES: readonly LayerName[] = PLAN_LAYERS

/** 无影像时的地球底色：深色而不是白屏，避免"看起来像崩了"。 */
const GLOBE_BASE_COLOR = '#101923'

/** 渲染端的显隐状态形状（值由视图持有，本文件只施加）。 */
export type LayerVisibility = Record<LayerName, boolean>

/** 渲染载荷：契约定义在 entities.ts（纯逻辑层），本文件只按它画，不再另立形状。 */
export type RenderData = RenderPlan

export interface MapSceneOptions {
  terrainUrl?: string
  basemap?: Partial<BasemapConfig>
  initialRectangle?: BBox
  /** 探针用 fetch：生产传 window.fetch，特殊环境可注入假实现。 */
  fetchLike?: ProbeFetch
  browserOnline?: () => boolean
  origin?: string
  onViewChanged?: (bbox: BBox | null, zoom: number) => void
  onSelect?: (featureId: string | null) => void
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
  focusAt(point: LngLat): void
  fitBBox(box: BBox): void
  currentBBox(): BBox | null
  currentZoom(): number
  setLayerVisible(layer: LayerName, visible: boolean): void
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

/** 站点：有台账高程时用绝对高度（地形缺失也不贴地），否则钳地。高危等级才出文字标签。 */
function stationGraphics(cesium: CesiumModule, feature: StationFeature & { lonlat: LngLat }): Entity.ConstructorOptions {
  const severe = feature.riskLevel !== null && feature.riskLevel <= 2
  return {
    id: feature.id,
    name: feature.title,
    position: cesium.Cartesian3.fromDegrees(feature.lonlat.lon, feature.lonlat.lat, feature.elevationM ?? 0),
    point: {
      pixelSize: severe ? 15 : 10,
      color: cesium.Color.fromCssColorString(feature.colorCss),
      outlineColor: cesium.Color.WHITE,
      outlineWidth: 2,
      heightReference: feature.elevationM === null ? cesium.HeightReference.CLAMP_TO_GROUND : undefined,
      disableDepthTestDistance: Number.POSITIVE_INFINITY,
    },
    label: severe
      ? {
          text: feature.nameZh,
          font: '12px sans-serif',
          fillColor: cesium.Color.WHITE,
          outlineColor: cesium.Color.BLACK,
          outlineWidth: 2,
          pixelOffset: new cesium.Cartesian2(0, -18),
          showBackground: true,
          backgroundColor: cesium.Color.fromCssColorString(GLOBE_BASE_COLOR).withAlpha(0.7),
        }
      : undefined,
  }
}

/** 预警：区域级 marker，空心环形色带，等级由颜色承担，标题留在详情面板。 */
function warningGraphics(cesium: CesiumModule, feature: WarningFeature & { lonlat: LngLat }): Entity.ConstructorOptions {
  const color = cesium.Color.fromCssColorString(feature.colorCss)
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
function reachGraphics(cesium: CesiumModule, feature: ReachFeature & { lonlat: LngLat }): Entity.ConstructorOptions {
  return {
    id: feature.id,
    name: feature.title,
    position: cesium.Cartesian3.fromDegrees(feature.lonlat.lon, feature.lonlat.lat),
    point: {
      pixelSize: 8,
      color: cesium.Color.fromCssColorString(feature.colorCss),
      outlineColor: cesium.Color.BLACK,
      outlineWidth: 1,
      heightReference: cesium.HeightReference.CLAMP_TO_GROUND,
      disableDepthTestDistance: Number.POSITIVE_INFINITY,
    },
    label: {
      text: feature.title,
      font: '11px sans-serif',
      fillColor: cesium.Color.WHITE,
      pixelOffset: new cesium.Cartesian2(0, 14),
      showBackground: true,
      backgroundColor: cesium.Color.fromCssColorString(GLOBE_BASE_COLOR).withAlpha(0.6),
    },
  }
}

/** 灾害面：贴地分类（TERRAIN）→ 地形没烘焙时自动贴在椭球面上，同一份数据两种形态都对齐。 */
function hazardZoneGraphics(cesium: CesiumModule, feature: HazardZoneFeature, classificationType: ClassificationType): Entity.ConstructorOptions {
  const outer = feature.outer.map((point) => cesium.Cartesian3.fromDegrees(point.lon, point.lat))
  const holes = feature.holes.map(
    (ring) => new cesium.PolygonHierarchy(ring.map((point) => cesium.Cartesian3.fromDegrees(point.lon, point.lat))),
  )
  const color = cesium.Color.fromCssColorString(feature.colorCss)
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
function clusterGraphics(cesium: CesiumModule, cluster: PointCluster): Entity.ConstructorOptions {
  return {
    id: cluster.id,
    position: cesium.Cartesian3.fromDegrees(cluster.lonlat.lon, cluster.lonlat.lat),
    point: {
      pixelSize: Math.min(44, 18 + Math.log2(Math.max(2, cluster.count)) * 6),
      color: cesium.Color.fromCssColorString(cluster.colorCss).withAlpha(0.85),
      outlineColor: cesium.Color.WHITE,
      outlineWidth: 2,
      heightReference: cesium.HeightReference.CLAMP_TO_GROUND,
      disableDepthTestDistance: Number.POSITIVE_INFINITY,
    },
    label: {
      text: String(cluster.count),
      font: 'bold 12px sans-serif',
      fillColor: cesium.Color.WHITE,
      outlineColor: cesium.Color.BLACK,
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
export async function createMapScene(el: HTMLElement, options: MapSceneOptions = {}): Promise<MapScene> {
  ensureCesiumBaseUrl()
  const cesium = await import('cesium')

  const basemapConfig: BasemapConfig = { ...DEFAULT_BASEMAP_CONFIG, ...options.basemap }
  const terrainUrl = options.terrainUrl ?? DEFAULT_TERRAIN_URL
  const fetchLike: ProbeFetch =
    options.fetchLike ??
    ((url, init) => {
      if (typeof fetch === 'undefined') return Promise.reject(new Error('当前环境没有 fetch'))
      return fetch(url, init)
    })
  const browserOnline = options.browserOnline ?? (() => globalThis.navigator?.onLine !== false)

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

  const viewer: CesiumViewer = new cesium.Viewer(el, {
    baseLayer: basemapSetup.layer,
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

  viewer.scene.globe.baseColor = cesium.Color.fromCssColorString(GLOBE_BASE_COLOR)
  viewer.scene.globe.enableLighting = false
  viewer.scene.screenSpaceCameraController.maximumZoomDistance = 40_000_000
  viewer.scene.screenSpaceCameraController.minimumZoomDistance = 200
  viewer.camera.setView({ destination: cesium.Rectangle.fromDegrees(...rectArray(options.initialRectangle ?? TIBET_RECTANGLE)) })

  const handles = createLayerHandles(cesium, viewer)
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

  function render(data: RenderData): void {
    rebuild('hazards', data.hazards, (zone) => handles.hazards.entities.add(hazardZoneGraphics(cesium, zone, cesium.ClassificationType.TERRAIN)))
    rebuild('stations', data.stations, (feature) => handles.stations.entities.add(stationGraphics(cesium, feature)))
    rebuild('warnings', data.warnings, (feature) => handles.warnings.entities.add(warningGraphics(cesium, feature)))
    rebuild('reach', data.reach, (feature) => handles.reach.entities.add(reachGraphics(cesium, feature)))
    rebuild('clusters', data.clusters, (cluster) => handles.clusters.entities.add(clusterGraphics(cesium, cluster)))
  }

  const handler = new cesium.ScreenSpaceEventHandler(viewer.scene.canvas)
  handler.setInputAction((event: ScreenSpaceEventHandler.PositionedEvent) => {
    const picked = viewer.scene.pick(event.position) as { id?: unknown } | undefined
    options.onSelect?.(typeof picked?.id === 'string' ? picked.id : null)
  }, cesium.ScreenSpaceEventType.LEFT_CLICK)

  const onCameraChanged = (): void => {
    options.onViewChanged?.(currentBBox(), currentZoom())
  }
  viewer.camera.percentageChanged = 0.25
  viewer.camera.changed.addEventListener(onCameraChanged)
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

  let destroyed = false
  return {
    render,
    focusAt: (point) => {
      viewer.camera.flyTo({ destination: cesium.Cartesian3.fromDegrees(point.lon, point.lat, 20_000), duration: 1.2 })
    },
    fitBBox: (box) => {
      viewer.camera.flyTo({ destination: cesium.Rectangle.fromDegrees(...rectArray(box)), duration: 1.2 })
    },
    currentBBox,
    currentZoom,
    setLayerVisible: (layer, visible) => {
      handles[layer].dataSource.show = visible
      viewer.scene.requestRender()
    },
    destroy: () => {
      if (destroyed) return
      destroyed = true
      viewer.camera.changed.removeEventListener(onCameraChanged)
      handler.removeInputAction(cesium.ScreenSpaceEventType.LEFT_CLICK)
      handler.destroy()
      viewer.dataSources.removeAll(true)
      viewer.destroy()
    },
    info: {
      basemapMessage: basemapSetup.message,
      terrainMessage: terrainSetup.message,
      basemapSource: source.kind,
      terrainMode: terrainSetup.mode,
    },
  }
}
