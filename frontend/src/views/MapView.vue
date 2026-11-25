<script setup lang="ts">
/**
 * 一张图（MapView）—— 自托管离线三维底图上的山地灾害态势。
 *
 * 本页刻意只做三件事：**取数、装配纯逻辑产物、把产物交给 viewer.ts 渲染**。
 * 判断（坐标、配色、聚合、视野过滤、离线状态）全部在 `components/map/entities.ts` 与
 * `offline.ts`（无框架、已单测）；渲染全部在 `viewer.ts`（唯一接触 WebGL 的文件，
 * 且 cesium 由 `await import()` 取，不进首屏包）。
 *
 * 离线口径：底图 = 同源烘焙的 PMTiles（退化到 XYZ 模板，再退化到"无影像、只画地形+矢量"）；
 * 地形 = 同源 quantized-mesh（探不到 `layer.json` 时用椭球面兜底）。全程只连同源资产，无任何第三方底图/地形/地理编码服务。
 */

import { message } from 'ant-design-vue'
import { AimOutlined, AppstoreOutlined, CloudOutlined, DownOutlined, InfoCircleOutlined, MinusOutlined, PlusOutlined, ReloadOutlined, SettingOutlined } from '@ant-design/icons-vue'
import { useElementSize } from '@vueuse/core'
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { useRoute } from 'vue-router'

import mapApi, { isMissingResource, MAP_ASSET_PATHS, normalizeAnchors } from '@/api/map'
import type { RegionAnchorDto, StationDto } from '@/api/map'
import type { ChainSummary, RiskLevel, TelemetryReading, WarningRecord } from '@/api/types'
import type { BBox, LayerVisibility, MapLayers, PlanLayer } from '@/components/map/entities'
import PageHero from '@/components/PageHero.vue'
import {
  bboxOfPoints,
  buildLayers,
  buildRenderPlan,
  DEFAULT_LAYER_VISIBILITY,
  detailOf,
  expandBBox,
  filterLayersByRisk,
  filterLayersByViewport,
  focusAllNote,
  layerCounts,
  plottedCount,
  riskLevelCounts,
  TIBET_RECTANGLE,
  toUnlocatedItems,
} from '@/components/map/entities'
import {
  collectFacts,
  initialMachine,
  NOT_PROBED_YET,
  reduceOffline,
  tileUrlForTemplate,
} from '@/components/map/offline'
import type { OfflineMachine } from '@/components/map/offline'
import MapStatusPanel from '@/components/map/MapStatusPanel.vue'
import MapLayersPanel from '@/components/map/MapLayersPanel.vue'
import MapWeatherPanel from '@/components/map/MapWeatherPanel.vue'
import AmapMap from '@/components/map/AmapMap.vue'
import { GYIRONG_CASE, type HistoricalSite } from '@/components/map/gyirongCase'
import gyirongRelief from '@/components/map/gyirong-relief.json'
import gyirongGeography from '@/components/map/gyirong-geography.json'
import { terrainProbeUrl } from '@/components/map/terrain'
import type { HistoricalReliefStatus, LayerName, MapScale, MapScene, MapSceneInfo } from '@/components/map/viewer'
import { useTheme } from '@/theme/useTheme'
import { readBasemapMode, writeBasemapMode, type BasemapMode } from '@/components/map/external'
import { EMPTY_WEATHER_LAYER, WEATHER_MODES, type WeatherMapLayer, type WeatherMode } from '@/components/map/weather'

/** 数据轮询与源探针周期：野外弱网下"少而稳"胜过"快而抖"。 */
const DATA_REFRESH_MS = 30_000
const PROBE_REFRESH_MS = 20_000

/** host 必须是空容器：Cesium 往里塞 canvas，占位文案做它的兄弟节点（避免被清空/遮挡）。 */
const host = ref<HTMLElement | null>(null)
const scene = shallowRef<MapScene | null>(null)
const sceneInfo = ref<MapSceneInfo | null>(null)
const sceneError = ref<string | null>(null)
const sceneLoading = ref(false)
const scale = ref<MapScale | null>(null)
const scaleText = computed(() => {
  const meters = scale.value?.meters ?? 0
  return meters >= 1000 ? `${meters / 1000} km` : `${meters} m`
})
const terrainRamp = `linear-gradient(90deg, ${gyirongRelief.stops.map(([, color]) => color).join(', ')})`
const reliefStatus = ref<HistoricalReliefStatus | null>(null)
const globalTerrainVisible = ref(false)
const amapPanel = ref<InstanceType<typeof AmapMap> | null>(null)
let sceneSeq = 0
/** 画布内部读不到 CSS 变量，所以主题要显式喂给场景（见 `@/theme/map`）。 */
const { resolved: themeMode } = useTheme()
const route = useRoute()
const storage = typeof localStorage === 'undefined' ? null : localStorage
/**
 * 底图档。默认离线自托管（交付形态不依赖外部主机，见 external.ts）；'amap' 整块换成
 * 高德 JS API 渲染面（`AmapMap.vue`）。场景层（viewer.ts）永远是离线链、不感知这个档——
 * 换档是页面级"换组件"，不是换影像层。
 */
const basemapMode = ref<BasemapMode>(readBasemapMode(storage))
const basemapWarning = ref('')

const readings = ref<TelemetryReading[]>([])
const warnings = ref<WarningRecord[]>([])
const chains = ref<ChainSummary[]>([])
const stations = ref<StationDto[]>([])
const anchors = ref<RegionAnchorDto[]>([])
const hazardDocument = ref<unknown>(null)

const loading = ref(false)
const autoRefresh = ref(true)
type MapPreset = 'gyirong' | 'tibet'
const preset = ref<MapPreset>(route.query.view === 'tibet' ? 'tibet' : 'gyirong')
const isCase = computed(() => preset.value === 'gyirong')
const detailedTerrainVisible = computed(() => basemapMode.value === 'offline' && isCase.value && reliefStatus.value === 'visible')
const closeupSource = computed(() => {
  if (basemapMode.value !== 'offline' || !isCase.value) return null
  if (reliefStatus.value === 'street') return gyirongRelief.street
  return reliefStatus.value === 'closeup' ? gyirongRelief.closeup : null
})
const outsideDetailedTerrain = computed(() =>
  basemapMode.value === 'offline' && isCase.value && (reliefStatus.value === 'outside' || reliefStatus.value === 'surroundings'),
)
const basemapSummary = computed(() => {
  if (basemapMode.value === 'amap') return '高德卫星'
  if (closeupSource.value) return reliefStatus.value === 'street' ? '口岸高清' : '口岸近景'
  if (globalTerrainVisible.value) return '全球地形'
  if (detailedTerrainVisible.value) return '精细地形'
  if (isCase.value && reliefStatus.value === 'surroundings') return '周边地形'
  return outsideDetailedTerrain.value || !isCase.value ? '全域地形' : '离线地形'
})
const panelsCollapsed = ref(isCase.value)
const caseExpanded = ref<boolean | null>(null)
const activeCaseSite = shallowRef<HistoricalSite | null>(null)
const shellRef = ref<HTMLElement | null>(null)
const toolbarRef = ref<HTMLElement | null>(null)
const { width: mapWidth, height: mapHeight } = useElementSize(shellRef)
const { height: toolbarHeight } = useElementSize(toolbarRef, undefined, { box: 'border-box' })
const compact = computed(() => mapWidth.value > 0 && mapWidth.value < 820)
const settingsOpen = ref(false)
const mobilePane = ref<'status' | 'layers' | null>(null)
const weatherOpen = ref(false)
const weatherMode = ref<WeatherMode>('current')
const weatherSummary = ref('')
const weatherPicked = ref<string | null>(null)
const weatherLayer = shallowRef<WeatherMapLayer>(EMPTY_WEATHER_LAYER)
const weatherViewport = shallowRef<BBox | null>(null)
const subduedRain = computed(() => {
  const bounds = weatherViewport.value
  return weatherMode.value === 'daily' && !!bounds && Math.min(bounds.east - bounds.west, bounds.north - bounds.south) <= 0.10
})
const visibleWeather = computed(() => {
  if (weatherMode.value === 'hourly' ? isCase.value : !isCase.value) return EMPTY_WEATHER_LAYER
  const layer = weatherLayer.value
  const selected = layer.cells.find((cell) => cell.id === weatherPicked.value)
  return selected || subduedRain.value ? { ...layer, selected, subdued: subduedRain.value } : layer
})
const rainReplay = computed(() => weatherMode.value !== 'current' && (weatherMode.value === 'daily' ? isCase.value : !isCase.value))
const weatherButtonLabel = computed(() => weatherMode.value === 'daily' ? '日雨量回放' : weatherMode.value === 'hourly' ? '小时回放' : isCase.value && weatherSummary.value ? weatherSummary.value : '天气雨情')
const showCaseCard = computed(() =>
  isCase.value && weatherMode.value === 'current' && (compact.value ? mobilePane.value === null && !settingsOpen.value && !weatherOpen.value : panelsCollapsed.value),
)
const showCaseDetails = computed(() => caseExpanded.value ?? !compact.value)
const cameraBounds = shallowRef<BBox>(isCase.value ? GYIRONG_CASE.bounds : TIBET_RECTANGLE)
const cameraInMainland = ref(!isCase.value)
const fitCurrentViewport = ref(false)
const amapCamera = computed(() => {
  const { west, east, south, north } = cameraBounds.value
  // 高德相机按当前容器宽度取整档，窄屏仍要能同时容纳两个案例点。
  let zoom = isCase.value ? (east - west < 0.1 ? 13.5 : compact.value ? 11 : 12.5) : 5
  if (fitCurrentViewport.value && mapWidth.value && mapHeight.value) {
    const mercatorY = (lat: number) => {
      const radians = Math.max(-85, Math.min(85, lat)) * Math.PI / 180
      return Math.asinh(Math.tan(radians)) / (2 * Math.PI)
    }
    const xZoom = Math.log2((360 * mapWidth.value) / (256 * (east - west)))
    const yZoom = Math.log2(mapHeight.value / (256 * (mercatorY(north) - mercatorY(south))))
    zoom = Math.max(3, Math.min(18, xZoom, yZoom))
  }
  return {
    center: { lon: (west + east) / 2, lat: (south + north) / 2 },
    zoom,
    pitch: isCase.value && zoom < 13 ? 8 : 0,
    mainlandChina: cameraInMainland.value,
  }
})
function toggleMobilePane(pane: 'status' | 'layers'): void {
  weatherOpen.value = false
  mobilePane.value = mobilePane.value === pane ? null : pane
}

function openWeather(): void {
  weatherOpen.value = true
  panelsCollapsed.value = true
  mobilePane.value = null
  settingsOpen.value = false
}

function toggleWeather(): void {
  if (weatherOpen.value) {
    weatherOpen.value = false
    return
  }
  openWeather()
  if (!isCase.value || weatherMode.value !== 'current' || activeCaseSite.value) return
  const view = scene.value?.basemapSwitchBounds()
  const area = GYIRONG_CASE.bounds
  const nearDefaultView = view && view.east - view.west >= 0.2
    && Math.abs((view.west + view.east) - (area.west + area.east)) < 0.05
    && Math.abs((view.south + view.north) - (area.south + area.north)) < 0.05
  if (nearDefaultView) fitWeather(area)
}

function changeWeatherMode(mode: WeatherMode): void {
  openWeather()
  if (mode === weatherMode.value) {
    if (mode === 'hourly' ? isCase.value : !isCase.value) {
      selectPreset(mode === 'hourly' ? 'tibet' : 'gyirong')
      if (mode === 'daily' && weatherLayer.value.rain) fitWeather(weatherLayer.value.rain.bounds)
    }
    return
  }
  weatherPicked.value = null
  weatherLayer.value = EMPTY_WEATHER_LAYER
  weatherMode.value = mode
  selectPreset(mode === 'hourly' ? 'tibet' : 'gyirong')
  if (mode === 'current') fitWeather(GYIRONG_CASE.bounds)
}

function fitWeather(bounds: BBox): void {
  if (!isCase.value) selectPreset('gyirong')
  const box = expandBBox(bounds, compact.value ? 0.29 : 0.15)
  if (compact.value && weatherOpen.value) {
    // Leave the data in the exposed map area above the weather sheet.
    const shift = (bounds.north - bounds.south) * (weatherMode.value === 'current' ? 0.64 : 0.41)
    box.south -= shift
    box.north -= shift
  }
  moveCamera(box, true, true)
}
const regionCode = ref<string>('')
const observedRegionCodes = ref<string[]>([])
/** 第几趟 `load()`：换筛选、点刷新、30 秒自动刷新都算一次，只有最新那趟的结果才落页面。 */
let loadSeq = 0
const notes = ref<string[]>([])
// 开局还没探过测：状态按保守的"降级"显示，但说明必须写"尚未探测"，
// 不能一上来就替现场断言"至少一路缺位"——那是一句没有证据的结论。
const machine = ref<OfflineMachine>(initialMachine('degraded', NOT_PROBED_YET))
const bbox = ref<BBox | null>(null)
const zoom = ref(0)
const visibility = ref<LayerVisibility>({ ...DEFAULT_LAYER_VISIBILITY })
const selectedId = ref<string | null>(null)
/** 「定位到有点的区域」按完之后的下落；空串表示没什么要说。 */
const focusNote = ref('')

/** 纯逻辑装配：一处产出全部图层数据，页面其余部分只读它。 */
const layers = computed<MapLayers>(() =>
  buildLayers({
    readings: readings.value,
    stations: stations.value,
    warnings: warnings.value,
    chains: chains.value,
    anchors: anchors.value,
    hazardZones: hazardDocument.value,
  }),
)

/**
 * 风险等级筛选（面板那排芯片）。空数组＝不过滤。
 * 只在这一个地方落地：`viewLayers` 是唯一被送去渲染与计数的图层集，`layers` 保持全量给图例读数用。
 */
const riskFilter = ref<RiskLevel[]>([])
const viewLayers = computed(() => filterLayersByRisk(layers.value, riskFilter.value))

/** 面板里的图层计数是**筛后**的，全量另给一份用于"2 / 6"这种写法。 */
const plan = computed(() => {
  const source = viewLayers.value
  return buildRenderPlan({
    layers: {
      ...source,
      stations: visibility.value.stations ? source.stations : [],
      warnings: visibility.value.warnings ? source.warnings : [],
    },
    bbox: bbox.value,
    zoom: zoom.value,
    aggregate: visibility.value.clusters,
  })
})
const counts = computed(() => layerCounts(viewLayers.value))
const countsTotal = computed(() => layerCounts(layers.value))
/** 芯片上的数字始终按未筛的全量数——筛选不该让图例自己变小。 */
const riskCounts = computed(() => riskLevelCounts(layers.value))
const unlocated = computed(() => toUnlocatedItems(layers.value.unplaced))
const detail = computed(() => detailOf(layers.value, selectedId.value))
const plottedTotal = computed(() =>
  plottedCount(filterLayersByViewport({ bbox: bbox.value, layers: viewLayers.value }), visibility.value),
)
const regionOptions = computed(() =>
  [...new Set([...anchors.value.map((anchor) => anchor.code), ...observedRegionCodes.value, regionCode.value])]
    .filter(Boolean)
    .sort(),
)

/** fetch 只在这一处取全局引用，便于探针与建场共用同一注入点。 */
function probeFetch(
  url: string,
  init?: { method?: string; signal?: AbortSignal },
): Promise<{ ok: boolean; status: number }> {
  if (typeof fetch === 'undefined') return Promise.resolve({ ok: false, status: 0 })
  return fetch(url, init)
}

async function probeNetwork(): Promise<void> {
  const facts = await collectFacts({
    fetch: probeFetch,
    browserOnline: globalThis.navigator?.onLine !== false,
    targets: {
      api: '/healthz',
      basemapTemplate: tileUrlForTemplate(MAP_ASSET_PATHS.basemapTemplate),
      terrain: terrainProbeUrl(MAP_ASSET_PATHS.terrainReal),
      localBasemap: MAP_ASSET_PATHS.reliefPmtiles,
    },
  })
  machine.value = reduceOffline(machine.value, { type: 'probe', facts, at: Date.now() })
}

function settled<T>(result: PromiseSettledResult<T>): T | null {
  return result.status === 'fulfilled' ? result.value : null
}

function rejectedReason(result: PromiseSettledResult<unknown>): unknown {
  return result.status === 'rejected' ? result.reason : null
}

/** 区域过滤走后端真实查询参数（`api/routers/` 的 telemetry/warnings 都收 region_code），不在前端假过滤。 */
async function load(): Promise<void> {
  loading.value = true
  const seq = ++loadSeq
  const filter = regionCode.value ? { region_code: regionCode.value } : {}
  const [telemetry, warningList, eventList, stationList, anchorFile, zoneFile] = await Promise.allSettled([
    mapApi.telemetry({ ...filter, limit: 2_000 }),
    mapApi.warnings({ ...filter, limit: 100 }),
    mapApi.events(100),
    mapApi.stations({ ...filter, limit: 500 }),
    mapApi.regionAnchors(),
    mapApi.hazardZones(),
  ])
  /**
   * 这一趟在路上时，人可能已经换了区域（或 30 秒自动刷新与手动刷新撞在一起）。
   * 真机量到：切到 540121 之后清单是 18 条，3 秒后那一趟**无过滤**的旧快照迟到落地，
   * 数字跳回全区域的 64 条并停在那里，而选框上仍写着 540121——筛的是 A、图上画的是全部。
   */
  if (seq !== loadSeq) return

  readings.value = settled(telemetry)?.items ?? []
  warnings.value = settled(warningList)?.items ?? []
  chains.value = settled(eventList)?.items ?? []
  stations.value = settled(stationList)?.items ?? []
  observedRegionCodes.value = [...new Set([
    ...observedRegionCodes.value,
    ...readings.value.map((item) => item.region_code),
    ...warnings.value.flatMap((item) => item.region_codes),
    ...stations.value.map((item) => item.region_code).filter((code): code is string => Boolean(code)),
  ])]
  const anchorData = settled(anchorFile)
  anchors.value = anchorData ? normalizeAnchors(anchorData) : []
  hazardDocument.value = settled(zoneFile)

  const collected: string[] = []
  if (isMissingResource(rejectedReason(stationList))) collected.push('暂无站点坐标。缺少坐标的站点可在未定位清单查看。')
  if (isMissingResource(rejectedReason(anchorFile))) {
    collected.push('区域定位资料缺失。只有已知坐标的业务记录可以落点。')
  }
  if (isMissingResource(rejectedReason(zoneFile))) {
    collected.push('灾害分区边界资料缺失，当前无法显示分区面。')
  }
  for (const result of [telemetry, warningList, eventList]) {
    const reason = rejectedReason(result)
    if (reason) collected.push(`数据读取失败：${reason instanceof Error ? reason.message : String(reason)}`)
  }
  notes.value = collected
  loading.value = false
  await probeNetwork()
}

function changeRegion(): void {
  readings.value = []
  warnings.value = []
  stations.value = []
  chains.value = []
  selectedId.value = null
  focusNote.value = ''
  void load()
}

/** 地图是本页最重的一块：cesium 只在真正建场时动态取；建场失败也不影响右侧文字清单。 */
async function initScene(): Promise<void> {
  const target = host.value
  if (!target || sceneLoading.value) return
  const seq = ++sceneSeq
  sceneLoading.value = true
  sceneError.value = null
  try {
    const { createMapScene } = await import('@/components/map/viewer')
    if (seq !== sceneSeq || target !== host.value) return
    const initialRectangle = cameraBounds.value
    const created = await createMapScene(target, {
      terrainUrl: MAP_ASSET_PATHS.terrainReal,
      worldReliefUrl: MAP_ASSET_PATHS.worldRelief,
      initialRectangle,
      historicalSites: GYIRONG_CASE.sites,
      historicalRelief: {
        ...gyirongRelief,
        closeups: [
          { ...gyirongRelief.closeup, status: 'closeup', maxHeightM: 13_000 },
          { ...gyirongRelief.street, status: 'street', maxHeightM: 4_200 },
        ],
      },
      historicalGeography: gyirongGeography,
      onHistoricalRelief: (status) => {
        if (seq === sceneSeq) reliefStatus.value = status
      },
      onScaleChanged: (nextScale) => {
        if (seq === sceneSeq) scale.value = nextScale
      },
      onGlobalViewChanged: (visible) => {
        if (seq === sceneSeq) globalTerrainVisible.value = visible
      },
      // 离线档优先用真实高程阴影，缺失时仍可使用本地 OSM 道路瓦片。
      basemap: {
        template: MAP_ASSET_PATHS.basemapTemplate,
        pmtilesUrl: MAP_ASSET_PATHS.reliefPmtiles,
        prefer: 'pmtiles',
        maximumLevel: 10,
      },
      fetchLike: probeFetch,
      onViewChanged: (next, nextZoom) => {
        if (seq === sceneSeq) updateMapView(next, nextZoom)
      },
      onSelect: (featureId) => {
        if (seq === sceneSeq) showFeature(featureId)
      },
      themeMode: themeMode.value,
    })
    if (seq !== sceneSeq || target !== host.value) {
      created.destroy()
      return
    }
    scene.value = created
    sceneInfo.value = created.info
    if (cameraBounds.value !== initialRectangle) created.fitBBox(cameraBounds.value)
    created.setHistoryVisible(isCase.value)
    for (const layer of Object.keys(visibility.value) as PlanLayer[]) created.setLayerVisible(layer, visibility.value[layer])
    created.render(plan.value)
    created.setWeather(visibleWeather.value)
  } catch (error) {
    if (seq !== sceneSeq) return
    sceneError.value = error instanceof Error ? error.message : String(error)
    message.error('三维地图加载失败，可继续查看图层面板')
  } finally {
    if (seq === sceneSeq) sceneLoading.value = false
  }
}

function updateMapView(next: BBox | null, nextZoom: number): void {
  weatherViewport.value = next
  bbox.value = next ? expandBBox(next) : null
  zoom.value = nextZoom
}

function onToggle(layer: PlanLayer, visible: boolean): void {
  visibility.value = { ...visibility.value, [layer]: visible }
  scene.value?.setLayerVisible(layer as LayerName, visible)
}

/** 芯片是"多选并集"：再点一次取消该级；顺序按等级排，避免点法不同导致 title 里数字跳动。 */
function onToggleRisk(level: RiskLevel): void {
  const next = riskFilter.value.includes(level)
    ? riskFilter.value.filter((item) => item !== level)
    : [...riskFilter.value, level]
  riskFilter.value = next.sort((a, b) => a - b)
}

function showFeature(id: string | null): void {
  if (id?.startsWith('weather-')) {
    weatherPicked.value = id
    openWeather()
    return
  }
  weatherPicked.value = null
  const cluster = id && plan.value.clusters.find((item) => item.id === id)
  if (cluster) {
    selectedId.value = null
    focusBusinessPoint(cluster.lonlat)
    return
  }
  const site = isCase.value && GYIRONG_CASE.sites.find((item) => item.id === id)
  if (site) {
    selectedId.value = null
    activeCaseSite.value = site
    caseExpanded.value = true
    panelsCollapsed.value = true
    mobilePane.value = null
    settingsOpen.value = false
    return
  }
  selectedId.value = id
  if (!id) return
  weatherOpen.value = false
  if (compact.value) mobilePane.value = 'layers'
  else panelsCollapsed.value = false
}

function selectFeature(id: string | null): void {
  showFeature(id)
  const feature = detailOf(layers.value, id)
  if (feature && 'lonlat' in feature && feature.lonlat) focusBusinessPoint(feature.lonlat)
}

function focusBusinessPoint(point: { lon: number; lat: number }): void {
  if (scene.value) scene.value.focusAt(point)
  else amapPanel.value?.focusAt(point)
}

function moveCamera(bounds: BBox, mainlandChina = !isCase.value, fit = false): void {
  fitCurrentViewport.value = fit
  cameraBounds.value = { ...bounds }
  cameraInMainland.value = mainlandChina
  scene.value?.fitBBox(bounds)
}

function selectPreset(next: MapPreset): void {
  preset.value = next
  selectedId.value = null
  weatherPicked.value = null
  activeCaseSite.value = null
  focusNote.value = ''
  panelsCollapsed.value = isCase.value
  mobilePane.value = null
  settingsOpen.value = false
  caseExpanded.value = null
  scene.value?.setHistoryVisible(isCase.value)
  moveCamera(isCase.value ? GYIRONG_CASE.bounds : TIBET_RECTANGLE)
}

function zoomMap(direction: 1 | -1): void {
  if (scene.value) scene.value.zoomBy(direction)
  else amapPanel.value?.zoomBy(direction)
}

function focusCaseSite(site: HistoricalSite): void {
  showFeature(site.id)
  const { lon, lat } = site.lonlat
  moveCamera({ west: lon - 0.028, east: lon + 0.028, south: lat - 0.018, north: lat + 0.018 }, site.mainlandChina)
  if (compact.value) caseExpanded.value = false
}

function focusPortCloseup(): void {
  const port = GYIRONG_CASE.sites[0]
  const { lon, lat } = port.lonlat
  if (basemapMode.value === 'amap') chooseBasemap('offline')
  showFeature(port.id)
  moveCamera({ west: lon - 0.008, east: lon + 0.008, south: lat - 0.006, north: lat + 0.006 }, true, true)
  if (compact.value) caseExpanded.value = false
}

function focusAll(): void {
  selectedId.value = null
  // 外接框由 entities.bboxOfPoints 算（纯函数、已单测），这里只决定相机去哪
  const points = layers.value.stations.map((feature) => feature.lonlat)
  const located = points.filter((point): point is NonNullable<typeof point> => point !== null).length
  // 总数要算上没有坐标的那批：它们只在「未定位清单」里，不在 stations 图层里。
  // 先前的文案对着 `points.length` 说"还没有在册站点"，而同一页的未定位清单写着
  // "站点无经纬度 × 6"——两处隔着几十像素互相打脸。
  const missingStations = unlocated.value.filter((item) => item.kind === 'station').length
  const box = bboxOfPoints(points)
  if (isCase.value) selectPreset('tibet')
  if (box) moveCamera(expandBBox(box))
  else moveCamera(TIBET_RECTANGLE)
  focusNote.value = focusAllNote(located, located + missingStations)
}

function onBrowserOffline(): void {
  machine.value = reduceOffline(machine.value, { type: 'browser_offline', at: Date.now() })
}

function onBrowserOnline(): void {
  machine.value = reduceOffline(machine.value, { type: 'browser_online', at: Date.now() })
  void probeNetwork()
}

let dataTimer: number | undefined
let probeTimer: number | undefined

onMounted(() => {
  void load()
  void initScene()
  dataTimer = window.setInterval(() => {
    if (autoRefresh.value && !loading.value && document.visibilityState === 'visible') void load()
  }, DATA_REFRESH_MS)
  probeTimer = window.setInterval(() => void probeNetwork(), PROBE_REFRESH_MS)
  window.addEventListener('offline', onBrowserOffline)
  window.addEventListener('online', onBrowserOnline)
})

onBeforeUnmount(() => {
  loadSeq++
  sceneSeq++
  if (dataTimer) window.clearInterval(dataTimer)
  if (probeTimer) window.clearInterval(probeTimer)
  window.removeEventListener('offline', onBrowserOffline)
  window.removeEventListener('online', onBrowserOnline)
  scene.value?.destroy()
  scene.value = null
})

/** 主题翻转 → 场景换调色板并重画图元（图元色是 add 那一刻烤进去的，不重画就是半翻画面）。 */
watch(themeMode, (mode) => {
  scene.value?.setTheme(mode)
})
watch(visibleWeather, (layer) => scene.value?.setWeather(layer))
watch(() => route.query.view, (view) => selectPreset(view === 'tibet' ? 'tibet' : 'gyirong'))

/** 只记忆用户的选择；自动回退后，下次进页仍可重试在线地形。 */
function chooseBasemap(mode: BasemapMode): void {
  basemapWarning.value = ''
  if (mode === 'amap' && basemapMode.value === 'offline') {
    const view = scene.value?.basemapSwitchBounds()
    if (view && view.east > view.west && view.east - view.west < 45 && view.north > view.south) {
      cameraBounds.value = view
      fitCurrentViewport.value = true
    }
    settingsOpen.value = false
  }
  basemapMode.value = mode
  writeBasemapMode(storage, mode)
}

function onAmapFailure(): void {
  basemapWarning.value = '高德卫星暂不可用，已切换到离线地形。'
  basemapMode.value = 'offline'
}

watch(
  basemapMode,
  (mode) => {
    /**
     * 切到高德：`v-if` 会把 Cesium 的宿主 div 整个卸载掉，**连带把 canvas 一起带走**；
     * 而场景对象还活着（`scene.value !== null`），于是切回离线时既不重建、也没有画布可画——
     * 真机实测：宿主 692×490、`.cesium-widget` 不存在、画面只剩 `.map-host` 的深蓝背景。
     * 所以切走时必须销毁场景，切回时重建。
     */
    if (mode === 'amap') {
      sceneSeq++
      bbox.value = null
      weatherViewport.value = null
      zoom.value = 0
      scale.value = null
      reliefStatus.value = null
      globalTerrainVisible.value = false
      sceneLoading.value = false
      scene.value?.destroy()
      scene.value = null
      sceneInfo.value = null
      sceneError.value = null
      return
    }
    /**
     * 页面直接落在 'amap' 档（localStorage 记忆）时，挂载那刻 Cesium 宿主不存在，
     * initScene 在 `!host` 上早退、场景为 null；切回离线时宿主才出现——不在这里补建场，
     * 离线档就是永远转圈的"正在加载三维场景"（交付形态反而死了）。
     * 必须用 post：watch 默认先于渲染跑，那一刻宿主 div 还没挂上，`!host` 又会早退一次。
     */
    if (scene.value === null && sceneError.value === null) void initScene()
  },
  { flush: 'post' },
)

// 数据/视野变化 → 重算渲染计划并推给场景：plan 是 computed（纯函数产物），页面不手改图元。
watch(plan, (data) => {
  scene.value?.render(data)
})

// 显隐变化直接施加到 DataSource 上（viewer 内部按图层名持有 CustomDataSource）。
watch(visibility, (value) => {
  for (const layer of Object.keys(value) as PlanLayer[]) scene.value?.setLayerVisible(layer as LayerName, value[layer])
})
</script>

<template>
  <div
    ref="shellRef"
    class="map-shell"
    :class="{ 'is-panels-collapsed': panelsCollapsed, 'is-compact': compact, 'is-case': showCaseCard, 'is-weather-open': weatherOpen, 'is-rain-replay': rainReplay }"
    :style="{ '--map-bar-end': toolbarHeight > 0 ? `${toolbarHeight + 24}px` : '78px' }"
  >
    <!-- 顶部悬浮工具条：标题 + 底图/区域/刷新/定位/自动刷新，半透明压在图上 -->
    <div ref="toolbarRef" class="map-floatbar">
      <PageHero
        class="map-floatbar__hero"
        title="应急一张图"
        :badge="basemapSummary"
        caption="查看吉隆案例地形、卫星影像和全域监测图层。"
      />
      <a-button
        v-if="compact"
        class="map-settings-toggle"
        :aria-expanded="settingsOpen"
        aria-controls="map-tools"
        data-testid="map-settings"
        @click="weatherOpen = false; settingsOpen = !settingsOpen"
      >
        <template #icon><SettingOutlined aria-hidden="true" /></template>
        地图设置
      </a-button>
      <button
        type="button" class="map-weather-trigger"
        :aria-expanded="weatherOpen" aria-controls="map-weather"
        :aria-label="`天气与雨情 · ${weatherButtonLabel}`"
        @click="toggleWeather"
      >
        <CloudOutlined aria-hidden="true" />
        <span>{{ weatherButtonLabel }}</span>
      </button>
      <div v-if="weatherOpen || weatherMode !== 'current'" class="map-weather-modes" role="group" aria-label="天气数据模式">
        <button v-for="item in WEATHER_MODES" :key="item.id" type="button" :aria-pressed="weatherMode === item.id" @click="changeWeatherMode(item.id)">{{ item.label }}</button>
        <button v-if="rainReplay" type="button" :aria-pressed="visibility.warnings" @click="onToggle('warnings', !visibility.warnings)">风险点</button>
      </div>
      <div class="map-preset" role="group" aria-label="地图视野">
        <button type="button" :aria-pressed="isCase" @click="selectPreset('gyirong')">吉隆案例</button>
        <button type="button" :aria-pressed="!isCase" @click="selectPreset('tibet')">西藏全域</button>
      </div>
      <div v-show="!compact || settingsOpen" id="map-tools" class="map-floatbar__tools">
        <!-- 底图档：默认离线自托管（交付形态，见 external.ts 的默认档论证）；高德是带 key 的 JS API 档，加载失败会在图上说明 -->
        <a-radio-group :value="basemapMode" size="small" button-style="solid" data-testid="basemap-mode" @update:value="chooseBasemap">
          <a-radio-button value="offline">本地地形</a-radio-button>
          <a-radio-button value="amap">高德卫星</a-radio-button>
        </a-radio-group>
        <a-select v-model:value="regionCode" class="map-region" show-search placeholder="全部区域" aria-label="区域" @change="changeRegion">
          <a-select-option value="">全部区域</a-select-option>
          <a-select-option v-for="code in regionOptions" :key="code" :value="code">{{ code }}</a-select-option>
        </a-select>
        <a-button :loading="loading" @click="load">
          <template #icon><ReloadOutlined aria-hidden="true" /></template>
          刷新
        </a-button>
        <a-button v-if="basemapMode === 'offline'" @click="focusAll">
          <template #icon><AimOutlined aria-hidden="true" /></template>
          定位站点
        </a-button>
        <div class="map-auto-refresh">
          <a-switch v-model:checked="autoRefresh" aria-label="自动刷新" />
          <span class="muted">30 秒自动刷新</span>
        </div>
      </div>
    </div>

    <!-- 提示：加载告警与"定位退不回全局"的原因，压在工具条下方 -->
    <div v-if="focusNote || (basemapWarning && showCaseCard && !compact)" class="map-floatnotes">
      <a-alert v-if="basemapWarning && showCaseCard && !compact" type="warning" show-icon :message="basemapWarning" />
      <!-- 定位这颗按钮先前点了没反应也不解释；现在把"退不回全局视野的原因"说一句 -->
      <p v-if="focusNote" class="muted" data-testid="focus-note">{{ focusNote }}</p>
    </div>

    <!-- 地图铺满整块，左右列与顶部条都是悬浮层 -->
    <div class="map-frame">
      <!-- 高德 JS API 是另一套渲染引擎，与 Cesium 不叠在同一块画布上：整块换表面 -->
      <AmapMap
        v-if="basemapMode === 'amap'"
        ref="amapPanel"
        :plan="plan"
        :visibility="visibility"
        :camera="amapCamera"
        :historical-sites="isCase ? GYIRONG_CASE.sites : undefined"
        :weather="visibleWeather"
        @view="updateMapView"
        @select="selectFeature"
        @failure="onAmapFailure"
      />
      <template v-else>
        <div ref="host" class="map-host" />
        <div v-if="!sceneInfo && !sceneError" class="map-placeholder" role="status">
          <div class="map-placeholder__card">
            <p class="map-placeholder__title">正在准备离线地图</p>
            <p class="map-placeholder__desc">加载期间仍可查看图层和数据状态。</p>
          </div>
        </div>
        <div v-if="sceneError" class="map-placeholder">
          <div class="map-placeholder__card">
            <p class="map-placeholder__title">三维地图加载失败</p>
            <p class="map-placeholder__desc">图层和数据状态仍可查看。</p>
            <a-button size="small" @click="initScene">重试地图</a-button>
            <details class="map-technical-error">
              <summary>查看原因</summary>
              <p class="hint">{{ sceneError }}</p>
            </details>
          </div>
        </div>
      </template>
    </div>

    <div v-if="isCase && (showCaseCard || weatherOpen || weatherMode === 'daily')" class="map-camera-controls" role="group" aria-label="地图视角">
      <button type="button" aria-label="放大地图" title="放大地图" @click="zoomMap(1)"><PlusOutlined aria-hidden="true" /></button>
      <button type="button" aria-label="缩小地图" title="缩小地图" @click="zoomMap(-1)"><MinusOutlined aria-hidden="true" /></button>
      <button type="button" aria-label="重置吉隆视野" title="重置吉隆视野" @click="selectPreset('gyirong')"><AimOutlined aria-hidden="true" /></button>
    </div>

    <div v-show="rainReplay && !compact" id="map-rain-replay" class="map-rain-dock" />
    <MapWeatherPanel
      class="map-weather-position" :open="weatherOpen" :mode="weatherMode" :picked="weatherPicked" :subdued="subduedRain" :compact="compact"
      @close="weatherOpen = false" @layer="weatherLayer = $event"
      @summary="weatherSummary = $event" @fit="fitWeather"
    />

    <aside v-if="showCaseCard" class="map-case" aria-labelledby="map-case-title">
      <button
        type="button"
        class="map-case__heading"
        :aria-expanded="showCaseDetails"
        aria-controls="map-case-details"
        @click="caseExpanded = !showCaseDetails"
      >
        <span class="map-case__heading-text">
          <span class="map-case__meta"><span class="map-case__dot" />历史案例 <time :datetime="GYIRONG_CASE.date">{{ GYIRONG_CASE.date }}</time></span>
          <strong id="map-case-title">吉隆口岸冰岩崩—泥石流</strong>
        </span>
        <DownOutlined aria-hidden="true" class="map-case__chevron" />
      </button>
      <div v-show="showCaseDetails" id="map-case-details" class="map-case__details">
        <p class="map-case__route">
          尼泊尔蓝塘里壤峰北坡冰岩崩，碎屑沿沟道下泄，经普热普强藏布、东林藏布，冲击吉隆口岸。
        </p>
        <dl class="map-case__metrics">
          <div><dt>沿沟道传播</dt><dd>约 22 <small>km</small></dd></div>
          <div><dt>启动区海拔</dt><dd>约 5,200 <small>m</small></dd></div>
        </dl>
        <div class="map-case__actions">
          <button
            v-for="site in GYIRONG_CASE.sites"
            :key="site.id"
            type="button"
            :aria-pressed="activeCaseSite?.id === site.id"
            @click="focusCaseSite(site)"
          >
            <AimOutlined aria-hidden="true" />{{ site.action }}
          </button>
        </div>
        <p class="map-case__note" aria-live="polite">
          {{ activeCaseSite?.note ?? (closeupSource || detailedTerrainVisible ? '蓝线为常态水系，金线为 G216。灾害路径可查看遥感解译。' : '灾害路径可查看遥感解译。') }}
        </p>
        <div class="map-case__sources" aria-label="案例资料">
          <a v-for="source in GYIRONG_CASE.sources" :key="source.url" :href="source.url" target="_blank" rel="noopener noreferrer">
            {{ source.label }} ↗
          </a>
          <a v-if="detailedTerrainVisible || closeupSource" :href="gyirongGeography.source" target="_blank" rel="noopener noreferrer">路网和水系来源 ↗</a>
        </div>
      </div>
    </aside>

    <div v-if="isCase && (!compact || (!settingsOpen && mobilePane === null && !weatherOpen)) && (scale || outsideDetailedTerrain || basemapMode === 'amap')" class="map-reference">
      <div v-if="scale" class="map-reference__scale" :aria-label="`地图比例尺 ${scaleText}`">
        <span>{{ scaleText }}</span>
        <i :style="{ width: `${scale.width}px` }" />
      </div>
      <div v-if="detailedTerrainVisible" class="map-reference__contours">
        <span class="map-reference__ramp" :style="{ backgroundImage: terrainRamp }" role="img" aria-label="海拔低到高的颜色" />
        <span>{{ gyirongRelief.contourIntervalM }} m 等高距</span>
      </div>
      <a v-if="closeupSource" class="map-reference__return" :href="closeupSource.catalog" target="_blank" rel="noopener noreferrer" :title="`成像 ${closeupSource.capturedAt}`">
        {{ closeupSource.provider }} · {{ closeupSource.resolutionM }} m ↗
      </a>
      <button v-else-if="basemapMode === 'amap' && zoom >= 13" type="button" class="map-reference__return" @click="focusPortCloseup">
        <AimOutlined aria-hidden="true" /> 口岸近景
      </button>
      <button
        v-if="(detailedTerrainVisible || closeupSource) && (activeCaseSite || (scale && scale.meters <= 200))"
        type="button"
        class="map-reference__return"
        @click="chooseBasemap('amap')"
      >
        看高德卫星
      </button>
      <button v-if="outsideDetailedTerrain" type="button" class="map-reference__return" @click="selectPreset('gyirong')">
        <AimOutlined aria-hidden="true" />
        返回吉隆地形
      </button>
    </div>

    <!-- 左列：离线状态 + 未定位清单 + 未渲染的几何 -->
    <div
      id="map-status"
      v-show="!compact || mobilePane === 'status'"
      class="map-float-col map-float-col--left"
      :inert="!compact && panelsCollapsed"
    >
      <a-alert v-if="basemapWarning" type="warning" show-icon :message="basemapWarning" />
      <details v-if="notes.length" class="map-data-issues">
        <summary>地图数据缺少 {{ notes.length }} 项</summary>
        <ul class="map-diagnostic"><li v-for="note in notes" :key="note">{{ note }}</li></ul>
      </details>
      <MapStatusPanel
        :status="machine.status"
        :status-reason="machine.reason"
        :basemap-message="basemapMode === 'amap' ? '高德官方卫星底图' : sceneInfo?.basemapMessage ?? '尚未建场（状态见上方标签）'"
        :terrain-message="basemapMode === 'amap' ? '高德在线 3D 地形' : sceneInfo?.terrainMessage ?? '尚未建场'"
        :unlocated="unlocated"
        :rejected="layers.rejected"
        :zoom="zoom"
        :loading="loading"
        :scene-error="sceneError"
        @refresh="load"
        @select="selectFeature"
      />
    </div>

    <!-- 右列：图层 + 风险筛选 + 要素详情；收起/展开按钮挂在「要素详情」卡右下角 -->
    <div
      id="map-layers"
      v-show="!compact || mobilePane === 'layers'"
      class="map-float-col map-float-col--right"
      :inert="!compact && panelsCollapsed"
    >
      <MapLayersPanel
        :counts="counts"
        :visibility="visibility"
        :clustered="plan.clustered"
        :risk-filter="riskFilter"
        :risk-counts="riskCounts"
        :counts-total="countsTotal"
        :detail="detail"
        @toggle="onToggle"
        @toggle-risk="onToggleRisk"
      >
        <template #detail-action>
          <a-button
            v-if="!compact"
            class="map-panel-toggle"
            size="small"
            :aria-pressed="panelsCollapsed"
            title="收起两侧面板，只看地图"
            data-testid="toggle-panels"
            @click="panelsCollapsed = true"
          >
            收起 »
          </a-button>
        </template>
      </MapLayersPanel>
    </div>

    <!-- 收起后右列滑出视口，留一枚悬浮按钮把它拉回来 -->
    <a-button
      v-if="panelsCollapsed && !compact && !weatherOpen"
      class="map-expand-btn"
      size="small"
      title="展开两侧面板"
      data-testid="toggle-panels-expand"
      @click="panelsCollapsed = false"
    >
      « 展开面板
    </a-button>

    <div v-if="compact && !weatherOpen" class="map-pane-switch" role="group" aria-label="地图面板">
      <a-button :aria-pressed="mobilePane === 'layers'" aria-controls="map-layers" @click="toggleMobilePane('layers')">
        <template #icon><AppstoreOutlined aria-hidden="true" /></template>
        图层与详情
      </a-button>
      <a-button :aria-pressed="mobilePane === 'status'" aria-controls="map-status" @click="toggleMobilePane('status')">
        <template #icon><InfoCircleOutlined aria-hidden="true" /></template>
        数据状态
      </a-button>
    </div>

    <div class="scene-bar" :class="{ 'is-compact': compact }" v-show="!compact || (mobilePane === null && !weatherOpen)">
      <!-- 视野与缩放是 Cesium 相机的量，高德档下没有这两个数，别摆一个假"未就绪" -->
      <span v-if="!compact && !isCase && basemapMode === 'offline'">
        视野：{{
          bbox
            ? `${bbox.west.toFixed(2)}, ${bbox.south.toFixed(2)} → ${bbox.east.toFixed(2)}, ${bbox.north.toFixed(2)}`
            : '未就绪'
        }}
      </span>
      <span>业务上图 {{ plottedTotal }} · 未定位 {{ unlocated.length }}</span>
      <span v-if="!compact && !isCase && basemapMode === 'offline'">· 缩放 {{ zoom }}</span>
      <span v-if="!compact && plan.clustered" class="hint">· 已按网格聚合（圆点上的数字=该格要素数）</span>
      <!-- 底图来源与坐标系要说在画面上：只写在代码注释里，看图的人无从知道落点被纠过偏 -->
      <span v-if="!compact" class="hint">· 底图：{{ basemapSummary }}</span>
    </div>
  </div>
</template>

<style scoped>
/* —— 全屏地图壳：铺满顶栏以下，所有信息都是浮层 —— */
.map-shell {
  position: relative;
  height: 100%;
  min-height: 400px;
  overflow: hidden;
}

/* 顶部悬浮条（对应参考图地图内的「运维区域」横条） */
.map-floatbar {
  position: absolute;
  top: var(--aeg-space-s3);
  left: var(--aeg-space-s3);
  right: var(--aeg-space-s3);
  z-index: 6;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--aeg-space-s3);
  padding: var(--aeg-space-s2) var(--aeg-space-s4);
  border: 1px solid var(--aeg-color-map-panel-border);
  border-radius: var(--aeg-radius-xl);
  background: var(--aeg-color-map-floatbar-bg);
  box-shadow: var(--aeg-shadow-md);
}
.map-floatbar__hero {
  flex: 1 1 auto;
  min-width: 0;
}
/* 页头收进悬浮条：只留标题与徽标，说明句已在 title 提示里 */
.map-floatbar :deep(.page-head) {
  margin: 0;
}
.map-floatbar :deep(.page-head__row) {
  gap: var(--aeg-space-s2);
}
.map-floatbar :deep(.page-head__title) {
  font-size: var(--aeg-font-md);
}
.map-floatbar :deep(.page-head__caption) {
  display: none;
}
.map-floatbar__tools {
  display: flex;
  flex: 0 1 auto;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--aeg-space-s2);
  min-width: 0;
  max-width: 100%;
}
.map-weather-trigger {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  min-height: 34px;
  padding-inline: 10px;
  border: 1px solid color-mix(in srgb, var(--aeg-color-overview-accent) 35%, var(--aeg-color-map-panel-border));
  border-radius: 12px;
  background: color-mix(in srgb, var(--aeg-color-overview-accent) 9%, var(--aeg-color-map-floatbar-bg));
  color: var(--aeg-color-overview-accent);
  font: inherit;
  font-size: 12px;
  font-weight: 650;
  white-space: nowrap;
  cursor: pointer;
}
.map-weather-trigger:is(:hover, [aria-expanded='true']) {
  border-color: var(--aeg-color-overview-accent);
  background: color-mix(in srgb, var(--aeg-color-overview-accent) 17%, var(--aeg-color-map-floatbar-bg));
}
.map-weather-modes {
  display: flex;
  min-width: 0;
  gap: 3px;
  padding: 3px;
  border: 1px solid var(--aeg-color-map-panel-border);
  border-radius: var(--aeg-radius-lg);
  background: var(--aeg-color-map-floatbar-bg);
}
.map-weather-modes button {
  min-height: 28px;
  padding: 0 var(--aeg-space-s2);
  border: 1px solid transparent;
  border-radius: var(--aeg-radius-md);
  background: transparent;
  color: var(--aeg-color-map-panel-text);
  font: inherit;
  font-size: var(--aeg-font-sm);
  white-space: nowrap;
  cursor: pointer;
}
.map-weather-modes button[aria-pressed='true'] {
  border-color: var(--aeg-color-map-panel-border);
  background: color-mix(in srgb, var(--aeg-color-status-info) 22%, var(--aeg-color-map-panel-bg));
}
.map-weather-modes button:focus-visible { outline: 2px solid var(--aeg-color-overview-accent); }
.is-compact .map-weather-modes { order: 6; max-width: 100%; overflow-x: auto; }
.map-rain-dock {
  position: absolute;
  z-index: 6;
  inset-inline: var(--aeg-space-s3) calc(var(--aeg-space-s3) + 68px);
  bottom: 64px;
  pointer-events: none;
}
.map-rain-dock > :deep(*) { pointer-events: auto; }
.is-rain-replay:not(.is-compact) :is(.map-reference, .map-camera-controls) { bottom: 208px; }
.map-weather-position {
  position: absolute;
  z-index: 7;
  top: var(--map-bar-end);
  right: var(--aeg-space-s3);
  width: min(326px, calc(100% - 24px));
  max-height: calc(100% - var(--map-bar-end) - 145px);
}
.is-rain-replay:not(.is-compact) .map-weather-position {
  max-height: calc(100% - var(--map-bar-end) - 208px);
}
.is-compact .map-weather-position {
  top: auto;
  bottom: 29px;
  inset-inline: var(--aeg-space-s3);
  width: auto;
  max-height: min(54%, calc(100% - var(--map-bar-end) - 75px));
}
.is-compact.is-weather-open .map-camera-controls {
  top: var(--map-bar-end);
  bottom: auto;
}
:global(.app-shell:has(.map-shell.is-compact.is-weather-open) .assistant-dock:not(.is-open)) {
  visibility: hidden;
}
.is-compact .map-weather-trigger { min-height: 38px; }
.map-preset {
  display: inline-flex;
  gap: 3px;
  padding: 3px;
  border: 1px solid var(--aeg-color-map-panel-border);
  border-radius: var(--aeg-radius-lg);
  background: color-mix(in srgb, var(--aeg-color-sunken), transparent 22%);
}
.map-preset button {
  min-height: 30px;
  padding: 0 var(--aeg-space-s3);
  border: 1px solid transparent;
  border-radius: var(--aeg-radius-md);
  background: transparent;
  color: var(--aeg-color-map-floatbar-text-muted);
  font: inherit;
  font-size: var(--aeg-font-sm);
  font-weight: var(--aeg-weight-semibold);
  white-space: nowrap;
  cursor: pointer;
}
.map-preset button[aria-pressed='true'] {
  border-color: color-mix(in srgb, var(--aeg-color-overview-accent), transparent 68%);
  background: color-mix(in srgb, var(--aeg-color-overview-accent), var(--aeg-color-map-floatbar-bg) 86%);
  color: var(--aeg-color-overview-accent);
}
.map-preset button:hover {
  color: var(--aeg-color-map-floatbar-text);
}
.map-region {
  width: 150px;
  max-width: 100%;
}
.map-auto-refresh {
  display: flex;
  align-items: center;
  gap: var(--aeg-space-s1);
  white-space: nowrap;
}
@supports (backdrop-filter: blur(12px)) {
  @media (prefers-reduced-transparency: no-preference) {
    .map-floatbar {
      background: color-mix(in srgb, var(--aeg-color-map-floatbar-bg) 92%, transparent);
      backdrop-filter: blur(12px) saturate(120%);
    }
  }
}
/* 自动刷新开关抬到 24px 点击面（small 档 16px 高在值班台上点不准）。 */
.map-floatbar__tools :deep(.ant-switch) {
  min-height: 24px;
}

/* 告警与定位提示：条下方浮一小块，不挡地图拖拽 */
.map-floatnotes {
  position: absolute;
  top: var(--map-bar-end);
  left: max(var(--aeg-space-s3), 25%);
  right: max(var(--aeg-space-s3), 26%);
  z-index: 6;
  display: flex;
  flex-direction: column;
  gap: var(--aeg-space-s1);
  pointer-events: none;
}
.map-floatnotes .muted {
  align-self: flex-start;
  margin: 0;
  padding: 4px 12px;
  border: 1px solid var(--aeg-color-map-panel-border);
  border-radius: var(--aeg-radius-pill);
  background: var(--aeg-color-map-floatbar-bg);
}
.is-case:not(.is-compact) .map-floatnotes {
  left: max(calc(var(--aeg-space-s3) + 320px), 25%);
}

.map-camera-controls,
.map-reference {
  position: absolute;
  z-index: 5;
  border: 1px solid var(--aeg-color-map-panel-border);
  border-radius: var(--aeg-radius-lg);
  background: var(--aeg-color-map-floatbar-bg);
  color: var(--aeg-color-map-floatbar-text);
  box-shadow: var(--aeg-shadow-md);
}
.map-camera-controls {
  right: calc(var(--aeg-space-s3) + 64px);
  bottom: 108px;
  display: grid;
  padding: 3px;
}
.map-camera-controls button {
  display: grid;
  width: 38px;
  height: 38px;
  place-items: center;
  border: 0;
  border-radius: var(--aeg-radius-md);
  background: transparent;
  color: inherit;
  font-size: 16px;
  cursor: pointer;
}
.map-camera-controls button:is(:hover, :focus-visible) {
  color: var(--aeg-color-overview-accent);
  background: var(--aeg-color-sunken);
}
.map-reference {
  left: var(--aeg-space-s3);
  bottom: 66px;
  display: flex;
  align-items: stretch;
  gap: 10px;
  padding: 7px 10px;
  font-size: var(--aeg-font-xs);
  font-variant-numeric: tabular-nums;
}
.map-reference__scale,
.map-reference__contours {
  display: grid;
  align-content: center;
  justify-items: start;
  gap: 3px;
  line-height: 1.2;
}
.map-reference__scale i {
  display: block;
  height: 5px;
  border: solid currentColor;
  border-width: 0 1px 1px;
}
.map-reference__contours {
  padding-left: 10px;
  border-left: 1px solid var(--aeg-color-map-panel-border);
  color: var(--aeg-color-map-floatbar-text-muted);
}
.map-reference__ramp {
  width: 100%;
  height: 4px;
  border-radius: var(--aeg-radius-pill);
}
.map-reference__return {
  display: inline-flex;
  min-height: 26px;
  align-items: center;
  gap: 6px;
  padding: 1px 6px;
  border: 0;
  border-radius: var(--aeg-radius-md);
  background: transparent;
  color: var(--aeg-color-overview-accent);
  font: inherit;
  cursor: pointer;
}
.map-reference__return:hover {
  background: var(--aeg-color-sunken);
}

.map-case {
  position: absolute;
  top: var(--map-bar-end);
  left: var(--aeg-space-s3);
  z-index: 5;
  width: 306px;
  max-height: calc(100% - var(--map-bar-end) - 58px);
  overflow: auto;
  overscroll-behavior: contain;
  border: 1px solid var(--aeg-color-map-panel-border);
  border-radius: var(--aeg-radius-xl);
  background: var(--aeg-color-map-floatbar-bg);
  box-shadow: var(--aeg-shadow-lg);
  color: var(--aeg-color-map-floatbar-text);
}
.map-case__heading {
  display: flex;
  width: 100%;
  align-items: center;
  justify-content: space-between;
  gap: var(--aeg-space-s2);
  padding: var(--aeg-space-s3) var(--aeg-space-s4);
  border: none;
  border-radius: var(--aeg-radius-xl);
  background: transparent;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}
.map-case__heading-text {
  display: grid;
  gap: var(--aeg-space-s1);
}
.map-case__heading strong {
  font-size: var(--aeg-font-md);
  line-height: 1.3;
}
.map-case__meta {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--aeg-color-overview-accent);
  font-size: var(--aeg-font-xs);
  font-weight: var(--aeg-weight-semibold);
}
.map-case__meta time {
  margin-inline-start: auto;
  color: var(--aeg-color-map-floatbar-text-muted);
  font-variant-numeric: tabular-nums;
}
.map-case__dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: currentColor;
}
.map-case__chevron {
  color: var(--aeg-color-map-floatbar-text-muted);
}
.map-case__heading[aria-expanded='true'] .map-case__chevron {
  transform: rotate(180deg);
}
.map-case__details {
  display: grid;
  gap: var(--aeg-space-s2);
  padding: 0 var(--aeg-space-s4) var(--aeg-space-s3);
  font-size: var(--aeg-font-sm);
  color: var(--aeg-color-map-floatbar-text-muted);
}
.map-case__details p,
.map-case__metrics,
.map-case__metrics dd {
  margin: 0;
}
.map-case__route {
  line-height: 1.55;
}
.map-case__metrics,
.map-case__actions {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--aeg-space-s2);
}
.map-case__metrics > div {
  padding: 6px 9px;
  border: 1px solid var(--aeg-color-map-panel-border);
  border-radius: var(--aeg-radius-md);
  background: color-mix(in srgb, var(--aeg-color-sunken), transparent 24%);
}
.map-case__metrics dt,
.map-case__metrics small,
.map-case__note {
  font-size: var(--aeg-font-xs);
}
.map-case__metrics dd {
  color: var(--aeg-color-map-floatbar-text);
  font-size: var(--aeg-font-md);
  font-weight: var(--aeg-weight-semibold);
  font-variant-numeric: tabular-nums;
}
.map-case__actions button {
  display: inline-flex;
  min-height: 36px;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 0 6px;
  border: 1px solid var(--aeg-color-map-panel-border);
  border-radius: var(--aeg-radius-md);
  background: transparent;
  color: var(--aeg-color-overview-accent);
  font: inherit;
  cursor: pointer;
}
.map-case__actions button:is(:hover, [aria-pressed='true']) {
  border-color: var(--aeg-color-overview-accent);
  background: color-mix(in srgb, var(--aeg-color-overview-accent), transparent 90%);
}
.map-case__sources {
  display: flex;
  flex-wrap: wrap;
  gap: 4px var(--aeg-space-s2);
  padding-top: 4px;
  border-top: 1px solid var(--aeg-color-map-panel-border);
}
.map-case__sources a {
  display: inline-flex;
  align-items: center;
  min-height: 24px;
  color: var(--aeg-color-overview-accent);
  font-size: var(--aeg-font-xs);
}
.map-case__sources a:hover {
  text-decoration: underline;
}

/* 地图铺满整块 */
.map-frame {
  position: absolute;
  inset: 0;
}
.map-host {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  background: var(--aeg-color-sunken);
}
/* Cesium 版权行默认 10px、展开链接 14px 高：抬到 12px 字与 24px 点击面（真机探针量到 10px / 81×14）。
   颜色留给 Cesium 自带的深底白字，不动它以免改出对比度回归。 */
.map-host :deep(.cesium-credit-wrapper),
.map-host :deep(.cesium-credit-expand-link) {
  font-size: 12px;
}
.map-host :deep(.cesium-credit-expand-link) {
  display: inline-block;
  padding: 5px 0;
}
.map-placeholder {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0 24px;
  pointer-events: none;
}
/* 黑洞里放一张体面的卡：未就绪/不可用都说清楚下一步，不摆一句永远转圈的"正在加载" */
.map-placeholder__card {
  pointer-events: auto;
  max-width: 520px;
  padding: var(--aeg-space-s5) var(--aeg-space-s6);
  border: 1px solid var(--aeg-color-map-panel-border);
  border-radius: var(--aeg-radius-lg);
  background: var(--aeg-color-map-panel-bg);
  box-shadow: var(--aeg-shadow-md);
  color: var(--aeg-color-map-panel-text);
  text-align: left;
}
.map-placeholder__title {
  margin: 0 0 var(--aeg-space-s2);
  font-size: var(--aeg-font-md);
  font-weight: var(--aeg-weight-bold);
}
.map-placeholder__desc {
  margin: 0 0 var(--aeg-space-s2);
  font-size: var(--aeg-font-sm);
  line-height: var(--aeg-lh-normal);
  color: var(--aeg-color-map-panel-text-muted);
}
.map-placeholder .hint {
  margin: 0;
  color: var(--aeg-color-map-panel-text-muted);
  font-size: 12px;
  line-height: 1.7;
}
.map-technical-error {
  margin-top: var(--aeg-space-s2);
  font-size: var(--aeg-font-sm);
}
.map-technical-error summary {
  cursor: pointer;
}

/* 面板透出少量底图，文字对比度由地图专用令牌保证。 */
.map-float-col {
  --aeg-color-text: var(--aeg-color-map-panel-text);
  --aeg-color-text-secondary: var(--aeg-color-map-panel-text-muted);
  --aeg-color-text-muted: var(--aeg-color-map-panel-text-muted);
  position: absolute;
  top: var(--map-bar-end);
  bottom: var(--aeg-space-s3);
  z-index: 5;
  width: 300px;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding-right: 2px;
  pointer-events: none;
  scrollbar-width: thin;
  color: var(--aeg-color-map-panel-text);
  transition:
    transform var(--aeg-motion-base) ease,
    opacity var(--aeg-motion-base) ease;
}
.map-float-col--left {
  left: var(--aeg-space-s3);
}
.map-float-col--right {
  right: var(--aeg-space-s3);
  width: 340px;
}
/* 收起态：两列滑出视口，底部状态条顺势铺满；收起的卡片也不再吃鼠标事件 */
.map-shell.is-panels-collapsed:not(.is-compact) .map-float-col--left {
  opacity: 0;
  transform: translateX(calc(-100% - var(--aeg-space-s3)));
}
.map-shell.is-panels-collapsed:not(.is-compact) .map-float-col--right {
  opacity: 0;
  transform: translateX(calc(100% + var(--aeg-space-s3)));
}
.map-shell.is-panels-collapsed:not(.is-compact) .map-float-col :deep(.ant-card) {
  pointer-events: none;
}
.map-shell.is-panels-collapsed .scene-bar {
  left: var(--aeg-space-s3);
  right: var(--aeg-space-s3);
}
.map-shell.is-case .scene-bar {
  right: auto;
  width: max-content;
  max-width: calc(100% - 24px);
}
/* 收起按钮：定位在「要素详情」卡的右下角（锚点由 MapLayersPanel 的 .detail-card 提供） */
.map-panel-toggle {
  position: absolute;
  right: var(--aeg-space-s3);
  bottom: var(--aeg-space-s3);
  z-index: 2;
}
.map-expand-btn {
  position: absolute;
  top: var(--map-bar-end);
  right: calc(var(--aeg-space-s3) + 64px);
  z-index: 7;
  border-color: var(--aeg-color-map-panel-border);
  background: var(--aeg-color-map-floatbar-bg);
}
/* 面板本体：半透明玻璃，卡内白底一律去掉 */
.map-float-col :deep(.ant-card) {
  pointer-events: auto;
  background: var(--aeg-color-map-panel-bg);
  border: 1px solid var(--aeg-color-map-panel-border);
  border-radius: var(--aeg-radius-lg);
  box-shadow: var(--aeg-shadow-md);
  color: var(--aeg-color-map-panel-text);
}
/* antd 组件自己带文字色，不跟随上面的变量，逐个扳到面板自己的那档 */
.map-float-col :deep(.ant-card-head-title),
.map-float-col :deep(.ant-descriptions-title) {
  color: var(--aeg-color-map-panel-text);
}
.map-float-col :deep(.ant-descriptions-item-label),
.map-float-col :deep(.ant-descriptions-item-content),
.map-float-col :deep(.ant-empty-description) {
  color: var(--aeg-color-map-panel-text-muted);
}
.map-float-col :deep(.ant-card-head) {
  border-bottom: none;
  background: transparent;
}
.map-float-col :deep(.ant-card-body) {
  background: transparent;
}

/* 底部状态条：夹在两列之间的悬浮胶囊 */
.scene-bar {
  position: absolute;
  left: 324px;
  right: 352px;
  bottom: 30px;
  z-index: 6;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 4px 12px;
  border: 1px solid var(--aeg-color-map-panel-border);
  border-radius: var(--aeg-radius-pill);
  background: var(--aeg-color-map-floatbar-bg);
  font-size: 12px;
  color: var(--aeg-color-map-floatbar-text-muted);
}
.scene-bar .hint {
  /* 这条 hint 坐在**浅色**悬浮胶囊上（合成底实测 rgb(222,237,254)）：
     `--aeg-color-status-warning` 的浅主题档 #B45309 实算只有 4.23:1，12px 小字差一点点。
     改用同为主题里的"琥珀文字"那一档 #8A4005（同一支也被台账/工单用在亮底上），实算 6.27:1；
     深主题里它翻成 #FBBF24，压在深胶囊上 6.78:1，两档都过。 */
  color: var(--aeg-tag-orange-text);
}
.muted {
  font-size: 12px;
  color: var(--aeg-color-map-floatbar-text-muted);
}
.map-diagnostic {
  margin: 4px 0 10px;
  padding-inline-start: var(--aeg-space-s3);
}
.map-data-issues {
  padding: 0 var(--aeg-space-s3);
  border: 1px solid var(--aeg-color-map-panel-border);
  border-radius: var(--aeg-radius-lg);
  background: var(--aeg-color-map-panel-bg);
  color: var(--aeg-color-map-panel-text-muted);
  font-size: var(--aeg-font-sm);
  pointer-events: auto;
}
.map-data-issues summary {
  padding: 9px 0;
  color: var(--aeg-color-map-panel-text);
  cursor: pointer;
}
.is-compact .map-floatbar {
  gap: var(--aeg-space-s2);
  padding-inline: var(--aeg-space-s3);
}
.is-compact .map-floatbar__hero {
  flex: 1 1 0;
}
.is-compact .map-floatbar__tools {
  flex-basis: 100%;
}
.is-compact .map-preset {
  flex-basis: 100%;
}
.is-compact .map-preset button {
  flex: 1 1 0;
  min-height: 36px;
}
.is-compact .map-case {
  inset-inline: var(--aeg-space-s3);
  width: auto;
  max-height: calc(100% - var(--map-bar-end) - 130px);
}
.is-compact .map-camera-controls {
  right: var(--aeg-space-s3);
  bottom: 162px;
}
.is-compact .map-camera-controls button {
  width: 40px;
  height: 40px;
}
.is-compact .map-reference {
  bottom: 143px;
  max-width: calc(100% - 2 * var(--aeg-space-s3));
  flex-wrap: wrap;
}
.is-compact .map-floatbar__tools :deep(.ant-radio-button-wrapper),
.is-compact .map-floatbar__tools :deep(.ant-btn),
.is-compact .map-settings-toggle,
.map-pane-switch :deep(.ant-btn) {
  min-height: 38px;
}
.is-compact .map-floatbar__tools :deep(.ant-radio-button-wrapper) {
  line-height: 36px;
}
.is-compact .map-floatnotes {
  inset-inline: var(--aeg-space-s3);
}
.is-compact .map-float-col {
  inset-inline: var(--aeg-space-s3);
  top: auto;
  bottom: 102px;
  width: auto;
  max-height: min(55%, calc(100% - var(--map-bar-end) - 110px));
  padding: var(--aeg-space-s2);
  border: 1px solid var(--aeg-color-map-panel-border);
  border-radius: var(--aeg-radius-xl);
  background: var(--aeg-color-map-panel-bg);
  box-shadow: var(--aeg-shadow-lg);
  pointer-events: auto;
}
.map-pane-switch {
  position: absolute;
  bottom: 40px;
  inset-inline: var(--aeg-space-s3);
  z-index: 7;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--aeg-space-s1);
  padding: var(--aeg-space-s1);
  border: 1px solid var(--aeg-color-map-panel-border);
  border-radius: var(--aeg-radius-lg);
  background: var(--aeg-color-map-floatbar-bg);
  box-shadow: var(--aeg-shadow-md);
}
.map-pane-switch :deep(.ant-btn[aria-pressed='true']) {
  background: var(--aeg-color-masthead-tab);
  border-color: var(--aeg-color-masthead-tab);
  color: var(--aeg-color-masthead-tab-ink);
}
.scene-bar.is-compact {
  inset-inline: var(--aeg-space-s3);
  bottom: 103px;
  width: max-content;
  max-width: calc(100% - 24px);
}

@media (max-width: 370px) {
  .is-compact .map-settings-toggle {
    padding-inline: 8px;
  }
  .is-compact .map-weather-trigger { padding-inline: 8px; }
}

/* 1024 档：两列收窄，把中间的地图让出来 */
@media (max-width: 1200px) {
  .map-float-col {
    width: 250px;
  }
  .map-float-col--right {
    width: 280px;
  }
  .scene-bar {
    left: 274px;
    right: 292px;
  }
}
</style>
