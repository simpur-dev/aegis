<script setup lang="ts">
/**
 * 高德 JS API 地图面（`/map` 的"真地图"档）。
 *
 * 与 Cesium 那张的关系：**两套引擎不叠在同一块画布上**。高德是它自己的渲染器，
 * 挂上它就没有 Cesium 的地形与图元；所以这里按"档"整块换表面，而不是在一个 canvas 上混着画。
 * 离线自托管那档（Cesium + 自烘瓦 + quantized-mesh）保持原样，一键切回。
 *
 * 坐标：高德是 GCJ-02，数据里的经纬度是 WGS84，落图前一律过 `wgs84ToGcj02`——
 * 不纠偏就整体偏几百米，而画面看着完全正常（这是应急产品里最不该有的那种错）。
 */
import { onBeforeUnmount, ref, watch } from 'vue'

import type { RenderPlan } from './entities'
import { loadAmap, type AMapMapInstance, type AMapNamespace, type AMapOverlay } from './amap'
import { wgs84ToGcj02 } from './gcj02'
import { amapStyleName } from '@/theme/map'
import { useTheme } from '@/theme/useTheme'

const props = defineProps<{ plan: RenderPlan; center?: [number, number] }>()
const emit = defineEmits<{ (event: 'select', featureId: string): void }>()

const { resolved } = useTheme()
const host = ref<HTMLElement | null>(null)
const error = ref<string | null>(null)
const status = ref<'idle' | 'loading' | 'ready' | 'failed'>('idle')

let amap: AMapNamespace | null = null
let map: AMapMapInstance | null = null
let overlays: AMapOverlay[] = []

async function ensureMap(): Promise<boolean> {
  if (map) return true
  if (!host.value) return false
  status.value = 'loading'
  error.value = null
  try {
    amap = await loadAmap()
    map = new amap.Map(host.value, {
      zoom: props.center ? 7 : 5,
      center: props.center ?? [91.13, 29.66],
      viewMode: '2D',
      // 高德的默认控件里那几样（比例尺外的搜索、路名切换等）在值班台上都是噪声
      mapStyle: amapStyleName(resolved.value),
      resizeEnable: true,
    })
    status.value = 'ready'
    return true
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : String(reason)
    status.value = 'failed'
    return false
  }
}

function draw(): void {
  if (!amap || !map) return
  if (overlays.length) map.remove(overlays)
  overlays = []
  const plan = props.plan
  for (const zone of plan.hazards) {
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
    ...plan.stations.map((item) => ({ id: item.id, title: item.title, color: item.colorCss, lonlat: item.lonlat })),
    ...plan.warnings.map((item) => ({ id: item.id, title: item.title, color: item.colorCss, lonlat: item.lonlat })),
    ...plan.reach.map((item) => ({ id: item.id, title: item.title, color: item.colorCss, lonlat: item.lonlat })),
  ]
  for (const point of points) {
    const shifted = wgs84ToGcj02(point.lonlat)
    const marker = new amap.CircleMarker({
      center: [shifted.lon, shifted.lat],
      radius: 6,
      strokeColor: point.color,
      strokeWeight: 2,
      fillColor: point.color,
      fillOpacity: 0.55,
      cursor: 'pointer',
      // 标题挂在 label 上：高德没有 Cesium 那套 disableDepthTest，靠 label 保证"点是谁"可读
      bubble: true,
    })
    marker.on('click', () => emit('select', point.id))
    overlays.push(marker)
  }
  if (overlays.length) map.add(overlays)
}

async function render(): Promise<void> {
  if (await ensureMap()) draw()
}

watch(() => props.plan, () => {
  void render()
})

/** 主题翻转 → 换官方样式档。样式名是画布内部的档位，与 Cesium/ECharts 同一条纪律：派生层给档，组件不自己挑。 */
watch(resolved, (mode) => {
  map?.setMapStyle(amapStyleName(mode))
})

onBeforeUnmount(() => {
  map?.destroy()
  map = null
  overlays = []
})

void render()
</script>

<template>
  <div class="amap-frame">
    <div ref="host" class="amap-host" data-testid="amap-host" />
    <div v-if="status === 'loading'" class="amap-note">正在加载高德 JS API…</div>
    <!-- 加载失败不静默退回空白：值班台看到空白会以为"这一片没有站点"，而真相是地图引擎没起来 -->
    <div v-if="status === 'failed'" class="amap-note is-error" data-testid="amap-error">
      <div>高德地图不可用：{{ error }}</div>
      <div class="amap-hint">
        检查 <code>.env.local</code> 里的 <code>VITE_AMAP_KEY</code>（以及账号要求的安全密钥
        <code>VITE_AMAP_SECURITY_CODE</code>）与域名白名单；或切回「离线自托管」档。
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 高度必须是实值，不能是 100%：父级 .map-frame 高度 auto，百分比会塌缩成 0，
   而 JS API 在建图那一刻量容器高——量到 0 画布就是 0 高，之后再也不长（真机量到 amap-layer 1714×0）。
   68vh/420px 与 Cesium 那档的 .map-host 保持同一约定（MapView.vue）。 */
.amap-frame {
  position: relative;
  width: 100%;
  height: 68vh;
  min-height: 420px;
}
.amap-host {
  width: 100%;
  height: 100%;
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
.amap-note.is-error {
  color: var(--aeg-color-status-danger);
}
.amap-hint {
  max-width: 62ch;
  font-size: var(--aeg-font-xs);
  color: var(--aeg-color-text-muted);
}
</style>
