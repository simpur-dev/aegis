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
import { BASEMAP_ATTRIBUTION, chooseBasemapSource, createBasemapSetup, DEFAULT_BASEMAP_CONFIG } from './basemap'
import { mapPalette, type MapPalette } from '@/theme/map'
import type { ThemeMode } from '@/theme/tokens'
import { clearMapDebugHandle, publishMapDebugHandle } from './debugHandle'
import { createEnergyPlanetCanvas } from './energyPlanet'
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

/** 无影像时的地球底色等画面内部的颜色，全部由 `@/theme/map` 按当前主题派生（本文件不写字面色）。 */

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
  /** 建场时所处主题；之后的翻转由 `setTheme` 推进。缺省按浅色。 */
  themeMode?: ThemeMode
  /**
   * 「深空皮肤」：拉远到能看见地球弧线时，给地球叠一层程序化能量纹理 + 蓝色大气边缘 + 泛光。
   *
   * 默认关：其余页面（以及 `viewer.theme.spec.ts` 钉住的画面口径）保持原样，
   * 只有「一张图」传 true。区域视图（默认约 1.7e6 m）不叠纹理，底图与要素色不变。
   */
  spaceLook?: boolean
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

/**
 * 「深空视图」的高度门槛（米）。默认区域视野（西藏全域）相机高度约 1.7e6 m，
 * 这个高度上还在看地图；3.5e6 m 以上才看得见地球弧线与大气边缘，那时才叠能量纹理。
 */
const SPACE_LOOK_HEIGHT_M = 3_500_000

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

  let palette = mapPalette(options.themeMode ?? 'light')
  const initialRectangle = options.initialRectangle ?? TIBET_RECTANGLE
  /**
   * 建场那一刻容器若还没有宽度，`setView` 会算出全球视野（真机：宽度 0 时视野 -180..180、缩放 0）；
   * 拿到尺寸后必须补一次，否则画布恢复成 811×490 了、画面却停在星空里。
   */
  let viewApplied = el.clientWidth > 0
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

  /**
   * 主题落到场景上：地球底色 + 画面外底色，并把太阳/月亮/天空大气关掉——
   * 那三样是不表达任何状态的装饰光，而深色档下那条蓝色地平线辉光是画布里唯一不肯翻面的亮面。
   */
  function applyPalette(): void {
    viewer.scene.globe.baseColor = cesium.Color.fromCssColorString(palette.globeBase)
    viewer.scene.backgroundColor = cesium.Color.fromCssColorString(palette.spaceBackdrop)
    if (viewer.scene.skyAtmosphere) viewer.scene.skyAtmosphere.show = false
    if (viewer.scene.sun) viewer.scene.sun.show = false
    if (viewer.scene.moon) viewer.scene.moon.show = false
  }
  /**
   * 深空皮肤（仅 options.spaceLook）：深蓝地球 + 蓝色大气边缘 + 泛光。
   *
   * 只改"看着像能量球"的那几件事——地球上的影像、地形与要素一个像素都不动，
   * 所以拉回区域视图时还是原来那张图。默认关，其余页面与 `viewer.theme.spec.ts`
   * 钉住的画面口径都不受影响。
   */
  function applySpaceLook(): void {
    if (!options.spaceLook) return
    const scene = viewer.scene
    if (scene.skyAtmosphere) {
      scene.skyAtmosphere.show = true
      scene.skyAtmosphere.hueShift = 0.62
      scene.skyAtmosphere.saturationShift = 0.42
      scene.skyAtmosphere.brightnessShift = -0.3
      scene.skyAtmosphere.atmosphereLightIntensity = 7
    }
    scene.globe.baseColor = cesium.Color.fromCssColorString(palette.spaceGlobe)
    scene.backgroundColor = cesium.Color.fromCssColorString(palette.deepSpace)
    const bloom = scene.postProcessStages?.bloom
    if (!bloom) return
    bloom.uniforms.glowOnly = false
    bloom.uniforms.brightness = -0.35
    bloom.uniforms.contrast = 128
    bloom.uniforms.delta = 1
    bloom.uniforms.sigma = 2
    bloom.uniforms.stepSize = 1
  }

  /**
   * 覆盖在球面上的程序化能量纹理。只在第一次真正进入深空视图时才生成——
   * 常规区域视图进页面不该为一张 1024×512 的噪声付渲染成本。
   */
  let energyLayer: { alpha: number } | null = null
  let energyLayerFailed = false

  function ensureEnergyLayer(): void {
    if (energyLayer || energyLayerFailed) return
    try {
      const canvas = createEnergyPlanetCanvas()
      const provider = new cesium.SingleTileImageryProvider({
        url: canvas.toDataURL('image/png'),
        rectangle: cesium.Rectangle.MAX_VALUE,
        // Cesium 1.x 把这两个当必填：缺了会抛 DevError 并停掉渲染循环
        tileWidth: canvas.width,
        tileHeight: canvas.height,
      })
      const layer = viewer.imageryLayers.addImageryProvider(provider) as unknown as { alpha: number }
      layer.alpha = 0
      energyLayer = layer
    } catch (error) {
      // 纹理只是观感增强：拿不到就退回纯深蓝地球，绝不能让一张贴图把地图渲染搞停
      energyLayerFailed = true
      console.warn('[map] 深空纹理不可用，回退到纯深蓝地球：', error)
    }
  }

  /**
   * 按相机高度切"深空视图"：只有看得见地球弧线时才叠能量纹理、开泛光。
   * 区域视图（默认约 1.7e6 m）纹理透明度归零，底图、要素、取色全部保持原样。
   */
  function updateSpaceView(): void {
    if (!options.spaceLook) return
    const inSpace = viewer.camera.positionCartographic.height > SPACE_LOOK_HEIGHT_M
    el.classList?.toggle('is-space-view', inSpace)
    const bloom = viewer.scene.postProcessStages?.bloom
    if (bloom) bloom.enabled = inSpace
    if (inSpace) ensureEnergyLayer()
    if (energyLayer) {
      const next = inSpace ? 0.97 : 0
      if (energyLayer.alpha !== next) {
        energyLayer.alpha = next
        viewer.scene.requestRender()
      }
    }
  }

  applyPalette()
  applySpaceLook()
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
  viewer.scene.globe.tileLoadProgressEvent.addEventListener(() => {
    viewer.scene.requestRender()
  })
  viewer.scene.screenSpaceCameraController.maximumZoomDistance = 40_000_000
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
      viewer.scene.requestRender()
      requestAnimationFrame(() => viewer.scene.requestRender())
    })
  }
  // `?mapDebug` 时才挂只读句柄：视觉烟雾门禁要读 globe.getHeight() 这类真场景状态（见 debugHandle.ts）。
  publishMapDebugHandle(viewer, location.search)

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
    // 深空皮肤会覆盖底色/大气，翻主题后要按新档再上一次，否则翻一次就掉回普通画布
    applySpaceLook()
    for (const name of LAYER_NAMES) signatures[name] = '#aegis-rebuild-for-theme#'
    if (lastData) render(lastData)
    viewer.scene.requestRender()
  }

  const handler = new cesium.ScreenSpaceEventHandler(viewer.scene.canvas)
  handler.setInputAction((event: ScreenSpaceEventHandler.PositionedEvent) => {
    const picked = viewer.scene.pick(event.position) as { id?: unknown } | undefined
    options.onSelect?.(typeof picked?.id === 'string' ? picked.id : null)
  }, cesium.ScreenSpaceEventType.LEFT_CLICK)

  const onCameraChanged = (): void => {
    updateSpaceView()
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
    setTheme,
    get palette() {
      return palette
    },
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
      resizeObserver?.disconnect()
      viewer.camera.changed.removeEventListener(onCameraChanged)
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
