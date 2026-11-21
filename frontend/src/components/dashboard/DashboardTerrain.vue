<script setup lang="ts">
/**
 * 态势总览的三维地形嵌入（2026-11-20 指挥中心裁决：地图是主视觉中心）。
 *
 * 刻意做成「一张图」的薄嵌入，而不是另起一套渲染：
 * - 取数走 `@/api/map`（与 MapView 同一份契约），判断走 `components/map/entities.ts`（纯逻辑、已单测），
 *   渲染走 `components/map/viewer.ts`（唯一接触 WebGL 的文件，cesium 由 `await import()` 取，不进首屏包）。
 * - 与 MapView 的唯一差别是这里多传一个 `onReady` 钩子：建场后把 viewer 交给本组件，
 *   在其上叠「风险点呼吸」与「数据连线」，并把相机摆成看得见高原起伏的斜视角。
 *   钩子是 additive 的——「一张图」不传它，画面口径与门禁完全不变。
 *
 * 诚实性口径（与全站一致，不臆造）：
 * - 底图/地形是同源烘焙资产；区划锚点与灾害面**刻意不预置**（未核对的位置不能当真值，见 public/basemaps/README.md）。
 *   所以 markers/连线/灾害面随后端台账与烘焙资产点亮，缺数据时如实留空，绝不拿假坐标补一个几内亚湾的点。
 * - 辖区框画的是 `TIBET_RECTANGLE`（78–99°E、26–37°N，与后端 persistence/geo.py 同一口径）——真实的指挥辖区，不是编的。
 * - 数据连线只画「站点 → 其区划锚点」这条真实归属关系，且仅当站点坐标与锚点不重合时才画（重合即无连线可言）。
 */
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'

import type { CustomDataSource, Entity, ScreenSpaceEventHandler, Viewer as CesiumViewer } from 'cesium'

import mapApi, { MAP_ASSET_PATHS, normalizeAnchors } from '@/api/map'
import type { RegionAnchorDto, StationDto } from '@/api/map'
import type { ChainSummary, TelemetryReading, WarningRecord } from '@/api/types'
import { RELIEF_ATTRIBUTION } from '@/components/map/basemap'
import { colorSlots } from '@/theme/tokens'
import type { BBox, FeatureDetail, LayerVisibility, MapLayers, PlanLayer } from '@/components/map/entities'
import {
  buildLayers,
  buildRenderPlan,
  DEFAULT_LAYER_VISIBILITY,
  detailOf,
  expandBBox,
  hazardLabel,
  indexAnchors,
  layerCounts,
  PLAN_LAYERS,
  riskLabel,
  riskLegend,
  TIBET_RECTANGLE,
} from '@/components/map/entities'
import type { LayerName, MapScene, MapSceneInfo } from '@/components/map/viewer'
import { useTheme } from '@/theme/useTheme'

type CesiumModule = typeof import('cesium')

const DATA_REFRESH_MS = 30_000

const shellRef = ref<HTMLElement | null>(null)
const host = ref<HTMLElement | null>(null)
const scene = shallowRef<MapScene | null>(null)
const sceneInfo = ref<MapSceneInfo | null>(null)
const sceneError = ref<string | null>(null)
const { resolved: themeMode } = useTheme()

const readings = ref<TelemetryReading[]>([])
const warnings = ref<WarningRecord[]>([])
const chains = ref<ChainSummary[]>([])
const stations = ref<StationDto[]>([])
const anchors = ref<RegionAnchorDto[]>([])
const hazardDocument = ref<unknown>(null)
const loading = ref(false)
let loadSeq = 0

const bbox = ref<BBox | null>(null)
const zoom = ref(0)
const visibility = ref<LayerVisibility>({ ...DEFAULT_LAYER_VISIBILITY })
const selectedId = ref<string | null>(null)
const fullscreen = ref(false)
/** 悬停浮签：鼠标停在风险点/站点上时给一眼可读的摘要，点击才进完整详情。 */
const hover = ref<{ x: number; y: number; detail: FeatureDetail } | null>(null)

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
const plan = computed(() => buildRenderPlan({ layers: layers.value, bbox: bbox.value, zoom: zoom.value }))
const counts = computed(() => layerCounts(layers.value))
const detail = computed<FeatureDetail | null>(() => detailOf(layers.value, selectedId.value))
const legend = riskLegend()

const LAYER_META: { key: PlanLayer; label: string }[] = [
  { key: 'stations', label: '监测站点' },
  { key: 'warnings', label: '预警' },
  { key: 'reach', label: '触达' },
  { key: 'hazards', label: '灾害面' },
  { key: 'clusters', label: '聚合' },
]

function countOf(key: PlanLayer): number {
  return key === 'clusters' ? plan.value.clusters.length : counts.value[key]
}

const KIND_LABELS: Record<string, string> = {
  station: '监测站点',
  warning: '预警',
  reach: '触达',
  'hazard-zone': '灾害面',
}

const statusLine = computed(() =>
  sceneInfo.value
    ? `西藏指挥辖区 · 真实高程三维地形 · ${warnings.value.length} 条预警在案`
    : '三维场景加载中…',
)

function settled<T>(result: PromiseSettledResult<T>): T | null {
  return result.status === 'fulfilled' ? result.value : null
}

async function load(): Promise<void> {
  loading.value = true
  const seq = ++loadSeq
  const [telemetry, warningList, eventList, stationList, anchorFile, zoneFile] = await Promise.allSettled([
    mapApi.telemetry({ limit: 2_000 }),
    mapApi.warnings({ limit: 100 }),
    mapApi.events(100),
    mapApi.stations({ limit: 500 }),
    mapApi.regionAnchors(),
    mapApi.hazardZones(),
  ])
  // 这趟在路上时又发了更新的一趟（30s 自动刷新与手动撞车）：旧快照迟到就丢掉。
  if (seq !== loadSeq) return
  readings.value = settled(telemetry)?.items ?? []
  warnings.value = settled(warningList)?.items ?? []
  chains.value = settled(eventList)?.items ?? []
  stations.value = settled(stationList)?.items ?? []
  const anchorData = settled(anchorFile)
  anchors.value = anchorData ? normalizeAnchors(anchorData) : []
  hazardDocument.value = settled(zoneFile)
  loading.value = false
}

// ---------- onReady 之后才有的场景内部状态（呼吸 / 连线） ----------
const viewerRef = shallowRef<CesiumViewer | null>(null)
const cesiumRef = shallowRef<CesiumModule | null>(null)
let linksSource: CustomDataSource | null = null
let linksSignature = ''
let breathRaf = 0
let lastBreathTs = 0
let breathPhase = 0
let inView = true
let reduceMotion = false
let motionQuery: MediaQueryList | null = null
let intersectionObserver: IntersectionObserver | null = null
let hoverHandler: ScreenSpaceEventHandler | null = null
let hoverLastTs = 0

function readCssColor(name: string): string {
  // 兜底也走令牌真源：样式表还没挂上时（jsdom/首帧）至少还是同一支辅助蓝，不另写一个字面色。
  const fallback = colorSlots.dark['brand-alt']
  if (typeof getComputedStyle !== 'function' || typeof document === 'undefined') return fallback
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback
}

// ---------- 高原 relief：真实高程 + 垂直夸张 ----------
// 地形走 /terrain-real（Mapzen/AWS Terrain Tiles 真实 DEM 离线烘焙），影像走 relief.pmtiles
// （同一份 DEM 派生的分层设色 × 山体阴影）——几何与像素同源，旋转/拉近时山脊与河谷对得上。
// 垂直夸张只抬几何不改造数据：1.6 倍在 90 万米斜视角下读得出高原起伏，又不至于把坡度画假。
const RELIEF_EXAGGERATION = 1.6

function applyRelief(): void {
  const viewer = viewerRef.value
  if (!viewer || viewer.isDestroyed()) return
  viewer.scene.verticalExaggeration = RELIEF_EXAGGERATION
  viewer.scene.requestRender()
}

function findDataSource(viewer: CesiumViewer, name: string): CustomDataSource | null {
  const collection = viewer.dataSources
  for (let i = 0; i < collection.length; i += 1) {
    const ds = collection.get(i) as CustomDataSource
    if (ds.name === name) return ds
  }
  return null
}

/** 辖区框的抬升高度：贴地线在起伏地形上会被切片成虚线，抬到高原最高点之上更干净。 */
const AOI_HEIGHT_M = 14_000

/** 站点→区划锚点的真实归属连线 + 指挥辖区框。签名守卫：数据/主题没变就不重建。 */
function rebuildLinks(): void {
  const viewer = viewerRef.value
  const cesium = cesiumRef.value
  if (!viewer || !cesium || !linksSource || viewer.isDestroyed()) return
  const anchorIndex = indexAnchors(anchors.value)
  const arcs: { from: { lon: number; lat: number }; to: { lon: number; lat: number } }[] = []
  for (const station of layers.value.stations) {
    const anchor = anchorIndex[station.regionCode]
    if (!anchor) continue
    // 站点坐标常回退到锚点本身（台账没写经纬度时）：重合就没有连线可言，不画零长度弧。
    if (Math.abs(station.lonlat.lon - anchor.lon) < 0.01 && Math.abs(station.lonlat.lat - anchor.lat) < 0.01) continue
    arcs.push({ from: station.lonlat, to: { lon: anchor.lon, lat: anchor.lat } })
  }
  const signature = `${themeMode.value}|${arcs.map((a) => `${a.from.lon},${a.from.lat}>${a.to.lon},${a.to.lat}`).join(';')}`
  if (signature === linksSignature) return
  linksSignature = signature

  linksSource.entities.removeAll()
  const lineColor = cesium.Color.fromCssColorString(readCssColor('--aeg-color-brand-alt'))

  // 指挥辖区框：真实西藏全域边界，抬在高原之上细描，给画面一个"指挥范围"的空间锚。
  const corners = [
    { lon: TIBET_RECTANGLE.west, lat: TIBET_RECTANGLE.south },
    { lon: TIBET_RECTANGLE.east, lat: TIBET_RECTANGLE.south },
    { lon: TIBET_RECTANGLE.east, lat: TIBET_RECTANGLE.north },
    { lon: TIBET_RECTANGLE.west, lat: TIBET_RECTANGLE.north },
    { lon: TIBET_RECTANGLE.west, lat: TIBET_RECTANGLE.south },
  ]
  linksSource.entities.add({
    id: 'aegis-aoi-frame',
    polyline: {
      positions: cesium.Cartesian3.fromDegreesArrayHeights(corners.flatMap((p) => [p.lon, p.lat, AOI_HEIGHT_M])),
      width: 2,
      material: lineColor.withAlpha(0.65),
      arcType: cesium.ArcType.NONE,
    },
  })

  // 数据连线：25 段抬升弧，弧高随距离钳在 8–60km，ArcType.NONE 保留我们给的抬升高度。
  for (const arc of arcs) {
    const from = cesium.Cartesian3.fromDegrees(arc.from.lon, arc.from.lat)
    const to = cesium.Cartesian3.fromDegrees(arc.to.lon, arc.to.lat)
    const distance = cesium.Cartesian3.distance(from, to)
    const lift = Math.min(60_000, Math.max(8_000, distance * 0.12))
    const positions = []
    for (let i = 0; i <= 24; i += 1) {
      const t = i / 24
      const lon = arc.from.lon + (arc.to.lon - arc.from.lon) * t
      const lat = arc.from.lat + (arc.to.lat - arc.from.lat) * t
      positions.push(cesium.Cartesian3.fromDegrees(lon, lat, Math.sin(Math.PI * t) * lift))
    }
    linksSource.entities.add({
      polyline: {
        positions,
        width: 1.5,
        material: lineColor.withAlpha(0.35),
        arcType: cesium.ArcType.NONE,
      },
    })
  }
  viewer.scene.requestRender()
}

/** 数值属性原地改值：warningGraphics 把 pixelSize/outlineWidth 包成了 ConstantProperty，
 *  直接 setValue 即可，不必每帧 new 一个（省 GC，也少一次定义变更）。类型上它是 Property，
 *  所以收一个 unknown 再窄化，避免把裸 number 赋给 Property 的类型错。 */
function setNumberProperty(prop: unknown, value: number): void {
  if (prop && typeof (prop as { setValue?: unknown }).setValue === 'function') {
    ;(prop as { setValue: (v: number) => void }).setValue(value)
  }
}

/** 风险点呼吸：约 25fps 节流，只调制预警点的尺寸/描边，静置与减动效下完全不动。 */
function breathFrame(ts: number): void {
  breathRaf = requestAnimationFrame(breathFrame)
  if (ts - lastBreathTs < 40) return
  lastBreathTs = ts
  const viewer = viewerRef.value
  if (!viewer || viewer.isDestroyed()) return
  if (reduceMotion || !inView || (typeof document !== 'undefined' && document.visibilityState !== 'visible')) return
  const ds = findDataSource(viewer, 'aegis-warnings')
  if (!ds || !ds.show) return
  breathPhase += 0.08
  const wave = Math.sin(breathPhase)
  for (const entity of ds.entities.values as Entity[]) {
    const point = entity.point
    if (!point) continue
    setNumberProperty(point.pixelSize, 18 + wave * 3.5)
    setNumberProperty(point.outlineWidth, 3 + wave * 1.2)
  }
  viewer.scene.requestRender()
}

function resetBreath(): void {
  const viewer = viewerRef.value
  if (!viewer || viewer.isDestroyed()) return
  const ds = findDataSource(viewer, 'aegis-warnings')
  if (!ds) return
  for (const entity of ds.entities.values as Entity[]) {
    const point = entity.point
    if (!point) continue
    setNumberProperty(point.pixelSize, 18)
    setNumberProperty(point.outlineWidth, 3)
  }
  viewer.scene.requestRender()
}

function onMotionChange(event: MediaQueryListEvent): void {
  reduceMotion = event.matches
  if (reduceMotion) resetBreath()
}

/** 悬停浮签：MOUSE_MOVE 节流到 ~50ms 拾取一次，命中要素才给浮签与手型光标。 */
function attachHover(viewer: CesiumViewer, cesium: CesiumModule): void {
  hoverHandler = new cesium.ScreenSpaceEventHandler(viewer.scene.canvas)
  hoverHandler.setInputAction((event: { endPosition: { x: number; y: number } }) => {
    const now = Date.now()
    if (now - hoverLastTs < 50) return
    hoverLastTs = now
    const picked = viewer.scene.pick(
      new cesium.Cartesian2(event.endPosition.x, event.endPosition.y),
    ) as { id?: unknown } | undefined
    const id = typeof picked?.id === 'string' ? picked.id : null
    const found = id ? detailOf(layers.value, id) : null
    if (!found) {
      hover.value = null
      viewer.scene.canvas.style.cursor = ''
      return
    }
    const w = host.value?.clientWidth ?? 0
    const h = host.value?.clientHeight ?? 0
    hover.value = {
      x: Math.max(8, Math.min(event.endPosition.x + 14, w - 232)),
      y: Math.max(8, Math.min(event.endPosition.y + 14, h - 92)),
      detail: found,
    }
    viewer.scene.canvas.style.cursor = 'pointer'
  }, cesium.ScreenSpaceEventType.MOUSE_MOVE)
}

function onSceneReady(viewer: CesiumViewer, cesium: CesiumModule): void {
  viewerRef.value = viewer
  cesiumRef.value = cesium

  // 斜视角：从高原南缘向北俯看，让地形起伏读得出来。
  // 用 setView 而不是 lookAt：lookAt 会留一个锁定变换，解除后视野容易跳回球外。
  // 取景：92°E/29°N、22 万米、俯角 -35°、航向 40°——
  // 这一档能把已烘焙的西藏地形铺满画框（山脊/沟谷都读得出来），
  // 又不会拉到 135 万米把"只有局部地形"的边缘露成一块漂浮台地。
  viewer.camera.setView({
    destination: cesium.Cartesian3.fromDegrees(92, 29, 220_000),
    orientation: {
      heading: cesium.Math.toRadians(40),
      pitch: cesium.Math.toRadians(-35),
      roll: 0,
    },
  })

  // 影像与地形同源（真实 DEM 烘焙）：不再移除影像层，山体阴影设色就是底图本身。
  // 烘焙出来的设色偏灰蓝，抬一档饱和度/对比度让山脊与沟谷读得出来（只调显示，不改数据）。
  for (let i = 0; i < viewer.imageryLayers.length; i += 1) {
    const layer = viewer.imageryLayers.get(i)
    layer.saturation = 1.35
    layer.contrast = 1.15
    layer.brightness = 1.05
    layer.gamma = 1.05
  }
  if (viewer.scene.skyBox) viewer.scene.skyBox.show = false
  // 关距离雾与地面大气：雾会把远山压成一条暗带，和近景亮瓦拼出"横向亮暗缝"；
  // 指挥舱要的是整幅均匀的山体阴影，不是大气透视。
  viewer.scene.fog.enabled = false
  viewer.scene.globe.showGroundAtmosphere = false

  attachHover(viewer, cesium)

  applyRelief()

  linksSource = new cesium.CustomDataSource('aegis-links')
  viewer.dataSources.add(linksSource)
  rebuildLinks()

  if (typeof matchMedia === 'function') {
    motionQuery = matchMedia('(prefers-reduced-motion: reduce)')
    reduceMotion = motionQuery.matches
    if (typeof motionQuery.addEventListener === 'function') motionQuery.addEventListener('change', onMotionChange)
  }
  if (typeof IntersectionObserver !== 'undefined' && host.value) {
    intersectionObserver = new IntersectionObserver((entries) => {
      inView = entries.some((entry) => entry.isIntersecting)
    })
    intersectionObserver.observe(host.value)
  }
  if (typeof requestAnimationFrame === 'function') breathRaf = requestAnimationFrame(breathFrame)
}

async function initScene(): Promise<void> {
  if (!host.value) return
  try {
    const { createMapScene } = await import('@/components/map/viewer')
    const created = await createMapScene(host.value, {
      terrainUrl: MAP_ASSET_PATHS.terrainReal,
      basemap: {
        template: MAP_ASSET_PATHS.basemapTemplate,
        pmtilesUrl: MAP_ASSET_PATHS.reliefPmtiles,
        prefer: 'pmtiles',
        maximumLevel: 6,
        attribution: RELIEF_ATTRIBUTION,
      },
      initialRectangle: TIBET_RECTANGLE,
      onViewChanged: (next, nextZoom) => {
        bbox.value = next ? expandBBox(next) : null
        zoom.value = nextZoom
      },
      onSelect: (featureId) => {
        selectedId.value = featureId
      },
      themeMode: themeMode.value,
      // 区域视图不叠深空皮肤：这里要看清高原上的要素，不是看地球弧线。
      spaceLook: false,
      onReady: onSceneReady,
    })
    scene.value = created
    sceneInfo.value = created.info
    created.render(plan.value)
    for (const layer of PLAN_LAYERS) created.setLayerVisible(layer as LayerName, visibility.value[layer])
  } catch (error) {
    sceneError.value = error instanceof Error ? error.message : String(error)
  }
}

function onToggle(layer: PlanLayer): void {
  const next = !visibility.value[layer]
  visibility.value = { ...visibility.value, [layer]: next }
  scene.value?.setLayerVisible(layer as LayerName, next)
  // 连线跟着站点层走：站点关了，归属连线也收起来。
  if (layer === 'stations' && linksSource) {
    linksSource.show = next
    viewerRef.value?.scene.requestRender()
  }
}

async function toggleFullscreen(): Promise<void> {
  if (typeof document !== 'undefined' && document.fullscreenElement) {
    await document.exitFullscreen()
    return
  }
  if (fullscreen.value) {
    fullscreen.value = false
    return
  }
  const el = shellRef.value
  if (el && typeof el.requestFullscreen === 'function') {
    try {
      await el.requestFullscreen()
    } catch {
      fullscreen.value = true
    }
  } else {
    fullscreen.value = true
  }
}

function onFullscreenChange(): void {
  fullscreen.value = typeof document !== 'undefined' && !!document.fullscreenElement
}

let dataTimer: ReturnType<typeof setInterval> | null = null

onMounted(() => {
  void load()
  void initScene()
  dataTimer = setInterval(() => {
    if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return
    void load()
  }, DATA_REFRESH_MS)
  if (typeof document !== 'undefined') document.addEventListener('fullscreenchange', onFullscreenChange)
})

onBeforeUnmount(() => {
  if (dataTimer !== null) clearInterval(dataTimer)
  dataTimer = null
  if (typeof document !== 'undefined') document.removeEventListener('fullscreenchange', onFullscreenChange)
  // 呼吸/连线/监听必须在场景销毁前收掉：旧 viewer 没了还留着 rAF 只会读到一堆 destroyed 报错。
  if (breathRaf !== 0 && typeof cancelAnimationFrame === 'function') cancelAnimationFrame(breathRaf)
  breathRaf = 0
  motionQuery?.removeEventListener('change', onMotionChange)
  motionQuery = null
  intersectionObserver?.disconnect()
  intersectionObserver = null
  hoverHandler?.destroy()
  hoverHandler = null
  hover.value = null
  linksSource = null
  linksSignature = ''
  viewerRef.value = null
  cesiumRef.value = null
  scene.value?.destroy()
  scene.value = null
})

watch(themeMode, (mode) => {
  scene.value?.setTheme(mode)
  rebuildLinks()
})
watch(plan, (data) => {
  scene.value?.render(data)
})
watch(layers, () => rebuildLinks())
</script>

<template>
  <div ref="shellRef" class="terrain" :class="{ 'is-fullscreen': fullscreen }" data-testid="dashboard-terrain">
    <div ref="host" class="terrain__host"></div>

    <div
      v-if="hover"
      class="terrain__tip"
      :style="{ left: `${hover.x}px`, top: `${hover.y}px` }"
      data-testid="terrain-hover-tip"
      role="tooltip"
    >
      <span class="terrain__tip-kind">{{ KIND_LABELS[hover.detail.kind] ?? '要素' }}</span>
      <span class="terrain__tip-title">{{ hover.detail.title }}</span>
      <span
        class="terrain__tip-risk"
        :style="hover.detail.riskLevel !== null ? { color: `var(--aeg-risk-text-${hover.detail.riskLevel})` } : undefined"
        >{{ riskLabel(hover.detail.riskLevel) }}</span
      >
    </div>

    <div v-if="!sceneInfo && !sceneError" class="terrain__placeholder">
      <div class="terrain__placeholder-card">
        <p class="terrain__placeholder-title">三维地形加载中</p>
        <p class="terrain__placeholder-desc">正在装配同源底图与地形；图层开关与图例已就绪。</p>
      </div>
    </div>
    <div v-if="sceneError" class="terrain__placeholder">
      <div class="terrain__placeholder-card">
        <p class="terrain__placeholder-title">三维地形不可用</p>
        <p class="terrain__placeholder-desc">{{ sceneError }}</p>
        <p class="terrain__placeholder-hint">其余面板与清单仍可用。首次部署需把地图引擎静态资源拷到 public/cesium/ 并烘焙底图/地形。</p>
      </div>
    </div>

    <!-- 左上：图层开关（兼计数）。实色卡面板压在画布上，两主题都过 AA。 -->
    <div class="terrain__hud terrain__hud--layers">
      <button
        v-for="meta in LAYER_META"
        :key="meta.key"
        type="button"
        class="terrain__chip"
        :class="{ 'is-on': visibility[meta.key] }"
        :aria-pressed="visibility[meta.key]"
        :data-testid="`terrain-layer-${meta.key}`"
        @click="onToggle(meta.key)"
      >
        <span class="terrain__chip-dot" aria-hidden="true"></span>
        {{ meta.label }}
        <span class="terrain__chip-count">{{ countOf(meta.key) }}</span>
      </button>
    </div>

    <!-- 右上：全屏 + 去一张图 -->
    <div class="terrain__hud terrain__hud--tools">
      <button type="button" class="terrain__tool" :aria-pressed="fullscreen" data-testid="terrain-fullscreen" @click="toggleFullscreen">
        {{ fullscreen ? '退出全屏' : '全屏' }}
      </button>
      <router-link class="terrain__tool" to="/map" data-testid="terrain-open-map">一张图 →</router-link>
    </div>

    <!-- 左下：风险图例（与图上同一份 RISK_COLORS） -->
    <div class="terrain__hud terrain__hud--legend">
      <span v-for="entry in legend" :key="entry.level" class="terrain__legend-item">
        <i class="terrain__legend-swatch" :style="{ background: entry.colorCss }" aria-hidden="true"></i>
        {{ entry.label }}
      </span>
    </div>

    <!-- 右下：选中要素详情，否则一行场景状态 -->
    <div class="terrain__hud terrain__hud--detail">
      <div v-if="detail" class="terrain__detail" data-testid="terrain-detail">
        <div class="terrain__detail-head">
          <span class="terrain__detail-kind">{{ KIND_LABELS[detail.kind] ?? '要素' }}</span>
          <button type="button" class="terrain__detail-close" aria-label="关闭详情" @click="selectedId = null">×</button>
        </div>
        <div class="terrain__detail-title">{{ detail.title }}</div>
        <dl class="terrain__detail-facts">
          <div><dt>区划</dt><dd>{{ detail.regionCode || '—' }}</dd></div>
          <div>
            <dt>风险</dt>
            <dd :style="detail.riskLevel !== null ? { color: `var(--aeg-risk-text-${detail.riskLevel})` } : undefined">
              {{ riskLabel(detail.riskLevel) }}
            </dd>
          </div>
          <div><dt>灾种</dt><dd>{{ hazardLabel(detail.hazardType) }}</dd></div>
        </dl>
        <router-link class="terrain__detail-link" to="/map">在一张图查看 →</router-link>
      </div>
      <p v-else class="terrain__status" data-testid="terrain-status">{{ statusLine }}</p>
    </div>
  </div>
</template>

<style scoped>
.terrain {
  position: relative;
  width: 100%;
  height: 100%;
  min-height: 320px;
  overflow: hidden;
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-xl);
  background: var(--aeg-color-sunken);
  box-shadow: var(--aeg-shadow-md);
}
/* 全屏兜底：requestFullscreen 被拒（iframe 等）时用 fixed 铺满，仍可退出。 */
.terrain.is-fullscreen {
  position: fixed;
  inset: 0;
  z-index: 1000;
  min-height: 0;
  border-radius: 0;
}
.terrain__host {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
}
/* 暗角：把画布边缘收进面板底色里，避免深色地图与波浪底图硬切；HUD 在其上（z-index 5）。
   顶部/底部再压两条渐变：局部烘焙的地形在瓦边会有硬接缝，渐变把接缝留到画框外的视觉死区。 */
.terrain::after {
  content: '';
  position: absolute;
  inset: 0;
  z-index: 1;
  pointer-events: none;
  border-radius: inherit;
  background:
    linear-gradient(
      to bottom,
      color-mix(in srgb, var(--aeg-color-page) 96%, transparent) 0%,
      color-mix(in srgb, var(--aeg-color-page) 55%, transparent) 10%,
      transparent 26%
    ),
    linear-gradient(
      to top,
      color-mix(in srgb, var(--aeg-color-page) 80%, transparent) 0%,
      transparent 18%
    ),
    radial-gradient(
      120% 92% at 50% 42%,
      transparent 44%,
      color-mix(in srgb, var(--aeg-color-page) 58%, transparent) 100%
    );
}
.terrain__host :deep(.cesium-credit-wrapper),
.terrain__host :deep(.cesium-credit-expand-link) {
  font-size: 12px;
}

.terrain__placeholder {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0 24px;
  pointer-events: none;
}
.terrain__placeholder-card {
  pointer-events: auto;
  max-width: 460px;
  padding: var(--aeg-space-s5) var(--aeg-space-s6);
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-lg);
  background: var(--aeg-color-card);
  box-shadow: var(--aeg-shadow-md);
}
.terrain__placeholder-title {
  margin: 0 0 var(--aeg-space-s2);
  font-size: var(--aeg-font-md);
  font-weight: var(--aeg-weight-bold);
  color: var(--aeg-color-text);
}
.terrain__placeholder-desc {
  margin: 0;
  font-size: var(--aeg-font-sm);
  line-height: var(--aeg-lh-normal);
  color: var(--aeg-color-text-secondary);
}
.terrain__placeholder-hint {
  margin: var(--aeg-space-s2) 0 0;
  font-size: 12px;
  line-height: 1.7;
  color: var(--aeg-color-text-muted);
}

/* HUD 一律实色卡底（不用半透玻璃）：画布是深色，半透白底合成后次要文字会掉到 AA 以下。 */
.terrain__hud {
  position: absolute;
  z-index: 5;
  display: flex;
  gap: var(--aeg-space-s1);
  padding: var(--aeg-space-s1);
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-lg);
  background: var(--aeg-color-card);
  box-shadow: var(--aeg-shadow-sm);
}
.terrain__hud--layers {
  top: var(--aeg-space-s3);
  left: var(--aeg-space-s3);
  flex-wrap: wrap;
  max-width: calc(100% - 200px);
}
.terrain__hud--tools {
  top: var(--aeg-space-s3);
  right: var(--aeg-space-s3);
}
.terrain__hud--legend {
  bottom: var(--aeg-space-s3);
  left: var(--aeg-space-s3);
  flex-wrap: wrap;
  align-items: center;
}
.terrain__hud--detail {
  right: var(--aeg-space-s3);
  bottom: var(--aeg-space-s3);
  max-width: 280px;
}

.terrain__chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  min-height: 26px;
  padding: 2px 9px;
  border: 1px solid var(--aeg-color-border);
  border-radius: 999px;
  background: var(--aeg-color-sunken);
  color: var(--aeg-color-text-secondary);
  font-size: var(--aeg-font-xs);
  cursor: pointer;
  transition:
    border-color var(--aeg-motion-base) ease,
    color var(--aeg-motion-base) ease;
}
.terrain__chip:hover {
  border-color: var(--aeg-color-brand-line);
}
.terrain__chip.is-on {
  border-color: var(--aeg-color-brand-line);
  color: var(--aeg-color-text);
}
.terrain__chip-dot {
  width: 7px;
  height: 7px;
  border-radius: 999px;
  background: var(--aeg-color-border-strong);
}
.terrain__chip.is-on .terrain__chip-dot {
  background: var(--aeg-color-brand);
}
.terrain__chip-count {
  font-variant-numeric: tabular-nums;
  color: var(--aeg-color-text-muted);
}

.terrain__tool {
  display: inline-flex;
  align-items: center;
  min-height: 26px;
  padding: 2px 10px;
  border: 1px solid var(--aeg-color-border);
  border-radius: 999px;
  background: var(--aeg-color-sunken);
  color: var(--aeg-color-text-secondary);
  font-size: var(--aeg-font-xs);
  text-decoration: none;
  cursor: pointer;
  transition:
    border-color var(--aeg-motion-base) ease,
    color var(--aeg-motion-base) ease;
}
.terrain__tool:hover {
  border-color: var(--aeg-color-brand-line);
  color: var(--aeg-color-brand-hover);
}

.terrain__legend-item {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 0 4px;
  font-size: var(--aeg-font-xs);
  color: var(--aeg-color-text-secondary);
}
.terrain__legend-swatch {
  width: 10px;
  height: 10px;
  border-radius: 3px;
}

/* 悬停浮签：贴在指针右下、不拦截鼠标（pointer-events:none），深底细边与面板同语。 */
.terrain__tip {
  position: absolute;
  z-index: 6;
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-width: 220px;
  padding: 6px 10px;
  border: 1px solid var(--aeg-color-brand-line);
  border-radius: var(--aeg-radius-md);
  background: var(--aeg-color-elevated);
  box-shadow: var(--aeg-shadow-md);
  pointer-events: none;
}
.terrain__tip-kind {
  font-size: var(--aeg-font-xs);
  color: var(--aeg-color-text-secondary);
}
.terrain__tip-title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--aeg-font-sm);
  font-weight: var(--aeg-weight-medium);
  color: var(--aeg-color-text);
}
.terrain__tip-risk {
  font-size: var(--aeg-font-xs);
  color: var(--aeg-color-text-secondary);
}
.terrain__detail {
  display: flex;
  flex-direction: column;
  gap: var(--aeg-space-s1);
  padding: var(--aeg-space-s1) var(--aeg-space-s2);
}
.terrain__detail-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--aeg-space-s2);
}
.terrain__detail-kind {
  font-size: var(--aeg-font-xs);
  font-weight: var(--aeg-weight-semibold);
  letter-spacing: 0.06em;
  color: var(--aeg-color-brand-hover);
}
.terrain__detail-close {
  width: 22px;
  height: 22px;
  border: none;
  border-radius: var(--aeg-radius-sm);
  background: transparent;
  color: var(--aeg-color-text-muted);
  font-size: 16px;
  line-height: 1;
  cursor: pointer;
}
.terrain__detail-close:hover {
  background: var(--aeg-color-sunken);
  color: var(--aeg-color-text);
}
.terrain__detail-title {
  font-size: var(--aeg-font-sm);
  font-weight: var(--aeg-weight-semibold);
  line-height: var(--aeg-lh-snug);
  color: var(--aeg-color-text);
}
.terrain__detail-facts {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin: 0;
}
.terrain__detail-facts > div {
  display: flex;
  gap: var(--aeg-space-s2);
  font-size: var(--aeg-font-xs);
}
.terrain__detail-facts dt {
  flex: 0 0 32px;
  color: var(--aeg-color-text-muted);
}
.terrain__detail-facts dd {
  margin: 0;
  color: var(--aeg-color-text-secondary);
}
.terrain__detail-link {
  font-size: var(--aeg-font-xs);
  color: var(--aeg-color-brand-hover);
  text-decoration: underline;
}
.terrain__status {
  margin: 0;
  padding: var(--aeg-space-s1) var(--aeg-space-s2);
  font-size: var(--aeg-font-xs);
  line-height: var(--aeg-lh-snug);
  color: var(--aeg-color-text-secondary);
}
</style>