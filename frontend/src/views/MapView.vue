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
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'

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
import AmapMap from '@/components/map/AmapMap.vue'
import { terrainProbeUrl } from '@/components/map/terrain'
import type { LayerName, MapScene, MapSceneInfo } from '@/components/map/viewer'
import { useTheme } from '@/theme/useTheme'
import { readBasemapMode, writeBasemapMode, type BasemapMode } from '@/components/map/external'

/** 数据轮询与源探针周期：野外弱网下"少而稳"胜过"快而抖"。 */
const DATA_REFRESH_MS = 30_000
const PROBE_REFRESH_MS = 20_000

/** host 必须是空容器：Cesium 往里塞 canvas，占位文案做它的兄弟节点（避免被清空/遮挡）。 */
const host = ref<HTMLElement | null>(null)
const scene = shallowRef<MapScene | null>(null)
const sceneInfo = ref<MapSceneInfo | null>(null)
const sceneError = ref<string | null>(null)
/** 画布内部读不到 CSS 变量，所以主题要显式喂给场景（见 `@/theme/map`）。 */
const { resolved: themeMode } = useTheme()
/**
 * 底图档。默认离线自托管（交付形态不依赖外部主机，见 external.ts）；'amap' 整块换成
 * 高德 JS API 渲染面（`AmapMap.vue`）。场景层（viewer.ts）永远是离线链、不感知这个档——
 * 换档是页面级"换组件"，不是换影像层。
 */
const basemapMode = ref<BasemapMode>(readBasemapMode(typeof localStorage === 'undefined' ? null : localStorage))
/** 画面上一行说实话：档名 + 纠偏口径。高德档是带 key 的官方 JS API（key 在 .env.local，不入库）。 */
const basemapSummary = computed(() =>
  basemapMode.value === 'amap' ? '高德 JS API · 图元按 GCJ02 纠偏' : '离线自托管（可切「高德在线」）',
)

const readings = ref<TelemetryReading[]>([])
const warnings = ref<WarningRecord[]>([])
const chains = ref<ChainSummary[]>([])
const stations = ref<StationDto[]>([])
const anchors = ref<RegionAnchorDto[]>([])
const hazardDocument = ref<unknown>(null)

const loading = ref(false)
const autoRefresh = ref(true)
/** 左右两列悬浮面板是否收起：收起来只看地图。 */
const panelsCollapsed = ref(false)
/** 收起按钮点下去那一刻的位置（相对地图壳），展开按钮就浮回同一个角落 */
const expandPos = ref<{ right: number; top: number } | null>(null)
const shellRef = ref<HTMLElement | null>(null)

function collapsePanels(event: MouseEvent): void {
  const button = event.currentTarget as HTMLElement | null
  const shell = shellRef.value
  if (button && shell) {
    const b = button.getBoundingClientRect()
    const s = shell.getBoundingClientRect()
    expandPos.value = { right: Math.max(12, Math.round(s.right - b.right)), top: Math.round(b.top - s.top) }
  }
  panelsCollapsed.value = true
}
const regionCode = ref<string>('')
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
const plan = computed(() => buildRenderPlan({ layers: viewLayers.value, bbox: bbox.value, zoom: zoom.value }))
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
  [...new Set([...anchors.value.map((anchor) => anchor.code), ...readings.value.map((reading) => reading.region_code)])]
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
      terrain: terrainProbeUrl(MAP_ASSET_PATHS.terrain),
      localBasemap: MAP_ASSET_PATHS.pmtiles,
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
  const anchorData = settled(anchorFile)
  anchors.value = anchorData ? normalizeAnchors(anchorData) : []
  hazardDocument.value = settled(zoneFile)

  const collected: string[] = []
  if (isMissingResource(rejectedReason(stationList))) {
    collected.push('后端尚未开放 GET /api/v1/stations：站点经纬度不可得，缺坐标的站点进「未定位清单」，不落 (0,0)。')
  }
  if (isMissingResource(rejectedReason(anchorFile))) {
    collected.push(
      `未找到 ${MAP_ASSET_PATHS.regionAnchors}：区域锚点缺失，预警与站点无处落点（烘焙说明见 public/basemaps/README.md）。`,
    )
  }
  if (isMissingResource(rejectedReason(zoneFile))) {
    collected.push(`未找到 ${MAP_ASSET_PATHS.hazardZones}：灾害分区面为空图层。`)
  }
  for (const result of [telemetry, warningList, eventList]) {
    const reason = rejectedReason(result)
    if (reason) collected.push(`数据读取失败：${reason instanceof Error ? reason.message : String(reason)}`)
  }
  notes.value = collected
  loading.value = false
  await probeNetwork()
}

/** 地图是本页最重的一块：cesium 只在真正建场时动态取；建场失败也不影响右侧文字清单。 */
async function initScene(): Promise<void> {
  if (!host.value) return
  try {
    const { createMapScene } = await import('@/components/map/viewer')
    const created = await createMapScene(host.value, {
      terrainUrl: MAP_ASSET_PATHS.terrain,
      // prefer:'template'：本地烘过真 OSM 瓦片时优先用它们（探针命中 z0/0/0 才算可达），
      // 没烘（新克隆）就自动退回仓库自带的合成 PMTiles——两条路都是同源，降级链不变。
      // maximumLevel 必须与烘焙档位一致：写 14 而瓦只到 z10，Cesium 会去要 z11–z14 并吃一串 404。
      basemap: {
        template: MAP_ASSET_PATHS.basemapTemplate,
        pmtilesUrl: MAP_ASSET_PATHS.pmtiles,
        prefer: 'template',
        maximumLevel: 10,
      },
      fetchLike: probeFetch,
      onViewChanged: (next, nextZoom) => {
        bbox.value = next ? expandBBox(next) : null
        zoom.value = nextZoom
      },
      onSelect: (featureId) => {
        selectedId.value = featureId
      },
      themeMode: themeMode.value,
      // 「深空皮肤」：只在拉远到能看见地球弧线时生效（高度门槛在 viewer.ts）。
      // 只改画面观感，不动影像/地形/要素；其他页面不传这个开关，表现与之前一致。
      spaceLook: true,
    })
    scene.value = created
    sceneInfo.value = created.info
    created.render(plan.value)
  } catch (error) {
    sceneError.value = error instanceof Error ? error.message : String(error)
    message.error('三维场景初始化失败，已退回文字清单模式')
  }
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

function selectFeature(id: string): void {
  selectedId.value = id
  const feature = detailOf(layers.value, id)
  if (feature && 'lonlat' in feature && feature.lonlat) scene.value?.focusAt(feature.lonlat)
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
  scene.value?.fitBBox(box ? expandBBox(box) : TIBET_RECTANGLE)
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
    if (autoRefresh.value) void load()
  }, DATA_REFRESH_MS)
  probeTimer = window.setInterval(() => void probeNetwork(), PROBE_REFRESH_MS)
  window.addEventListener('offline', onBrowserOffline)
  window.addEventListener('online', onBrowserOnline)
})

onBeforeUnmount(() => {
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

/** 底图档翻转：页面级换渲染面（v-if 切 AmapMap/Cesium host），把选择记下来（下次进页还是它）。 */
watch(
  basemapMode,
  (mode) => {
    writeBasemapMode(typeof localStorage === 'undefined' ? null : localStorage, mode)
    /**
     * 切到高德：`v-if` 会把 Cesium 的宿主 div 整个卸载掉，**连带把 canvas 一起带走**；
     * 而场景对象还活着（`scene.value !== null`），于是切回离线时既不重建、也没有画布可画——
     * 真机实测：宿主 692×490、`.cesium-widget` 不存在、画面只剩 `.map-host` 的深蓝背景。
     * 所以切走时必须销毁场景，切回时重建。
     */
    if (mode === 'amap') {
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
  <div ref="shellRef" class="map-shell" :class="{ 'is-panels-collapsed': panelsCollapsed }">
    <!-- 顶部悬浮工具条：标题 + 底图/区域/刷新/定位/自动刷新，半透明压在图上 -->
    <div class="map-floatbar">
      <PageHero
        class="map-floatbar__hero"
        title="应急一张图"
        :badge="basemapMode === 'amap' ? '在线底图' : '离线三维'"
        :caption="
          basemapMode === 'amap'
            ? '底图走高德 JS API 官方通道（key 放 frontend/.env.local，不入库）；要素坐标已按 WGS84→GCJ-02 纠偏。加载失败会在图上说明原因，切回「离线自托管」即恢复交付形态。'
            : '底图、地形与矢量瓦片由本进程自托管，弱网与断网下仍可用。'
        "
      />
      <a-space wrap class="map-floatbar__tools">
        <!-- 底图档：默认离线自托管（交付形态，见 external.ts 的默认档论证）；高德是带 key 的 JS API 档，加载失败会在图上说明 -->
        <a-radio-group v-model:value="basemapMode" size="small" button-style="solid" data-testid="basemap-mode">
          <a-radio-button value="offline">离线自托管</a-radio-button>
          <a-radio-button value="amap">高德在线</a-radio-button>
        </a-radio-group>
        <a-select v-model:value="regionCode" style="width: 150px" placeholder="全部区域" size="small" @change="load">
          <a-select-option value="">全部区域</a-select-option>
          <a-select-option v-for="code in regionOptions" :key="code" :value="code">{{ code }}</a-select-option>
        </a-select>
        <a-button size="small" :loading="loading" @click="load">刷新</a-button>
        <a-button size="small" @click="focusAll">定位到有点的区域</a-button>
        <a-switch v-model:checked="autoRefresh" size="small" />
        <span class="muted">自动刷新 30s</span>
      </a-space>
    </div>

    <!-- 提示：加载告警与"定位退不回全局"的原因，压在工具条下方 -->
    <div v-if="notes.length || focusNote" class="map-floatnotes">
      <a-alert v-for="note in notes" :key="note" type="warning" show-icon :message="note" />
      <!-- 定位这颗按钮先前点了没反应也不解释；现在把"退不回全局视野的原因"说一句 -->
      <p v-if="focusNote" class="muted" data-testid="focus-note">{{ focusNote }}</p>
    </div>

    <!-- 地图铺满整块，左右列与顶部条都是悬浮层 -->
    <div class="map-frame">
      <!-- 高德 JS API 是另一套渲染引擎，与 Cesium 不叠在同一块画布上：整块换表面 -->
      <AmapMap v-if="basemapMode === 'amap'" :plan="plan" @select="selectFeature" />
      <template v-else>
        <div ref="host" class="map-host" />
        <div v-if="!sceneInfo && !sceneError" class="map-placeholder">
          正在加载三维场景（地图引擎独立分块，按需下载）…
        </div>
        <div v-if="sceneError" class="map-placeholder">
          <div>三维场景不可用：{{ sceneError }}</div>
          <div class="hint">
            右侧面板与清单仍可用。首次部署需把 需把地图引擎的静态资源目录（Workers / Assets / ThirdParty /
            Widgets）拷到
            <code>public/cesium/</code>，并烘焙底图与地形资产（清单见
            public/basemaps/README.md、public/terrain/README.md）。
          </div>
        </div>
      </template>
    </div>

    <!-- 左列：离线状态 + 未定位清单 + 未渲染的几何 -->
    <div class="map-float-col map-float-col--left">
      <MapStatusPanel
        :status="machine.status"
        :status-reason="machine.reason"
        :basemap-message="sceneInfo?.basemapMessage ?? '尚未建场（状态见上方标签）'"
        :terrain-message="sceneInfo?.terrainMessage ?? '尚未建场'"
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
    <div class="map-float-col map-float-col--right">
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
            class="map-panel-toggle"
            size="small"
            :aria-pressed="panelsCollapsed"
            title="收起两侧面板，只看地图"
            data-testid="toggle-panels"
            @click="collapsePanels"
          >
            收起 »
          </a-button>
        </template>
      </MapLayersPanel>
    </div>

    <!-- 收起后右列滑出视口，留一枚悬浮按钮把它拉回来 -->
    <a-button
      v-if="panelsCollapsed"
      class="map-expand-btn"
      size="small"
      title="展开两侧面板"
      data-testid="toggle-panels-expand"
      :style="expandPos ? { right: `${expandPos.right}px`, top: `${expandPos.top}px` } : undefined"
      @click="panelsCollapsed = false"
    >
      « 展开面板
    </a-button>

    <div class="scene-bar">
      <!-- 视野与缩放是 Cesium 相机的量，高德档下没有这两个数，别摆一个假"未就绪" -->
      <span v-if="basemapMode === 'offline'">
        视野：{{
          bbox
            ? `${bbox.west.toFixed(2)}, ${bbox.south.toFixed(2)} → ${bbox.east.toFixed(2)}, ${bbox.north.toFixed(2)}`
            : '未就绪'
        }}
      </span>
      <span>· 上图要素 {{ plottedTotal }} · 未定位 {{ unlocated.length }}</span>
      <span v-if="basemapMode === 'offline'">· 缩放 {{ zoom }}</span>
      <span v-if="plan.clustered" class="hint">· 已按网格聚合（圆点上的数字=该格要素数）</span>
      <!-- 底图来源与坐标系要说在画面上：只写在代码注释里，看图的人无从知道落点被纠过偏 -->
      <span class="hint">· 底图：{{ basemapSummary }}</span>
    </div>
  </div>
</template>

<style scoped>
/* —— 全屏地图壳：铺满顶栏以下，所有信息都是浮层 —— */
.map-shell {
  position: relative;
  height: calc(100vh - var(--aeg-header-height));
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
  border-radius: var(--aeg-radius-pill);
  background: var(--aeg-color-map-floatbar-bg);
  backdrop-filter: blur(var(--aeg-glass-blur));
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
  flex: 0 0 auto;
}

/* 告警与定位提示：条下方浮一小块，不挡地图拖拽 */
.map-floatnotes {
  position: absolute;
  top: 78px;
  left: var(--aeg-space-s3);
  right: var(--aeg-space-s3);
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
  backdrop-filter: blur(var(--aeg-glass-blur));
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
  background: #101923;
}
/*
 * 「深空皮肤」的暗角：只在看得见地球弧线时压一圈，让球体边缘"浮"在星空里。
 * 颜色本体由 viewer.ts 叠的程序化能量纹理负责——不对画布做整体调色，
 * 那会把星空和要素一起染蓝。类名由 viewer.ts 按相机高度切换。
 */
.map-host.is-space-view::after {
  content: '';
  position: absolute;
  inset: 0;
  pointer-events: none;
  background: radial-gradient(circle at 50% 46%, transparent 42%, var(--aeg-color-overlay-scrim) 100%);
}
.map-placeholder {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: #d9d9d9;
  font-size: 13px;
  text-align: center;
  padding: 0 24px;
  pointer-events: none;
}
.map-placeholder .hint {
  color: var(--aeg-color-text-secondary);
  font-size: 12px;
  line-height: 1.7;
}

/* —— 左右两列悬浮面板 ——
   列容器不吃鼠标事件，面板本体恢复 auto：中间地图仍可拖拽/缩放。
   底色与主题相反（浅色主题用深蓝、深色主题用浅蓝），所以列内把"文字三档"整体反转：
   子组件里的 .note/.label/.count/.risk-chip 都读这几个变量，一处反转、全列生效。 */
.map-float-col {
  --aeg-color-text: var(--aeg-color-map-panel-text);
  --aeg-color-text-secondary: var(--aeg-color-map-panel-text-muted);
  --aeg-color-text-muted: var(--aeg-color-map-panel-text-muted);
  position: absolute;
  top: 78px;
  bottom: var(--aeg-space-s3);
  z-index: 5;
  width: 300px;
  overflow-y: auto;
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
.map-shell.is-panels-collapsed .map-float-col--left {
  opacity: 0;
  transform: translateX(calc(-100% - var(--aeg-space-s3)));
}
.map-shell.is-panels-collapsed .map-float-col--right {
  opacity: 0;
  transform: translateX(calc(100% + var(--aeg-space-s3)));
}
.map-shell.is-panels-collapsed .map-float-col :deep(.ant-card) {
  pointer-events: none;
}
.map-shell.is-panels-collapsed .scene-bar {
  left: var(--aeg-space-s3);
  right: var(--aeg-space-s3);
}
/* 收起按钮：定位在「要素详情」卡的右下角（锚点由 MapLayersPanel 的 .detail-card 提供） */
.map-panel-toggle {
  position: absolute;
  right: var(--aeg-space-s3);
  bottom: var(--aeg-space-s3);
  z-index: 2;
}
/* 收起后把两列拉回来的悬浮按钮：位置取收起那一刻按钮的角落（内联 style 覆盖），
   没有量到位置时落到右上角兜底 */
.map-expand-btn {
  position: absolute;
  top: 78px;
  right: var(--aeg-space-s3);
  z-index: 7;
  border-color: var(--aeg-color-map-panel-border);
  background: var(--aeg-color-map-floatbar-bg);
  backdrop-filter: blur(var(--aeg-glass-blur));
}
/* 面板本体：半透明玻璃，卡内白底一律去掉 */
.map-float-col :deep(.ant-card) {
  pointer-events: auto;
  background: var(--aeg-color-map-panel-bg);
  border: 1px solid var(--aeg-color-map-panel-border);
  border-radius: var(--aeg-radius-lg);
  backdrop-filter: blur(var(--aeg-glass-blur));
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
  bottom: var(--aeg-space-s3);
  z-index: 6;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 4px 12px;
  border: 1px solid var(--aeg-color-map-panel-border);
  border-radius: var(--aeg-radius-pill);
  background: var(--aeg-color-map-floatbar-bg);
  backdrop-filter: blur(var(--aeg-glass-blur));
  font-size: 12px;
  color: var(--aeg-color-text-secondary);
}
.scene-bar .hint {
  /* 这行提示原来写死 #fa8c16——在白底卡上是 2.38:1，12px 小字根本读不清（真机体检按 4.5:1 拦下）。
     警告档走令牌：浅主题 #B45309、深主题 #FBBF24，两档都在自己的卡面上过 4.5:1。 */
  color: var(--aeg-color-status-warning);
}
.muted {
  font-size: 12px;
  color: var(--aeg-color-text-secondary);
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
