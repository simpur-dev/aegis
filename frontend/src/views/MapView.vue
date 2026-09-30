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
import type { ChainSummary, TelemetryReading, WarningRecord } from '@/api/types'
import type { BBox, LayerVisibility, MapLayers, PlanLayer } from '@/components/map/entities'
import {
  bboxOfPoints,
  buildLayers,
  buildRenderPlan,
  DEFAULT_LAYER_VISIBILITY,
  detailOf,
  expandBBox,
  layerCounts,
  TIBET_RECTANGLE,
  toUnlocatedItems,
} from '@/components/map/entities'
import { collectFacts, initialMachine, reduceOffline, tileUrlForTemplate } from '@/components/map/offline'
import type { OfflineMachine } from '@/components/map/offline'
import MapPanel from '@/components/map/MapPanel.vue'
import { terrainProbeUrl } from '@/components/map/terrain'
import type { LayerName, MapScene, MapSceneInfo } from '@/components/map/viewer'

/** 数据轮询与源探针周期：野外弱网下"少而稳"胜过"快而抖"。 */
const DATA_REFRESH_MS = 30_000
const PROBE_REFRESH_MS = 20_000

/** host 必须是空容器：Cesium 往里塞 canvas，占位文案做它的兄弟节点（避免被清空/遮挡）。 */
const host = ref<HTMLElement | null>(null)
const scene = shallowRef<MapScene | null>(null)
const sceneInfo = ref<MapSceneInfo | null>(null)
const sceneError = ref<string | null>(null)

const readings = ref<TelemetryReading[]>([])
const warnings = ref<WarningRecord[]>([])
const chains = ref<ChainSummary[]>([])
const stations = ref<StationDto[]>([])
const anchors = ref<RegionAnchorDto[]>([])
const hazardDocument = ref<unknown>(null)

const loading = ref(false)
const autoRefresh = ref(true)
const regionCode = ref<string>('')
const notes = ref<string[]>([])
const machine = ref<OfflineMachine>(initialMachine())
const bbox = ref<BBox | null>(null)
const zoom = ref(0)
const visibility = ref<LayerVisibility>({ ...DEFAULT_LAYER_VISIBILITY })
const selectedId = ref<string | null>(null)

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

const plan = computed(() => buildRenderPlan({ layers: layers.value, bbox: bbox.value, zoom: zoom.value }))
const counts = computed(() => layerCounts(layers.value))
const unlocated = computed(() => toUnlocatedItems(layers.value.unplaced))
const detail = computed(() => detailOf(layers.value, selectedId.value))
const plottedTotal = computed(() => counts.value.stations + counts.value.warnings + counts.value.reach + counts.value.hazards)
const regionOptions = computed(() =>
  [...new Set([...anchors.value.map((anchor) => anchor.code), ...readings.value.map((reading) => reading.region_code)])]
    .filter(Boolean)
    .sort(),
)

/** fetch 只在这一处取全局引用，便于探针与建场共用同一注入点。 */
function probeFetch(url: string, init?: { method?: string; signal?: AbortSignal }): Promise<{ ok: boolean; status: number }> {
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

/** 区域过滤走后端真实查询参数（app.py 的 telemetry/warnings 都收 region_code），不在前端假过滤。 */
async function load(): Promise<void> {
  loading.value = true
  const filter = regionCode.value ? { region_code: regionCode.value } : {}
  const [telemetry, warningList, eventList, stationList, anchorFile, zoneFile] = await Promise.allSettled([
    mapApi.telemetry({ ...filter, limit: 2_000 }),
    mapApi.warnings({ ...filter, limit: 100 }),
    mapApi.events(100),
    mapApi.stations({ ...filter, limit: 500 }),
    mapApi.regionAnchors(),
    mapApi.hazardZones(),
  ])

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
    collected.push(`未找到 ${MAP_ASSET_PATHS.regionAnchors}：区域锚点缺失，预警与站点无处落点（烘焙说明见 public/basemaps/README.md）。`)
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
      basemap: { template: MAP_ASSET_PATHS.basemapTemplate, pmtilesUrl: MAP_ASSET_PATHS.pmtiles },
      fetchLike: probeFetch,
      onViewChanged: (next, nextZoom) => {
        bbox.value = next ? expandBBox(next) : null
        zoom.value = nextZoom
      },
      onSelect: (featureId) => {
        selectedId.value = featureId
      },
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

function selectFeature(id: string): void {
  selectedId.value = id
  const feature = detailOf(layers.value, id)
  if (feature && 'lonlat' in feature && feature.lonlat) scene.value?.focusAt(feature.lonlat)
}

function focusAll(): void {
  selectedId.value = null
  // 外接框由 entities.bboxOfPoints 算（纯函数、已单测），这里只决定相机去哪
  const box = bboxOfPoints(layers.value.stations.map((feature) => feature.lonlat))
  scene.value?.fitBBox(box ? expandBBox(box) : TIBET_RECTANGLE)
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
  <div>
    <a-card size="small" title="一张图 · 自托管离线三维（Cesium + quantized-mesh + PMTiles）">
      <template #extra>
        <a-space wrap>
          <a-select v-model:value="regionCode" style="width: 170px" placeholder="全部区域" @change="load">
            <a-select-option value="">全部区域</a-select-option>
            <a-select-option v-for="code in regionOptions" :key="code" :value="code">{{ code }}</a-select-option>
          </a-select>
          <a-button :loading="loading" @click="load">刷新</a-button>
          <a-button @click="focusAll">定位到有点的区域</a-button>
          <a-switch v-model:checked="autoRefresh" size="small" />
          <span class="muted">自动刷新 30s</span>
        </a-space>
      </template>
      <a-alert v-for="note in notes" :key="note" type="warning" show-icon banner :message="note" style="margin-bottom: 4px" />
    </a-card>

    <a-row :gutter="12" style="margin-top: 12px">
      <a-col :xs="24" :lg="17">
        <a-card size="small" :body-style="{ padding: '0' }">
          <div class="map-frame">
            <div ref="host" class="map-host" />
            <div v-if="!sceneInfo && !sceneError" class="map-placeholder">正在加载三维场景（地图引擎独立分块，按需下载）…</div>
            <div v-if="sceneError" class="map-placeholder">
              <div>三维场景不可用：{{ sceneError }}</div>
              <div class="hint">
                右侧面板与清单仍可用。首次部署需把
                需把地图引擎的静态资源目录（Workers / Assets / ThirdParty / Widgets）拷到
                <code>public/cesium/</code>，并烘焙底图与地形资产（清单见 public/basemaps/README.md、public/terrain/README.md）。
              </div>
            </div>
          </div>
          <div class="scene-bar">
            <span>
              视野：{{ bbox ? `${bbox.west.toFixed(2)}, ${bbox.south.toFixed(2)} → ${bbox.east.toFixed(2)}, ${bbox.north.toFixed(2)}` : '未就绪' }}
            </span>
            <span>· 缩放 {{ zoom }} · 上图要素 {{ plottedTotal }} · 未定位 {{ unlocated.length }}</span>
            <span v-if="plan.clustered" class="hint">· 已按网格聚合（圆点上的数字=该格要素数）</span>
          </div>
        </a-card>
      </a-col>
      <a-col :xs="24" :lg="7">
        <MapPanel
          :status="machine.status"
          :status-reason="machine.reason"
          :basemap-message="sceneInfo?.basemapMessage ?? '尚未建场（状态见上方标签）'"
          :terrain-message="sceneInfo?.terrainMessage ?? '尚未建场'"
          :counts="counts"
          :visibility="visibility"
          :unlocated="unlocated"
          :rejected="layers.rejected"
          :detail="detail"
          :clustered="plan.clustered"
          :zoom="zoom"
          :loading="loading"
          :scene-error="sceneError"
          @toggle="onToggle"
          @select="selectFeature"
          @refresh="load"
        />
      </a-col>
    </a-row>
  </div>
</template>

<style scoped>
.map-frame {
  position: relative;
}
.map-host {
  height: 68vh;
  min-height: 420px;
  width: 100%;
  background: #101923;
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
  color: #8c8c8c;
  font-size: 12px;
  line-height: 1.7;
}
.scene-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 6px 10px;
  font-size: 12px;
  color: #8c8c8c;
  border-top: 1px solid #f0f0f0;
  background: #fff;
}
.scene-bar .hint {
  color: #fa8c16;
}
.muted {
  font-size: 12px;
  color: #8c8c8c;
}
</style>
