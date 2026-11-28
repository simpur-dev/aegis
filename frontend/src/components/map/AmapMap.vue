<script setup lang="ts">
/**
 * 高德 JS API 地图面（`/map` 的"真地图"档）。
 *
 * 与 Cesium 那张的关系：**两套引擎不叠在同一块画布上**。高德是它自己的渲染器，
 * 挂上它就没有 Cesium 的地形与图元；所以这里按"档"整块换表面，而不是在一个 canvas 上混着画。
 * 离线自托管那档（Cesium + 自烘瓦 + quantized-mesh）保持原样，一键切回。
 *
 * 平台的国内业务点使用 GCJ-02；案例中的境外点沿用 WGS84。
 */
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

import type { BBox, LayerVisibility, LngLat, RenderPlan } from './entities'
import type { HistoricalSite } from './gyirongCase'
import { loadAmap, type AMapImageLayer, type AMapMapInstance, type AMapNamespace, type AMapOverlay } from './amap'
import { gcj02ToWgs84, wgs84ToGcj02 } from './gcj02'
import { mapPalette } from '@/theme/map'
import { useTheme } from '@/theme/useTheme'
import { rainCellAt, rainSelectionPoint, weatherMarkerText, type WeatherMapLayer } from './weather'

const props = defineProps<{
  plan: RenderPlan
  visibility: LayerVisibility
  camera: { center: LngLat; zoom: number; pitch: number; mainlandChina: boolean }
  historicalSites?: readonly HistoricalSite[]
  weather?: WeatherMapLayer
}>()
const emit = defineEmits<{
  (event: 'select', featureId: string | null): void
  (event: 'failure'): void
  (event: 'view', bounds: BBox | null, zoom: number): void
}>()

const { resolved } = useTheme()
const host = ref<HTMLElement | null>(null)
const loading = ref(false)

let amap: AMapNamespace | null = null
let map: AMapMapInstance | null = null
let overlays: AMapOverlay[] = []
let weatherOverlays: AMapOverlay[] = []
let rainLayer: AMapImageLayer | null = null
let renderedRain: WeatherMapLayer['rain']
let unmounted = false

function zoomBy(direction: 1 | -1): void {
  if (direction === 1) map?.zoomIn()
  else map?.zoomOut()
}

function focusAt(point: LngLat): void {
  const { lon, lat } = wgs84ToGcj02(point)
  map?.setZoomAndCenter(Math.max(13, map.getZoom()), [lon, lat])
}

defineExpose({ zoomBy, focusAt })

function reportView(): void {
  if (!map || unmounted) return
  const bounds = map.getBounds()
  const sw = bounds?.getSouthWest?.()
  const ne = bounds?.getNorthEast?.()
  // 业务坐标为 WGS84。视野裁剪留出 GCJ-02 偏移余量，具体落点照常转换。
  const margin = 0.01
  const box = sw && ne ? {
    west: sw.getLng() - margin, south: sw.getLat() - margin,
    east: ne.getLng() + margin, north: ne.getLat() + margin,
  } : null
  emit('view', box, Math.round(map.getZoom()))
}

function cameraCenter(): [number, number] {
  const { center, mainlandChina } = props.camera
  const { lon, lat } = mainlandChina ? wgs84ToGcj02(center) : center
  return [lon, lat]
}

async function ensureMap(): Promise<boolean> {
  if (map) return true
  const target = host.value
  if (!target || loading.value) return false
  loading.value = true
  try {
    amap = await loadAmap()
    if (unmounted) return false
    const { Satellite, RoadNet } = amap.TileLayer ?? {}
    if (!Satellite) throw new Error('高德卫星图层不可用')
    const basemapLayers = [new Satellite()]
    if (RoadNet) basemapLayers.push(new RoadNet())
    map = new amap.Map(target, {
      zoom: props.camera.zoom,
      center: cameraCenter(),
      pitch: props.camera.pitch,
      viewMode: '3D',
      terrain: true,
      rotateEnable: true,
      pitchEnable: true,
      layers: basemapLayers,
      resizeEnable: true,
    })
    map.on('complete', reportView)
    map.on('moveend', reportView)
    map.on('zoomend', reportView)
    map.on('zoomend', () => { draw(); drawWeather() })
    map.on('click', ({ lnglat }) => {
      const cells = props.weather?.cells
      if (!lnglat || !cells?.length) return
      const location = gcj02ToWgs84({ lon: lnglat.getLng(), lat: lnglat.getLat() })
      emit('select', rainCellAt(cells, location)?.id ?? null)
    })
    return true
  } catch {
    if (!unmounted) emit('failure')
    return false
  } finally {
    loading.value = false
  }
}

function draw(): void {
  if (!amap || !map) return
  if (overlays.length) map.remove(overlays)
  overlays = []
  const plan = props.plan
  const historyColor = mapPalette(resolved.value).historicalMarker
  for (const zone of props.visibility.hazards ? plan.hazards : []) {
    overlays.push(
      new amap.Polygon({
        path: zone.outer.map((point) => {
          const shifted = wgs84ToGcj02(point)
          return [shifted.lon, shifted.lat]
        }),
        strokeColor: zone.colorCss,
        fillColor: zone.colorCss,
        fillOpacity: 0.28,
        bubble: true,
      }),
    )
  }
  const points = [
    ...(props.visibility.stations ? plan.stations : []),
    ...(props.visibility.warnings ? plan.warnings : []),
    ...(props.visibility.reach ? plan.reach : []),
  ]
  for (const point of points) {
    const shifted = wgs84ToGcj02(point.lonlat)
    const marker = new amap.CircleMarker({
      center: [shifted.lon, shifted.lat],
      radius: 6,
      strokeColor: point.colorCss,
      strokeWeight: 2,
      fillColor: point.colorCss,
      fillOpacity: 0.55,
      cursor: 'pointer',
      zIndex: 30,
      // 标题挂在 label 上：高德没有 Cesium 那套 disableDepthTest，靠 label 保证"点是谁"可读
      bubble: false,
    })
    marker.on('click', () => emit('select', point.id))
    overlays.push(marker)
  }
  for (const cluster of props.visibility.clusters ? plan.clusters : []) {
    const { lon, lat } = wgs84ToGcj02(cluster.lonlat)
    const button = document.createElement('button')
    button.type = 'button'
    button.className = 'amap-cluster-marker'
    button.textContent = String(cluster.count)
    button.setAttribute('aria-label', `放大查看 ${cluster.count} 个地图点`)
    button.style.setProperty('--cluster-fill', mapPalette(resolved.value).fillFor(cluster.colorCss))
    button.addEventListener('click', () => emit('select', cluster.id))
    overlays.push(new amap.Marker({ position: [lon, lat], content: button, anchor: 'center' }))
  }
  for (const site of map.getZoom() >= 10 ? props.historicalSites ?? [] : []) {
    if (props.weather?.points.some((point) => point.id === `weather-${site.id}` || (site.id === 'history-gyirong-port' && point.id === 'weather-gyirong-grid-reference'))) continue
    const { lon, lat } = site.mainlandChina ? wgs84ToGcj02(site.lonlat) : site.lonlat
    const label = document.createElement('button')
    label.type = 'button'
    label.className = 'amap-history-marker'
    label.textContent = site.label
    label.style.setProperty('--marker-accent', historyColor)
    label.addEventListener('click', () => emit('select', site.id))
    const marker = new amap.Marker({ position: [lon, lat], content: label, anchor: 'bottom-center' })
    overlays.push(marker)
  }
  if (overlays.length) map.add(overlays)
}

function drawWeather(): void {
  if (!amap || !map) return
  const { rain, selected, points = [], subdued } = props.weather ?? {}
  if (renderedRain !== rain) {
    if (rainLayer) map.remove(rainLayer)
    rainLayer = null
    renderedRain = rain
    if (rain && amap.ImageLayer && amap.Bounds) {
      const { west, south, east, north } = rain.bounds
      const sw = wgs84ToGcj02({ lon: west, lat: south })
      const ne = wgs84ToGcj02({ lon: east, lat: north })
      rainLayer = new amap.ImageLayer({ url: rain.url, bounds: new amap.Bounds([sw.lon, sw.lat], [ne.lon, ne.lat]), zIndex: 11 })
      map.add(rainLayer)
    }
  }
  rainLayer?.setOpacity(subdued ? 0.32 : 1)
  if (weatherOverlays.length) map.remove(weatherOverlays)
  weatherOverlays = []
  if (selected) {
    const { west, east, south, north } = selected.bounds
    const path = [[west, south], [east, south], [east, north], [west, north]].map(([lon, lat]) => {
      const point = wgs84ToGcj02({ lon, lat })
      return [point.lon, point.lat]
    })
    const overlay = new amap.Polygon({ path, strokeColor: mapPalette(resolved.value).labelText,
      fillOpacity: 0, strokeStyle: 'dashed', strokeWeight: 1.5, zIndex: 22, bubble: false })
    overlay.on('click', () => emit('select', selected.id))
    weatherOverlays.push(overlay)
  }
  for (const point of selected ? [...points, rainSelectionPoint(selected)] : points) {
    const { lon, lat } = point.mainlandChina ? wgs84ToGcj02(point.lonlat) : point.lonlat
    const label = document.createElement('button')
    label.type = 'button'
    label.className = 'amap-weather-marker'
    if (point.detail) label.classList.add('is-two-line')
    label.style.setProperty('--weather-color', point.color)
    label.textContent = weatherMarkerText(point)
    label.addEventListener('click', (event) => { event.stopPropagation(); emit('select', point.id) })
    const anchor = point.labelPlacement === 'below' ? 'top-center' : point.labelPlacement || point.mainlandChina ? 'bottom-center' : 'bottom-right'
    weatherOverlays.push(new amap.Marker({ position: [lon, lat], content: label, anchor, zIndex: 25, bubble: false }))
  }
  if (weatherOverlays.length) map.add(weatherOverlays)
}

async function render(): Promise<void> {
  if (await ensureMap()) { draw(); drawWeather() }
}

watch([() => props.plan, () => props.historicalSites, () => props.visibility], () => {
  if (map) draw()
})
watch(() => props.weather?.points.map(({ id }) => id).join(), draw)
watch(() => props.weather, drawWeather)
watch(() => props.camera, ({ zoom, pitch }) => {
  map?.setZoomAndCenter(zoom, cameraCenter())
  map?.setPitch(pitch)
  map?.setRotation(0)
})

watch(resolved, draw)

onBeforeUnmount(() => {
  unmounted = true
  map?.destroy()
  map = null
  overlays = []
  weatherOverlays = []
  rainLayer = null
})

onMounted(() => { void render() })
</script>

<template>
  <div class="amap-frame">
    <div ref="host" class="amap-host" data-testid="amap-host" />
    <div v-if="loading" class="amap-note" role="status">正在加载高德地形…</div>
  </div>
</template>

<style scoped>
:global(.amap-weather-marker) {
  padding: 5px 9px;
  color: var(--aeg-color-map-panel-text);
  background: var(--aeg-color-map-panel-bg);
  border: 1px solid var(--weather-color);
  border-radius: 999px;
  box-shadow: var(--aeg-shadow-sm);
  font: 600 12px system-ui, sans-serif;
  white-space: nowrap;
  cursor: pointer;
}
:global(.amap-weather-marker.is-two-line) {
  border-radius: var(--aeg-radius-md);
  line-height: var(--aeg-lh-snug);
  white-space: pre;
}
/* 高度跟父级走：MapView 的 .map-frame 现在是绝对定位铺满（inset: 0），有确定高度，
   所以这里用 100% 不会塌缩——原来那档 68vh 会把高德地图只画到上半屏，下面露出页面底色。
   JS API 在建图那一刻量容器高，容器必须是满的（真机曾量到 amap-layer 1714×0）。 */
.amap-frame {
  position: relative;
  width: 100%;
  height: 100%;
  min-height: 0;
}
.amap-host {
  width: 100%;
  height: 100%;
}
.amap-host :deep(.amap-cluster-marker) {
  min-width: 30px;
  height: 30px;
  padding: 0 6px;
  border: 2px solid var(--cluster-fill);
  border-radius: var(--aeg-radius-pill);
  background: var(--aeg-color-map-floatbar-bg);
  color: var(--aeg-color-map-floatbar-text);
  font-weight: var(--aeg-weight-semibold);
  cursor: pointer;
}
.amap-host :deep(.amap-history-marker) {
  position: relative;
  display: block;
  margin-bottom: 9px;
  padding: 5px 9px;
  border: 1px solid var(--aeg-color-map-panel-border);
  border-radius: var(--aeg-radius-md);
  background: var(--aeg-color-map-floatbar-bg);
  color: var(--aeg-color-map-floatbar-text);
  box-shadow: var(--aeg-shadow-md);
  font-size: var(--aeg-font-sm);
  font-weight: var(--aeg-weight-semibold);
  white-space: nowrap;
  cursor: pointer;
}
.amap-host :deep(.amap-history-marker::after) {
  position: absolute;
  top: calc(100% + 2px);
  left: calc(50% - 5px);
  width: 10px;
  height: 10px;
  border: 2px solid var(--aeg-color-map-floatbar-bg);
  border-radius: 50%;
  background: var(--marker-accent);
  content: '';
}
.amap-note {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--aeg-space-s2);
  padding: var(--aeg-space-s4);
  text-align: center;
  font-size: var(--aeg-font-sm);
  color: var(--aeg-color-text-secondary);
  background: var(--aeg-color-sunken);
}
</style>
