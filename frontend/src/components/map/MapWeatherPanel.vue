<script setup lang="ts">
import { AimOutlined, CloseOutlined, CloudOutlined, LeftOutlined, PauseOutlined, PlayCircleOutlined, ReloadOutlined, RightOutlined, ThunderboltOutlined } from '@ant-design/icons-vue'
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import mapApi from '@/api/map'
import { weatherColors } from '@/theme/tokens'
import type { BBox } from './entities'
import { GYIRONG_CASE } from './gyirongCase'
import { createRainTexture } from './rainTexture'
import {
  DAILY_RAIN_BANDS, EMPTY_WEATHER_LAYER, HOUR_RAIN_BANDS,
  gridBounds, plotRainCells, rainCellCenter, rainColor, rainText, weatherCondition,
  type CurrentMapWeather, type RainArchive, type RainCatalog, type WeatherMapLayer, type WeatherMode,
} from './weather'

const props = defineProps<{ open: boolean; mode: WeatherMode; picked: string | null; compact: boolean; subdued?: boolean }>()
const emit = defineEmits<{
  close: []
  layer: [WeatherMapLayer]
  summary: [string]
  fit: [BBox]
}>()

const live = shallowRef<CurrentMapWeather | null>(null)
const liveBusy = ref(false)
const catalog = shallowRef<RainCatalog | null>(null)
const archive = shallowRef<RainArchive | null>(null)
const year = ref(0)
const frameIndex = ref(0)
const archiveBusy = ref(false)
const archiveError = ref('')
const pointId = ref(GYIRONG_CASE.sites[0].id)
const playing = ref(false)
let archiveRequest = 0
let refreshTimer: number | undefined
let playbackTimer: number | undefined
let disposed = false

const tz = 'Asia/Shanghai'
const clockText = new Intl.DateTimeFormat('zh-CN', { timeZone: tz, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })
const timeText = new Intl.DateTimeFormat('zh-CN', { timeZone: tz, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })
const archiveTimeText = new Intl.DateTimeFormat('zh-CN', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })
const peakIndex = computed(() => (archive.value?.frames ?? []).reduce((best, item, index, frames) =>
  (item.peak ?? -1) > (frames[best]?.peak ?? -1) ? index : best, 0))
const frame = computed(() => archive.value?.frames[frameIndex.value])
const frameCount = computed(() => archive.value?.frames.length ?? 0)
const bands = computed(() => props.mode === 'daily' ? DAILY_RAIN_BANDS : HOUR_RAIN_BANDS)
const sourceUrl = computed(() => props.mode === 'daily'
  ? 'https://essd.copernicus.org/articles/17/3987/2025/essd-17-3987-2025.html'
  : props.mode === 'hourly' ? 'https://open-meteo.com/en/docs/historical-weather-api' : 'https://open-meteo.com/en/docs')
const selectedWeather = computed(() => live.value?.points.find((point) => point.id === pointId.value))
const condition = computed(() => weatherCondition(selectedWeather.value?.weather_code ?? null, selectedWeather.value?.is_day))
const weatherStatus = computed(() => ({ available: '模式天气', stale: '上次缓存', unavailable: '暂不可用', disabled: '已停用' })[live.value?.status ?? 'unavailable'])
const selectedTemperature = computed(() => selectedWeather.value?.temperature_c == null ? '—' : Math.round(selectedWeather.value.temperature_c))
const forecast = computed(() => selectedWeather.value?.forecast ?? [])
const forecastMax = computed(() => Math.max(1, ...forecast.value.map((hour) => hour.rain_mm ?? 0)))
const availableValues = computed(() => frame.value?.values.filter((value) => value !== null).length ?? 0)
const hourlyRows = computed(() => (archive.value?.points ?? []).map((point, index) => ({
  ...point, value: frame.value?.values[index] ?? null,
})))
const highlightedCell = computed(() => layer.value.cells.find((cell) => cell.id === props.picked))
const highlightedCenter = computed(() => highlightedCell.value ? rainCellCenter(highlightedCell.value) : null)
const timestamp = computed(() => !frame.value ? '' : props.mode === 'daily' ? `${frame.value.end} 20:00` : archiveTimeText.format(new Date(frame.value.end)))
const chart = computed(() => {
  const frames = archive.value?.frames ?? []
  const count = props.compact ? 5 : 7
  const start = Math.max(0, Math.min(frames.length - count, frameIndex.value - Math.floor(count / 2)))
  const visibleFrames = frames.slice(start, start + count)
  const highest = Math.max(1, ...visibleFrames.map((item) => item.peak ?? 0))
  return visibleFrames.map((item, index) => ({
    index: start + index,
    date: props.mode === 'daily' ? item.end.slice(5) : clockText.format(new Date(item.end)),
    peak: item.peak,
    height: item.peak === null ? '0%' : `${Math.max(4, 100 * item.peak / highest)}%`,
    color: item.peak === null ? weatherColors['weather-missing'] : rainColor(item.peak, props.mode === 'daily' ? 'daily' : 'hourly'),
  }))
})

const rain = computed(() => props.mode === 'daily' && archive.value?.grid && frame.value
  ? createRainTexture(archive.value.grid, frame.value) : null)

const layer = computed<WeatherMapLayer>(() => {
  if (props.mode === 'current') {
    if (live.value?.status !== 'available') return EMPTY_WEATHER_LAYER
    return { cells: [], points: GYIRONG_CASE.sites.flatMap((site) => {
      const point = live.value?.points.find((item) => item.id === site.id)
      if (!point) return []
      const { label, icon } = weatherCondition(point.weather_code, point.is_day)
      const temperature = point.temperature_c === null ? '' : ` · ${Math.round(point.temperature_c)}°`
      return [{ id: `weather-${site.id}`, title: site.label, label: `${label}${temperature}`,
        lonlat: site.lonlat, mainlandChina: site.mainlandChina, color: icon === 'sun' ? weatherColors['weather-sun-core'] : weatherColors['rain-low'] }]
    }) }
  }
  if (!frame.value || !archive.value) return EMPTY_WEATHER_LAYER
  if (props.mode === 'daily') {
    const port = GYIRONG_CASE.sites[0]
    return { points: [{ id: 'weather-gyirong-grid-reference', title: port.label, label: '', lonlat: port.lonlat,
      mainlandChina: port.mainlandChina, color: weatherColors['weather-reference'] }],
      cells: archive.value.grid ? plotRainCells(archive.value.grid, frame.value) : [], rain: rain.value }
  }
  return { cells: [], points: hourlyRows.value.map(({ id, name, lon, lat, value }) => ({
    id: `weather-${id}`, title: name, lonlat: { lon, lat }, mainlandChina: true,
    label: value === null ? '缺测' : `${rainText(value)} mm`, color: value === null ? weatherColors['weather-missing'] : rainColor(value, 'hourly'),
  })) }
})

async function refreshLive(): Promise<void> {
  if (liveBusy.value) return
  liveBusy.value = true
  try {
    live.value = await mapApi.currentWeather()
    if (!navigator.onLine) onOffline()
  } catch {
    live.value = live.value?.points.length ? { ...live.value, status: 'stale' } : { status: 'unavailable', fetched_at: null, points: [] }
  } finally {
    liveBusy.value = false
  }
}

function fitArchive(): void {
  if (archive.value?.grid) emit('fit', gridBounds(archive.value.grid))
}

async function loadArchive(chosenYear?: number): Promise<void> {
  const mode = props.mode
  if (mode === 'current') return
  const sequence = ++archiveRequest
  stopPlayback()
  archive.value = null
  archiveError.value = ''
  archiveBusy.value = true
  try {
    catalog.value ??= await mapApi.rainCatalog()
    if (sequence !== archiveRequest) return
    if (mode === 'daily') {
      year.value = chosenYear ?? catalog.value.daily_years[0] ?? 0
      if (!year.value) { archiveError.value = '当前没有可用的吉隆日雨量网格。'; return }
    } else if (!catalog.value.hourly_available) {
      archiveError.value = '当前没有可用的县域小时数据。'
      return
    }
    const data = mode === 'daily' ? await mapApi.rainDaily(year.value) : await mapApi.rainHourly()
    if (sequence !== archiveRequest) return
    if (!data.frames.length) { archiveError.value = '这份数据没有可回放的时刻。'; return }
    archive.value = data
    frameIndex.value = peakIndex.value
    if (chosenYear === undefined && mode === 'daily') fitArchive()
  } catch {
    if (sequence === archiveRequest) archiveError.value = '降水数据读取失败，请重试。'
  } finally {
    if (sequence === archiveRequest) archiveBusy.value = false
  }
}

function stopPlayback(): void {
  window.clearInterval(playbackTimer)
  playing.value = false
}

function seek(index: number): void {
  frameIndex.value = Math.max(0, Math.min(index, frameCount.value - 1))
}

function togglePlayback(): void {
  if (playing.value) { stopPlayback(); return }
  if (frameCount.value < 2) return
  if (frameIndex.value === frameCount.value - 1) seek(0)
  playing.value = true
  playbackTimer = window.setInterval(() => {
    seek(frameIndex.value + 1)
    if (frameIndex.value >= frameCount.value - 1) stopPlayback()
  }, 620)
}

watch(layer, (value) => emit('layer', value), { immediate: true })
watch([live, liveBusy], () => {
  const home = live.value?.points.find((point) => point.id === GYIRONG_CASE.sites[0].id)
  emit('summary', live.value?.status === 'available' && home
    ? `${weatherCondition(home.weather_code).label}${home.temperature_c === null ? '' : ` ${Math.round(home.temperature_c)}°`}`
    : '')
})
watch(() => props.mode, (mode) => {
  archiveRequest++
  archive.value = null
  stopPlayback()
  if (mode === 'current') { archiveBusy.value = false; return }
  void loadArchive()
})
watch(() => props.open, (open) => { if (!open) stopPlayback() })
watch(() => props.picked, (picked) => {
  const site = GYIRONG_CASE.sites.find((item) => `weather-${item.id}` === picked)
  if (site) pointId.value = site.id
})

function onRefresh(): void {
  if (disposed || document.visibilityState === 'hidden' || !navigator.onLine || props.mode !== 'current') return
  void refreshLive()
}

function onOffline(): void {
  if (live.value?.status === 'available') live.value = { ...live.value, status: 'stale' }
}

onMounted(() => {
  void refreshLive()
  refreshTimer = window.setInterval(onRefresh, 120_000)
  window.addEventListener('online', onRefresh)
  window.addEventListener('offline', onOffline)
  document.addEventListener('visibilitychange', onRefresh)
})
onBeforeUnmount(() => {
  disposed = true
  archiveRequest++
  stopPlayback()
  window.clearInterval(refreshTimer)
  window.removeEventListener('online', onRefresh)
  window.removeEventListener('offline', onOffline)
  document.removeEventListener('visibilitychange', onRefresh)
})
</script>

<template>
  <aside v-show="open" id="map-weather" class="weather-panel" aria-labelledby="map-weather-title">
    <header class="weather-panel__header">
      <span class="weather-panel__emblem"><CloudOutlined aria-hidden="true" /></span>
      <div><h2 id="map-weather-title">{{ mode === 'daily' ? '吉隆日雨量回放' : mode === 'hourly' ? '县域小时雨量' : '当前天气' }}</h2><p>{{ mode === 'hourly' ? '西藏县域代表点' : mode === 'daily' ? '历史回放' : '吉隆口岸与上游' }}</p></div>
      <button type="button" class="weather-icon-button" aria-label="关闭天气与雨情" @click="emit('close')"><CloseOutlined /></button>
    </header>

    <template v-if="mode === 'current'">
      <div class="weather-sites" role="group" aria-label="天气参考点">
        <button v-for="site in GYIRONG_CASE.sites" :key="site.id" type="button" :aria-pressed="pointId === site.id" @click="pointId = site.id">
          {{ site.id === GYIRONG_CASE.sites[0].id ? '吉隆口岸' : '上游参考点' }}
        </button>
      </div>
      <div v-if="selectedWeather" class="weather-current">
        <div class="weather-current__main">
          <span class="weather-symbol" :data-icon="condition.icon" aria-hidden="true">
            <ThunderboltOutlined v-if="condition.icon === 'storm'" />
            <CloudOutlined v-else-if="condition.icon !== 'sun' && condition.icon !== 'moon' && condition.icon !== 'snow'" />
            <template v-else-if="condition.icon === 'snow'">❄</template>
          </span>
          <strong>{{ selectedTemperature }}<small v-if="selectedTemperature !== '—'">°C</small></strong>
          <div><span class="weather-condition">{{ condition.label }}</span><time :datetime="selectedWeather.valid_at">{{ timeText.format(new Date(selectedWeather.valid_at)) }}</time><span v-if="live?.status === 'stale'" class="weather-status-stale">上次缓存</span></div>
        </div>
        <dl class="weather-metrics">
          <div><dt>前 {{ selectedWeather.interval_minutes }} 分钟降雨</dt><dd>{{ rainText(selectedWeather.rain_mm) }} <span>mm</span></dd></div>
          <div><dt>10 米风速</dt><dd>{{ rainText(selectedWeather.wind_kmh) }} <span>km/h</span></dd></div>
        </dl>
      </div>
      <p v-else class="weather-empty" role="status">{{ liveBusy ? '正在获取当前天气…' : live?.status === 'disabled' ? '当前天气来源已停用。' : '此位置暂时没有当前天气。' }}</p>
      <div v-if="forecast.length && live?.status === 'available'" class="weather-forecast">
        <h3>接下来 6 小时 <span>降雨量 · mm</span></h3>
        <div class="weather-forecast__hours" :style="{ '--weather-hours': forecast.length }">
          <div v-for="hour in forecast" :key="hour.end" :title="`${timeText.format(new Date(hour.end))} 前一小时 · ${rainText(hour.rain_mm)} mm`">
            <strong>{{ rainText(hour.rain_mm) }}</strong>
            <span class="weather-forecast__track"><i :style="{ height: `${hour.rain_mm === null ? 0 : Math.max(8, 100 * hour.rain_mm / forecastMax)}%` }" /></span>
            <time :datetime="hour.end">{{ clockText.format(new Date(hour.end)) }}</time>
          </div>
        </div>
      </div>
      <footer class="weather-footer">
        <div><a :href="sourceUrl" target="_blank" rel="noopener noreferrer">Open-Meteo ↗</a><span> · {{ live?.status === 'stale' ? '模式天气' : weatherStatus }} · 北京时间</span></div>
        <button type="button" class="weather-icon-button" aria-label="刷新天气" :disabled="liveBusy" @click="refreshLive"><ReloadOutlined /></button>
      </footer>
    </template>

    <template v-else>
      <div class="rain-toolbar">
        <label v-if="mode === 'daily' && catalog?.daily_years.length">年份<select :value="year" @change="loadArchive(Number(($event.target as HTMLSelectElement).value))"><option v-for="item in catalog.daily_years" :key="item" :value="item">{{ item }}</option></select></label>
        <span v-else class="rain-toolbar__tag">历史回放</span>
        <button v-if="mode === 'daily' && archive" type="button" class="weather-chip" @click="fitArchive"><AimOutlined /> 定位雨区</button>
      </div>
      <p v-if="archiveBusy" class="weather-empty" role="status">正在读取历史雨量…</p>
      <div v-else-if="archiveError" class="weather-empty" role="status">
        <p>{{ archiveError }}</p><button type="button" class="weather-chip" @click="catalog = null; loadArchive()">重新读取</button>
      </div>
      <template v-else-if="frame && archive">
        <div class="rain-reading" aria-live="off">
          <div><time :datetime="mode === 'daily' ? `${frame.end}T20:00:00+08:00` : frame.end">{{ timestamp }}</time><span>{{ mode === 'daily' ? '24 小时累计 · 北京时间' : '此前 1 小时 · 北京时间' }}</span></div>
          <div><strong>{{ highlightedCell?.value === null ? '缺测' : rainText(highlightedCell ? highlightedCell.value : frame.peak) }}</strong><small v-if="highlightedCell?.value !== null">mm</small><span>{{ highlightedCell ? '选中格点' : '当帧最大值' }}</span></div>
        </div>
        <p v-if="highlightedCenter" class="rain-location">{{ highlightedCenter.lat.toFixed(2) }}°N · {{ highlightedCenter.lon.toFixed(2) }}°E</p>
        <Teleport to="#map-rain-replay" :disabled="compact">
          <section v-show="open || !compact" class="rain-timeline" :class="{ 'is-docked': !compact }" aria-label="历史雨量回放" data-testid="rain-replay">
            <div class="rain-timeline__actions">
              <button type="button" class="weather-icon-button" aria-label="前一帧" :disabled="frameIndex === 0" @click="stopPlayback(); seek(frameIndex - 1)"><LeftOutlined /></button>
              <button type="button" class="weather-chip weather-chip--play" :aria-label="playing ? '暂停回放' : '播放回放'" :disabled="frameCount < 2" @click="togglePlayback"><PauseOutlined v-if="playing" /><PlayCircleOutlined v-else />{{ playing ? '暂停' : '播放' }}</button>
              <button type="button" class="weather-icon-button" aria-label="后一帧" :disabled="frameIndex === frameCount - 1" @click="stopPlayback(); seek(frameIndex + 1)"><RightOutlined /></button>
              <button type="button" class="weather-chip rain-timeline__peak" @click="stopPlayback(); seek(peakIndex)">跳到峰值</button>
            </div>
            <div class="rain-timeline__series">
              <span class="rain-timeline__caption">{{ mode === 'daily' ? '全区格点峰值' : '代表点峰值' }} · mm</span>
              <div class="rain-timeline__chart">
                <button v-for="bar in chart" :key="bar.index" type="button" :aria-pressed="bar.index === frameIndex"
                  :aria-label="`${bar.date} · ${bar.peak === null ? '缺测' : `${rainText(bar.peak)} mm`}`" @click="stopPlayback(); seek(bar.index)">
                  <span v-if="!compact" class="rain-timeline__peak-value">{{ bar.peak === null ? '缺测' : rainText(bar.peak) }}</span>
                  <span class="rain-timeline__track"><i :style="{ height: bar.height, background: bar.color }" /></span>
                  <span>{{ bar.date }}</span>
                </button>
              </div>
              <input type="range" min="0" :max="frameCount - 1" :value="frameIndex" aria-label="历史雨量时间轴" :aria-valuetext="timestamp" @input="stopPlayback(); seek(Number(($event.target as HTMLInputElement).value))" />
            </div>
          </section>
        </Teleport>
        <div class="rain-legend" aria-label="降水色标，单位毫米">
          <span v-for="band in bands" :key="band.from"><i :style="{ background: band.color }" />{{ band.label }}</span><span>mm</span>
        </div>
        <ul v-if="mode === 'hourly'" class="rain-points">
          <li v-for="point in hourlyRows" :key="point.id" :class="{ 'is-picked': picked === `weather-${point.id}` }"><span>{{ point.name }}</span><strong>{{ point.value === null ? '缺测' : `${rainText(point.value)} mm` }}</strong></li>
        </ul>
        <footer class="weather-footer weather-footer--archive">
          <div><a :href="sourceUrl" target="_blank" rel="noopener noreferrer">{{ archive.source }} ↗</a><span> · {{ mode === 'daily' ? '0.1° 原始格点' : '网格代表点 · 含固态降水水当量' }}</span></div>
          <span v-if="mode === 'daily'">{{ availableValues }}/{{ frame.values.length }} 格有值 · {{ subdued ? '近景淡显' : '平滑显示，缺测留空' }}</span>
          <span v-else>{{ availableValues }}/{{ frame.values.length }} 点有值 · 代表点采样</span>
        </footer>
      </template>
    </template>
  </aside>
</template>

<style scoped>
:is(.weather-panel, .rain-timeline) {
  --rain-accent: var(--aeg-color-status-info);
}
.weather-panel {
  box-sizing: border-box;
  padding: 16px;
  overflow: auto;
  overscroll-behavior: contain;
  scrollbar-width: thin;
  color: var(--aeg-color-map-panel-text);
  background: var(--aeg-color-map-panel-bg);
  border: 1px solid var(--aeg-color-map-panel-border);
  border-radius: 23px;
  box-shadow: var(--aeg-shadow-lg);
  animation: weather-enter 180ms ease-out;
}
@keyframes weather-enter { from { opacity: 0; transform: translateY(-5px); } }
@media (prefers-reduced-motion: reduce) { .weather-panel { animation: none; } }
.weather-panel__header { display: grid; grid-template-columns: 40px 1fr auto; gap: 10px; align-items: center; margin-bottom: 13px; }
.weather-panel__emblem { display: grid; place-items: center; width: 40px; height: 40px; border-radius: 13px; background: color-mix(in srgb, var(--rain-accent) 17%, transparent); color: var(--rain-accent); font-size: 20px; }
.weather-panel h2, .weather-panel h3, .weather-panel p, .weather-panel dl, .weather-panel dd { margin: 0; }
.weather-panel h2 { font-size: 17px; letter-spacing: 0.02em; }
.weather-panel__header p, .weather-footer, .weather-current time, .weather-metrics dt, .rain-reading span, .weather-forecast time { color: var(--aeg-color-map-panel-text-muted); font-size: 11px; }
.weather-panel button, .weather-panel select, .rain-timeline button { font: inherit; cursor: pointer; }
.weather-panel button:disabled, .rain-timeline button:disabled { opacity: .4; cursor: default; }
:is(.weather-panel, .rain-timeline) :is(button, select, input, a):focus-visible { outline: 2px solid var(--rain-accent); outline-offset: 2px; }
.weather-icon-button, .weather-chip { display: inline-flex; align-items: center; justify-content: center; gap: 5px; min-height: 32px; color: inherit; background: color-mix(in srgb, var(--rain-accent) 7%, transparent); border: 1px solid var(--aeg-color-map-panel-border); border-radius: 10px; }
.weather-icon-button { width: 32px; padding: 0; flex: 0 0 32px; }
.weather-chip { padding: 0 9px; }
.weather-panel button:hover:not(:disabled) { border-color: var(--rain-accent); }
.weather-sites button { padding: 6px 3px; min-height: 32px; color: var(--aeg-color-map-panel-text-muted); white-space: nowrap; background: transparent; border: 1px solid transparent; border-radius: 9px; font-size: 12px; }
.weather-sites button[aria-pressed='true'] { color: var(--aeg-color-map-panel-text); background: color-mix(in srgb, var(--rain-accent) 18%, transparent); border-color: color-mix(in srgb, var(--rain-accent) 44%, transparent); }
.weather-sites { display: flex; gap: 7px; margin: 11px 0; }
.weather-sites button { min-height: 29px; padding: 3px 9px; border-color: var(--aeg-color-map-panel-border); }
.weather-current__main { display: flex; gap: 11px; align-items: center; min-height: 67px; }
.weather-current__main > strong { font-size: 44px; font-weight: 570; line-height: 1; letter-spacing: -0.07em; font-variant-numeric: tabular-nums; }
.weather-current__main small { font-size: 17px; margin-inline-start: 4px; letter-spacing: normal; }
.weather-current__main > div { display: grid; gap: 4px; margin-inline-start: auto; text-align: end; }
.weather-condition { font-size: 15px; font-weight: 650; }
.weather-symbol { position: relative; display: grid; place-items: center; width: 51px; height: 51px; color: var(--rain-accent); font-size: 34px; }
.weather-symbol[data-icon='sun']::before { width: 30px; height: 30px; content: ''; border-radius: 50%; background: radial-gradient(circle at 38% 32%, var(--aeg-color-weather-sun-core), var(--aeg-color-weather-sun-edge) 78%); box-shadow: 0 0 22px color-mix(in srgb, var(--aeg-color-weather-sun-edge) 40%, transparent), 0 0 0 6px color-mix(in srgb, var(--aeg-color-weather-sun-edge) 14%, transparent); }
.weather-symbol[data-icon='moon']::before { width: 28px; height: 28px; margin: -7px 0 0 -6px; content: ''; border-radius: 50%; box-shadow: 7px 7px 0 0 var(--aeg-color-weather-moon), 9px 9px 16px color-mix(in srgb, var(--aeg-color-weather-moon) 35%, transparent); }
.weather-status-stale { color: var(--aeg-tag-orange-text); font-size: 11px; }
.weather-symbol[data-icon='rain']::after { position: absolute; inset: auto auto 3px 20px; width: 2px; height: 7px; background: currentColor; border-radius: 2px; transform: rotate(24deg); box-shadow: 6px 0 currentColor, 12px 0 currentColor; content: ''; }
.weather-panel .weather-metrics { display: grid; grid-template-columns: 1.1fr .9fr; gap: 7px; margin-top: 5px; }
.weather-metrics > div { padding: 8px 9px; border-radius: 11px; background: color-mix(in srgb, var(--rain-accent) 7%, transparent); }
.weather-metrics dd { font-size: 18px; font-weight: 640; font-variant-numeric: tabular-nums; }
.weather-metrics dd span, .rain-reading small { color: var(--aeg-color-map-panel-text-muted); font-size: 11px; font-weight: 500; }
.weather-forecast { margin-top: 13px; }
.weather-forecast h3 { display: flex; align-items: center; justify-content: space-between; font-size: 12px; }
.weather-forecast h3 span { color: var(--aeg-color-map-panel-text-muted); font-size: 10px; font-weight: 500; }
.weather-forecast__hours { display: grid; grid-template-columns: repeat(var(--weather-hours), minmax(0, 1fr)); gap: 4px; margin-top: 7px; }
.weather-forecast__hours > div { display: grid; justify-items: center; gap: 3px; min-width: 0; font-size: 11px; }
.weather-forecast__track { display: flex; align-items: end; height: 25px; width: 11px; border-radius: 5px; background: color-mix(in srgb, var(--rain-accent) 10%, transparent); }
.weather-forecast__track i { width: 100%; border-radius: inherit; background: var(--rain-accent); }
.weather-footer { display: flex; align-items: center; justify-content: space-between; gap: 7px; margin-top: 11px; padding-top: 9px; border-top: 1px solid var(--aeg-color-map-panel-border); }
.weather-footer a { color: var(--rain-accent); text-decoration: underline; text-underline-offset: 3px; }
.weather-footer--archive { display: grid; gap: 3px; }
.weather-panel .weather-empty { margin-top: 10px; min-height: 61px; display: grid; align-content: center; justify-items: start; gap: 6px; font-size: 12px; color: var(--aeg-color-map-panel-text-muted); }
.rain-toolbar { margin-top: 10px; display: flex; align-items: center; justify-content: space-between; gap: 6px; font-size: 12px; }
.rain-toolbar__tag { color: var(--rain-accent); }
.rain-toolbar label { display: inline-flex; align-items: center; gap: 6px; color: var(--aeg-color-map-panel-text-muted); }
.rain-toolbar select { padding: 5px 7px; color: var(--aeg-color-map-panel-text); background: var(--aeg-color-map-floatbar-bg); border: 1px solid var(--aeg-color-map-panel-border); border-radius: 8px; }
.rain-reading { display: flex; justify-content: space-between; gap: 7px; align-items: center; margin-top: 9px; }
.rain-reading > div { display: block; min-width: 0; }
.rain-reading > div:last-child { text-align: right; }
.rain-reading time { font-size: 17px; font-weight: 650; font-variant-numeric: tabular-nums; }
.rain-reading strong { margin-inline-end: 4px; color: var(--rain-accent); font-size: 29px; letter-spacing: -0.05em; font-variant-numeric: tabular-nums; }
.rain-reading span { display: block; }
.weather-panel .rain-location { margin-top: var(--aeg-space-s1); color: var(--aeg-color-map-panel-text-muted); font-size: var(--aeg-font-sm); }
.rain-timeline { margin-top: 10px; }
.rain-timeline.is-docked {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: var(--aeg-space-s4);
  align-items: center;
  margin: 0;
  padding: var(--aeg-space-s2) var(--aeg-space-s3);
  border-radius: var(--aeg-radius-lg);
  border: 1px solid var(--aeg-color-map-panel-border);
  background: var(--aeg-color-map-panel-bg);
  color: var(--aeg-color-map-panel-text);
  box-shadow: var(--aeg-shadow-md);
}
.rain-timeline__caption { color: var(--aeg-color-map-panel-text-muted); font-size: var(--aeg-font-xs); }
.rain-timeline__chart { display: flex; gap: var(--aeg-space-s1); margin-top: 2px; }
.rain-timeline__chart button {
  flex: 1 1 0;
  min-width: 0;
  display: grid;
  justify-items: center;
  padding: 2px;
  border: 1px solid transparent;
  border-radius: var(--aeg-radius-sm);
  background: transparent;
  color: var(--aeg-color-map-panel-text-muted);
  font-size: var(--aeg-font-xs);
}
.rain-timeline__chart button[aria-pressed='true'] {
  border-color: var(--aeg-color-map-panel-border);
  background: color-mix(in srgb, var(--rain-accent) 13%, transparent);
  color: var(--aeg-color-map-panel-text);
}
.rain-timeline__track { display: flex; align-items: end; justify-content: center; width: 100%; height: 26px; }
.rain-timeline__track i { width: min(54%, 46px); border-radius: 3px 3px 0 0; opacity: 0.66; }
.rain-timeline__chart button[aria-pressed='true'] i { opacity: 1; }
.rain-timeline__peak-value { font-variant-numeric: tabular-nums; }
.rain-timeline input { display: block; width: 100%; height: 23px; margin: 0; accent-color: var(--rain-accent); cursor: ew-resize; }
.rain-timeline__actions { display: flex; gap: 5px; font-size: 12px; }
.rain-timeline__peak { margin-inline-start: auto; }
.weather-chip--play { background: color-mix(in srgb, var(--rain-accent) 19%, transparent); }
.rain-legend { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 4px; margin-top: 11px; font-size: 10px; color: var(--aeg-color-map-panel-text-muted); }
.rain-legend span { display: inline-flex; align-items: center; gap: 4px; }
.rain-legend i { width: 9px; height: 9px; border-radius: 3px; }
.rain-points { margin: 8px 0 0; padding: 0; list-style: none; }
.rain-points li { display: flex; justify-content: space-between; gap: 8px; padding: 3px 5px; font-size: 12px; border-radius: 6px; }
.rain-points li.is-picked { background: color-mix(in srgb, var(--rain-accent) 17%, transparent); }
@media (max-width: 819px) {
  .weather-panel { padding: 12px 14px; }
  .weather-panel__header { margin-bottom: 8px; }
  .weather-sites { margin: 7px 0; }
  .weather-current__main { min-height: 52px; }
  .weather-metrics > div { padding-block: 6px; }
  .weather-forecast { margin-top: 9px; }
  .weather-footer { margin-top: 7px; padding-top: 6px; }
  .rain-toolbar, .rain-timeline { margin-top: 7px; }
  .rain-timeline__track { height: 17px; }
  .rain-legend { margin-top: 8px; }
}
</style>
