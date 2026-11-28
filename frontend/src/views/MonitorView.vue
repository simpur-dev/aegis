<script setup lang="ts">
import { AreaChartOutlined, BarChartOutlined, DatabaseOutlined, ExperimentOutlined, ReloadOutlined, SlidersOutlined } from '@ant-design/icons-vue'
import { useMediaQuery } from '@vueuse/core'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import api, { ApiError, type TelemetryQuery } from '@/api/client'
import type { TelemetryReading } from '@/api/types'
import mapApi, { normalizeAnchors } from '@/api/map'
import EChart from '@/components/EChart.vue'
import PageHero from '@/components/PageHero.vue'
import DataWorkspaceHeader from '@/components/DataWorkspaceHeader.vue'
import FreshnessBar from '@/components/FreshnessBar.vue'
import ReportForm from '@/components/reports/ReportForm.vue'
import ServiceTokenBar from '@/components/ServiceTokenBar.vue'
import SystemTerminal from '@/components/SystemTerminal.vue'
import type { ChartOption } from '@/components/echarts'
import { echarts } from '@/components/echarts'
import { colorSlots } from '@/theme/tokens'
import { useTheme } from '@/theme/useTheme'
import { formatOperatingTime, operatingClock } from '@/utils/clock'
import { ingestLatencyLabel } from '@/utils/ingestLatency'

const REFRESH_MS = 15_000
const WINDOW_LIMIT = 2_000
const PAGE_SIZE = 10
const compact = useMediaQuery('(max-width: 720px)')
const route = useRoute()
const drillView = computed(() => route?.hash === '#drill')

const readings = ref<TelemetryReading[]>([])
const region = ref<string>('')
const metric = ref<string>('rain_10min')
const loading = ref(false)
const loadError = ref('')
/** 最近一次**成功**取遥测的时刻（UTC ISO）；null = 还没取到过。 */
const fetchedAt = ref<string | null>(null)
const drilling = ref(false)
const drillResult = ref<{ name: string; readings: number; regions: number; assessed: number; warnings: number } | null>(null)
const drillError = ref('')
const drillReceipt = ref<HTMLElement | null>(null)
let timer: ReturnType<typeof setInterval> | null = null
/**
 * 下拉可选项的底仓：区域来自烘焙好的区域锚点清单（那是全量区域），指标来自一次不带筛选的取样。
 *
 * 为什么不能从"当前这批读数"里推：见 `load()` 的注释——窗口只有最新 2000 条，
 * 用它当清单等于"只列出窗口里恰好出现过的区域"，其余区域在页面上直接不存在。
 */
const catalog = ref<{ regions: string[]; metrics: string[] }>({ regions: [], metrics: [] })

const regions = computed(() => sortedUnion(catalog.value.regions, readings.value.map((r) => r.region_code), [region.value]))
const metrics = computed(() => sortedUnion(catalog.value.metrics, readings.value.map((r) => r.metric), [metric.value]))

function sortedUnion(...groups: string[][]): string[] {
  return [...new Set(groups.flat().filter((item) => item !== ''))].sort()
}
/**
 * 人工上报（批次 B5）：这条腿是"接入 ≤5min"的第四个真实入口，
 * 表单与回执都在 `components/reports/ReportForm.vue` 一处，监测页只负责开门。
 */
const reportOpen = ref(false)

/** 图卡配色跟着主题换：canvas 读不到 CSS 变量，取色必须从 theme/chart.ts 派生（见那边的说明）。 */
const { resolved: themeMode } = useTheme()
const chartSource = ref('')
const chartSources = computed(() => {
  const sources = new Map<string, { id: string; label: string; readings: TelemetryReading[] }>()
  for (const reading of readings.value) {
    const id = JSON.stringify([reading.region_code, reading.station_id, reading.unit])
    let source = sources.get(id)
    if (!source) {
      source = { id, label: `${reading.station_id} · ${reading.region_code} · ${reading.unit}`, readings: [] }
      sources.set(id, source)
    }
    source.readings.push(reading)
  }
  return [...sources.values()]
})
const activeSource = computed(() => chartSources.value.find((source) => source.id === chartSource.value) ?? chartSources.value[0])
const chartReadings = computed(() => activeSource.value?.readings ?? [])
const qualityLabels: Record<TelemetryReading['quality_flag'], string> = {
  ok: '有效', suspect: '可疑', missing: '缺测', drift: '漂移',
}
const metricLabels: Record<string, string> = { rain_10min: '10 分钟降雨', debris_level: '沟道泥位' }
const rowKey = (row: TelemetryReading) => JSON.stringify([row.station_id, row.metric, row.observed_at, row.ingested_at])

/**
 * 最新一条**有效**读数：画布上劣化读数本来就是断点，右上角这块只报能用的数；
 * 没有就写 `—`，不拿 0 或上一条 suspect 冒充。
 */
const latestOk = computed<TelemetryReading | null>(() => {
  let best: TelemetryReading | null = null
  for (const reading of chartReadings.value) {
    if (reading.quality_flag !== 'ok') continue
    if (!best || reading.observed_at > best.observed_at) best = reading
  }
  return best
})

/** 只连接同一站点、区域及计量单位的读数。时间轴保留真实观测间隔。 */
const chartOption = computed<ChartOption>(() => {
  const dark = themeMode.value === 'dark'
  const colors = colorSlots[themeMode.value]
  const line = dark ? colors['chart-hero-line'] : colors.brand
  const axisText = colors['text-secondary']
  const samples = chartReadings.value
  const latest = latestOk.value
  const latestTime = latest ? Date.parse(latest.observed_at) : null
  return {
    // 标题由卡片头承担，画布里不再重复一行
    tooltip: {
      trigger: 'axis',
      axisPointer: {
        type: 'line',
        lineStyle: { color: line, type: 'dashed', width: 1 },
        label: { formatter: ({ value }: { value: number }) => formatOperatingTime(new Date(value).toISOString()) },
      },
    },
    grid: { left: 47, right: 16, top: 12, bottom: 35 },
    xAxis: {
      type: 'time',
      axisLine: { lineStyle: { color: colors['border-strong'] } },
      axisTick: { show: false },
      axisLabel: {
        color: axisText,
        fontSize: compact.value ? 10 : 12,
        hideOverlap: true,
        formatter: (value: number) => operatingClock(new Date(value).toISOString()),
      },
    },
    yAxis: {
      type: 'value',
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: axisText, fontSize: 12 },
      splitLine: { lineStyle: { color: dark ? colors['chart-hero-grid'] : colors['border-subtle'], type: 'dashed' } },
    },
    series: [
      {
        name: metricLabels[metric.value] ?? metric.value,
        type: 'line',
        smooth: true,
        showSymbol: samples.length < 60,
        // 窗口 2000 条压进 400px 宽会糊成一条白带：按像素做 LTTB 抽样，形状还在、线看得清
        sampling: 'lttb',
        symbolSize: 4,
        itemStyle: { color: line },
        lineStyle: { color: line, width: 1.55, shadowBlur: dark ? 5 : 0, shadowColor: colors['overview-accent'] },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: dark ? echarts.color.modifyAlpha(colors['overview-accent'], .56) : colors['brand-soft'] },
            { offset: 1, color: dark ? echarts.color.modifyAlpha(colors['overview-accent'], .1) : 'transparent' },
          ]),
        },
        data: samples.map((r) => [Date.parse(r.observed_at), r.quality_flag === 'ok' ? r.value : null] as const),
        connectNulls: false,
        markLine: latestTime === null ? undefined : {
          silent: true, symbol: 'none', label: { show: false },
          lineStyle: { type: 'dashed', color: line, width: 1.25, opacity: .72 },
          data: [{ xAxis: latestTime }],
        },
        markPoint: latestTime === null || !latest ? undefined : {
          silent: true, symbol: 'circle', symbolSize: 9, label: { show: false },
          itemStyle: { color: line, borderColor: colors['overview-accent'], borderWidth: 1 },
          data: [{ coord: [latestTime, latest.value] }],
        },
      },
    ],
  }
})

const columns = [
  { title: '站点', dataIndex: 'station_id', key: 'station_id', width: '14%' },
  { title: '区域', dataIndex: 'region_code', key: 'region_code', width: '9%' },
  { title: '指标', dataIndex: 'metric', key: 'metric', width: '12%' },
  { title: '数值', key: 'value', width: '12%' },
  { title: '质量', dataIndex: 'quality_flag', key: 'quality_flag', width: '8%' },
  { title: '观测时间（UTC+8，最新在上）', dataIndex: 'observed_at', key: 'observed_at', width: '25%' },
  { title: '接入时延', key: 'latency', width: '20%' },
]

/**
 * 表格单独排一份最新在上：接口给的是旧→新（500 条窗口），照原样摆进每页 10 条的台账，
 * 第一页就是最旧的 10 条，刚进来的那条要翻到第 49 页——这张页的标题是"实时监测"。
 * 时序图反过来读会错（左到右就该是旧到新），所以图表仍用 `readings` 原序。
 */
const ledgerReadings = computed<TelemetryReading[]>(() =>
  [...readings.value].sort((a, b) => (a.observed_at < b.observed_at ? 1 : a.observed_at > b.observed_at ? -1 : 0)),
)

/**
 * 取数。区域与指标**都作为查询参数发给后端**（`/api/v1/telemetry` 收 `region_code`/`metric`，
 * 一张图页一直是这么用的），不再"取最新 2000 条再本地挑"。
 *
 * 本地过滤的真机后果：台账里 50,000 条读数，窗口只装得下最新 2,000 条，
 * 而窗口里只出现过 3 个区域（`540121/540221/540321`）——其余区域的下拉项根本不存在，
 * 选不到也就看不到，页面表现成"这个区域没有遥测"。同一时刻后端按区域查能填满 2,000 条。
 */
let requestToken = 0

async function load(): Promise<void> {
  const token = ++requestToken
  loading.value = true
  try {
    const query: TelemetryQuery = { limit: WINDOW_LIMIT }
    if (region.value !== '') query.region_code = region.value
    if (metric.value !== '') query.metric = metric.value
    const data = await api.telemetry(query)
    // 迟到的旧响应不许盖掉新筛选的结果：连点两个区域时，前一个更慢回来是常态而不是意外
    if (token !== requestToken) return
    readings.value = data.items
    // 只有真取到数那一刻才算"更新于"；失败路径不动它，让"多旧"继续长大
    fetchedAt.value = new Date().toISOString()
    loadError.value = ''
  } catch (error) {
    if (token !== requestToken) return
    loadError.value = error instanceof Error ? error.message : '网络请求未完成'
  } finally {
    if (token === requestToken) loading.value = false
  }
}

/** 下拉底仓：锚点清单给全量区域，一次不筛的取样给指标名。任一失败都退回"至少还能用读数推"。 */
async function loadCatalog(): Promise<void> {
  const [anchors, sample] = await Promise.allSettled([mapApi.regionAnchors(), api.telemetry({ limit: WINDOW_LIMIT })])
  const anchorList = anchors.status === 'fulfilled' ? normalizeAnchors(anchors.value) : []
  const sampleList = sample.status === 'fulfilled' ? sample.value.items : []
  catalog.value = {
    regions: sortedUnion(anchorList.map((item) => item.code), sampleList.map((r) => r.region_code)),
    metrics: sortedUnion(sampleList.map((r) => r.metric)),
  }
}

async function drill(scenario: 'surge' | 'normal'): Promise<void> {
  // 重入守卫：`:loading` 只是让按钮转圈，antd 并不会因此禁用点击（真机实测：
  // 点击后 120ms 再点一次，第二次请求照样发出去）。一次演练会发布 14 条读数并跑完整链路，
  // 连点就是把接入量测的样本成倍灌进账本——值班员看到的"激增"就变成了自己手抖的结果。
  if (drilling.value) return
  drilling.value = true
  drillResult.value = null
  drillError.value = ''
  try {
    const result = await api.drill({ scenario, ticks: 1 })
    drillResult.value = {
      name: scenario === 'surge' ? '雨强激增' : '正常背景',
      readings: result.ingest.readings,
      regions: result.regions ?? 0,
      assessed: result.chains.filter((chain) => chain.acted).length,
      warnings: result.chains.filter((chain) => Boolean(chain.warning_id)).length,
    }
    if (!drillView.value) void load()
  } catch (error) {
    drillError.value = error instanceof ApiError ? `演练失败：${error.message}` : '演练失败，请重试。'
  } finally {
    drilling.value = false
    await nextTick()
    drillReceipt.value?.scrollIntoView?.({ block: 'nearest' })
  }
}

function latencyOf(row: TelemetryReading): string {
  return ingestLatencyLabel(row)
}

/**
 * 页码归自己管：换筛选条件就回第 1 页。
 *
 * antd 的表格在数据换一批时**不会**把 `current` 拉回来：先翻到第 200 页
 * （每个区域都有 2,000 条读数，今天真的存在第 200 页），再把指标换成一个只有
 * 几十条的，`current` 仍是 200 而新数据只有 4 页——表格里一行都没有，
 * 页面上也没有任何一句"你现在在第 200 页，这个筛选下只有 4 页"。
 * 值班员看到的就是"这个区域这个指标没数据"，而事实是筛完的页码还留在原地。
 */
const tablePage = ref(1)
const tablePagination = computed(() => ({
  current: tablePage.value, pageSize: PAGE_SIZE, showSizeChanger: false, showLessItems: true,
}))
const mobileReadings = computed(() => ledgerReadings.value.slice((tablePage.value - 1) * PAGE_SIZE, tablePage.value * PAGE_SIZE))

function onTableChange(pagination: { current?: number }): void {
  tablePage.value = pagination.current ?? 1
}

/** 筛选条件是查询参数而不是本地过滤器：改了就得重取，否则界面显示的仍是上一个区域的数。 */
watch([region, metric], () => {
  tablePage.value = 1
  readings.value = []
  chartSource.value = ''
  fetchedAt.value = null
  loadError.value = ''
  void load()
})
watch(ledgerReadings, (rows) => {
  tablePage.value = Math.min(tablePage.value, Math.max(1, Math.ceil(rows.length / PAGE_SIZE)))
})

function loadTelemetry(): void {
  void loadCatalog()
  void load()
}
watch(drillView, (active) => {
  if (!active) loadTelemetry()
})

onMounted(() => {
  if (!drillView.value) loadTelemetry()
  // 这是盯实时遥测的页面，只取一次数就等于把"最新读数"冻在打开那一刻：
  // 演练发完，表里还是旧数据，而页面标题写着"最新"。与态势总览同一口径——
  // 15 秒一轮，页签切到后台就不打接口，卸载停表。
  timer = setInterval(() => {
    if (document.visibilityState !== 'visible' || loading.value || drillView.value) return
    void load()
  }, REFRESH_MS)
})

onBeforeUnmount(() => {
  requestToken++
  if (timer !== null) clearInterval(timer)
  timer = null
})
</script>

<template>
  <div class="monitor-page" :class="{ 'data-workspace-content': !drillView }">
    <PageHero
      v-if="drillView"
      surface
      title="处置演练"
      badge="模拟场景"
      caption="选择雨情场景，查看模拟数据触发的研判与预警。"
    />
    <DataWorkspaceHeader
      v-else
      class="monitor-heading"
      title="监测数据"
      badge="实时监测"
      caption="查看站点读数与接入质量，或提交现场险情。"
    >
      <template #icon><AreaChartOutlined /></template>
      <template #actions>
        <a-button class="monitor-report" type="primary" data-testid="open-report" @click="reportOpen = true">人工上报</a-button>
      </template>
    </DataWorkspaceHeader>

    <a-card v-if="!drillView" class="monitor-card data-workspace-panel monitor-filter" size="small">
      <template #title>
        <h2 class="monitor-card__title"><SlidersOutlined aria-hidden="true" />数据筛选</h2>
      </template>
      <div class="ops-filters">
        <div class="ops-filters__field">
          <span>监测区域</span>
          <a-select v-model:value="region" show-search aria-label="监测区域">
            <a-select-option value="">全部区域</a-select-option>
            <a-select-option v-for="code in regions" :key="code" :value="code">{{ code }}</a-select-option>
          </a-select>
        </div>
        <div class="ops-filters__field">
          <span>监测指标</span>
          <a-select v-model:value="metric" aria-label="监测指标">
            <a-select-option v-for="name in metrics" :key="name" :value="name">{{ metricLabels[name] ?? name }}</a-select-option>
          </a-select>
        </div>
        <a-button class="monitor-refresh" :loading="loading" @click="load">
          <template #icon><ReloadOutlined aria-hidden="true" /></template>
          刷新
        </a-button>
      </div>
      <FreshnessBar
        :fetched-at="fetchedAt"
        :stacked="compact"
        :label="`最近 ${WINDOW_LIMIT} 条 · 每 ${REFRESH_MS / 1000} 秒刷新`"
        :interval-ms="REFRESH_MS"
        class="monitor-freshness"
      />
      <a-alert
        v-if="loadError"
        type="error"
        show-icon
        data-testid="monitor-load-error"
        :message="`读取遥测失败：${loadError}${readings.length ? '。当前显示上次成功读取的数据' : ''}`"
      />
    </a-card>

    <div v-if="!drillView" class="monitor-grid">
        <a-card
          class="monitor-card data-workspace-panel monitor-trend"
          size="small"
        >
          <template #title>
            <h2 class="monitor-card__title"><BarChartOutlined aria-hidden="true" />站点读数趋势</h2>
          </template>
          <template #extra>
            <div v-if="activeSource" class="chart-latest">
              <span class="chart-latest__value">
                {{ latestOk ? latestOk.value.toFixed(2) : '—'
                }}<span class="chart-latest__unit">{{ latestOk?.unit ?? '' }}</span>
              </span>
              <time v-if="latestOk" class="chart-latest__time" :datetime="latestOk.observed_at">{{ operatingClock(latestOk.observed_at) }}</time>
              <span v-else class="chart-latest__time">尚无有效读数</span>
            </div>
          </template>
          <div v-if="activeSource" class="chart-source">
            <a-select :value="activeSource.id" aria-label="曲线站点" @update:value="chartSource = String($event)">
              <a-select-option v-for="source in chartSources" :key="source.id" :value="source.id">{{ source.label }}</a-select-option>
            </a-select>
            <p>无效读数在曲线上留空。时间为 UTC+8。</p>
          </div>
          <EChart
            v-if="activeSource"
            :option="chartOption"
            :height="compact ? '225px' : '247px'"
            role="img"
            aria-label="所选站点读数趋势，详细观测值见遥测记录"
          />
          <div v-else class="empty-hero">
            <span class="empty-hero__icon"><AreaChartOutlined aria-hidden="true" /></span>
            <div>
              <p class="empty-hero__title">{{ loading ? '正在读取监测数据' : loadError ? '暂时无法读取趋势' : '当前筛选没有站点读数' }}</p>
              <p v-if="!loading && !loadError" class="empty-hero__desc">可更换监测区域或指标。</p>
            </div>
          </div>
        </a-card>
        <a-card class="monitor-card data-workspace-panel monitor-ledger" size="small">
          <template #title>
            <h2 class="monitor-card__title"><DatabaseOutlined aria-hidden="true" />遥测记录</h2>
          </template>
          <div v-if="compact" :aria-busy="loading">
            <ul v-if="mobileReadings.length" class="ops-list">
              <li v-for="record in mobileReadings" :key="rowKey(record)" class="ops-record">
                <div class="ops-record__head">
                  <span>{{ record.value }} {{ record.unit }}</span>
                  <a-tag :color="record.quality_flag === 'ok' ? 'green' : 'orange'">{{ qualityLabels[record.quality_flag] }}</a-tag>
                </div>
                <div class="ops-record__meta">
                  <span>{{ record.station_id }} · {{ record.region_code }}</span>
                  <time :datetime="record.observed_at">{{ formatOperatingTime(record.observed_at) }}（UTC+8）</time>
                  <span>接入 {{ latencyOf(record) }}</span>
                </div>
              </li>
            </ul>
            <a-pagination
              v-if="ledgerReadings.length > PAGE_SIZE"
              class="mobile-pagination"
              v-model:current="tablePage"
              :total="ledgerReadings.length"
              :page-size="PAGE_SIZE"
              :show-size-changer="false"
              simple
            />
            <p v-if="!mobileReadings.length" class="ops-record">
              {{ loading ? '正在读取记录…' : loadError ? '读取失败，可在上方重试。' : '当前筛选无遥测记录。' }}
            </p>
          </div>
          <div v-else class="monitor-table" :aria-busy="loading">
            <a-table
              :columns="columns"
              :data-source="ledgerReadings"
              :pagination="tablePagination"
              :row-key="rowKey"
              size="small"
              @change="onTableChange"
            >
            <template #emptyText>
              <!-- 空态不能只说"暂无数据"：这一页的读数要么来自演练、要么来自真实上报，
                   值班员站在这儿想知道的是"那我做什么才会有数"。 -->
              <div class="empty-hero">
                <span class="empty-hero__icon"><DatabaseOutlined aria-hidden="true" /></span>
                <div>
                  <p class="empty-hero__title">{{ loading ? '正在读取记录' : loadError ? '暂时无法读取记录' : '这个筛选下没有遥测读数' }}</p>
                  <p v-if="!loading && !loadError" class="empty-hero__desc">可更换区域或指标。</p>
                </div>
              </div>
            </template>
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'value'">
                {{ record.value }} {{ record.unit }}
              </template>
              <template v-else-if="column.key === 'quality_flag'">
                <a-tag :color="record.quality_flag === 'ok' ? 'green' : 'orange'">{{ qualityLabels[record.quality_flag as TelemetryReading['quality_flag']] }}</a-tag>
              </template>
              <template v-else-if="column.key === 'metric'">{{ metricLabels[record.metric] ?? record.metric }}</template>
              <template v-else-if="column.key === 'latency'">
                {{ latencyOf(record) }}
              </template>
              <template v-else-if="column.key === 'observed_at'">
                {{ formatOperatingTime(record.observed_at) }}
              </template>
            </template>
            </a-table>
          </div>
        </a-card>
    </div>

    <a-card v-else class="monitor-card monitor-drills ops-surface" size="small" title="处置链演练">
      <p class="monitor-drills__notice">演练会写入模拟监测数据，并按平台当前配置运行处置链。</p>
      <div v-if="drillResult" ref="drillReceipt" class="monitor-drills__receipt" role="status" data-testid="drill-result">
        <strong>{{ drillResult.name }}演练完成</strong>
        <dl class="monitor-drills__stats">
          <div><dt>模拟读数</dt><dd>{{ drillResult.readings }} 条</dd></div>
          <div><dt>覆盖区域</dt><dd>{{ drillResult.regions }} 个</dd></div>
          <div><dt>形成研判</dt><dd>{{ drillResult.assessed }} 条</dd></div>
          <div><dt>生成预警</dt><dd>{{ drillResult.warnings }} 条</dd></div>
        </dl>
      </div>
      <p v-else-if="drillError" ref="drillReceipt" class="monitor-drills__receipt is-error" role="alert">{{ drillError }}</p>
      <div class="monitor-drills__scenarios">
        <section class="monitor-drills__scenario" aria-labelledby="drill-surge-title">
          <h2 id="drill-surge-title">雨强激增</h2>
          <p>输入快速增强的降雨情景，观察处置链的响应。</p>
          <a-button type="primary" :loading="drilling" :disabled="drilling" data-testid="drill-surge" @click="drill('surge')">
            <template #icon><ExperimentOutlined aria-hidden="true" /></template>
            发起演练：雨强激增
          </a-button>
        </section>
        <section class="monitor-drills__scenario" aria-labelledby="drill-normal-title">
          <h2 id="drill-normal-title">正常背景</h2>
          <p>以常规雨情作对照，查看各区域的研判结果。</p>
          <a-button :loading="drilling" :disabled="drilling" data-testid="drill-normal" @click="drill('normal')">发起演练：正常背景</a-button>
        </section>
      </div>
    </a-card>

    <div v-if="!drillView" class="monitor-tools">
      <ServiceTokenBar />
      <SystemTerminal />
    </div>

    <a-modal v-model:open="reportOpen" title="人工上报（群防群治 / 巡查）" :footer="null" width="720px" data-testid="report-modal">
      <ReportForm :initial-region-code="region" />
    </a-modal>

  </div>
</template>

<style scoped>
.monitor-page {
  container: monitor / inline-size;
}
.monitor-heading { margin-bottom: var(--data-gap); }
.monitor-heading .monitor-report { min-width: 113px; font-weight: var(--aeg-weight-bold); }
.monitor-grid {
  display: grid;
  grid-template-columns: minmax(0, 42fr) minmax(0, 57fr);
  gap: var(--data-column-gap);
  margin-block: var(--data-gap);
}
.monitor-refresh {
  justify-self: start;
}
.monitor-page .ops-filters {
  width: min(100%, 66%);
  min-width: min(780px, 100%);
  grid-template-columns: repeat(2, minmax(0, 1fr)) auto;
  gap: 10px 20px;
}
.monitor-page .ops-filters__field { gap: 3px; }
.monitor-freshness { margin-block: 11px 0; }
.monitor-tools {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: start;
  gap: 8px 16px;
}
.monitor-tools > :deep(section) { margin: 0; }
.monitor-tools > :deep(.tok) { padding-top: 3px; }
.monitor-tools :deep(.sys-term__chip) { min-height: 34px; }
.monitor-tools :deep(.sys-term__body) { width: min(460px, 50cqw); }
.monitor-card__title {
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 0;
  color: var(--aeg-color-text);
  font-size: clamp(18px, 1.4vw, 24px);
  font-weight: var(--aeg-weight-bold);
  line-height: 1.3;
}
.monitor-card__title :deep(.anticon) {
  color: var(--aeg-color-brand);
  font-size: 1.04em;
}
.monitor-drills__notice {
  margin: 0 0 var(--aeg-space-s4);
  color: var(--aeg-color-text-secondary);
}
.monitor-drills__scenarios {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 245px), 1fr));
  gap: var(--aeg-space-s3);
}
.monitor-drills__scenario {
  display: grid;
  grid-template-rows: auto 1fr auto;
  justify-items: start;
  gap: var(--aeg-space-s2);
  padding: var(--aeg-space-s4);
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-lg);
  background: var(--aeg-color-overview-tile);
}
.monitor-drills__scenario h2 {
  margin: 0;
  font-size: var(--aeg-font-md);
}
.monitor-drills__scenario p {
  margin: 0 0 var(--aeg-space-s2);
  color: var(--aeg-color-text-secondary);
}
.monitor-drills__scenario button {
  min-height: 40px;
}
.monitor-drills__receipt {
  margin: 0 0 var(--aeg-space-s4);
  padding: var(--aeg-space-s3);
  border: 1px solid var(--aeg-color-brand-line);
  border-radius: var(--aeg-radius-md);
  background: var(--aeg-color-brand-soft);
}
.monitor-drills__stats {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: var(--aeg-space-s2);
  margin: var(--aeg-space-s2) 0 0;
}
.monitor-drills__stats dt {
  color: var(--aeg-color-text-secondary);
  font-size: var(--aeg-font-sm);
}
.monitor-drills__stats dd {
  margin: 2px 0 0;
  font-size: var(--aeg-font-md);
  font-weight: var(--aeg-weight-semibold);
  font-variant-numeric: tabular-nums;
}
.monitor-drills__receipt.is-error {
  color: var(--aeg-color-status-danger);
}
.chart-source {
  display: grid;
  justify-items: start;
  gap: var(--aeg-space-s1);
  min-width: 0;
  color: var(--aeg-color-text-secondary);
  font-size: var(--aeg-font-sm);
}
.chart-source :deep(.ant-select) {
  max-width: 100%;
}
.chart-source p {
  margin: 0;
}
.mobile-pagination {
  margin-block-start: var(--aeg-space-s3);
  text-align: right;
}
@container monitor (max-width: 1080px) {
  .monitor-grid {
    grid-template-columns: minmax(0, 1fr);
  }
}
@container monitor (max-width: 580px) {
  .monitor-page .ops-filters {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .monitor-tools { grid-template-columns: minmax(0, 1fr); }
  .monitor-tools :deep(.sys-term__body) { width: 100%; }
  .monitor-card :deep(.ant-card-head),
  .monitor-card :deep(.ant-card-body) {
    padding-inline: var(--aeg-space-s3);
  }
  .monitor-drills__stats {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
.monitor-drills {
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-xl);
  background: var(--aeg-color-card);
  box-shadow: var(--aeg-shadow-md);
}
.monitor-card :deep(.ant-card-head) {
  min-height: 0;
  padding: var(--aeg-space-s4) var(--aeg-space-s4) 0;
  border-bottom: none;
  background: transparent;
}
.monitor-card :deep(.ant-card-head-title) {
  padding: 0;
  color: var(--aeg-color-text);
  font-size: var(--aeg-font-md);
  font-weight: var(--aeg-weight-bold);
  letter-spacing: 0.01em;
  white-space: normal;
}
.monitor-card :deep(.ant-card-body) {
  padding: var(--aeg-space-s4);
  background: transparent;
}
.monitor-filter :deep(.ant-card-head),
.monitor-trend :deep(.ant-card-head),
.monitor-ledger :deep(.ant-card-head) {
  padding-block: 7px 0;
}
.monitor-filter :deep(.ant-card-body),
.monitor-trend :deep(.ant-card-body),
.monitor-ledger :deep(.ant-card-body) {
  padding-block: 5px 12px;
}
.monitor-trend :deep(.ant-card-head-wrapper) { align-items: start; }
.monitor-trend :deep(.ant-card-extra) { padding: 0; }
.monitor-trend .chart-source :deep(.ant-select) { min-width: min(255px, 100%); }
.monitor-ledger :deep(.ant-card-body) { padding-top: 11px; }
.monitor-filter :deep(.ant-btn), .monitor-filter :deep(.ant-select-selector) { min-height: 36px; }
.monitor-filter :deep(.ant-select-single .ant-select-selector .ant-select-selection-item) { line-height: 34px; }
.monitor-filter :deep(.ant-select-single) { height: 36px; }
.monitor-filter :deep(.ant-select) { min-width: 0; }
.monitor-filter :deep(.ant-alert) { margin-top: 9px; }

html[data-theme='dark'] .monitor-filter { --data-highlight-position: 68%; }
html[data-theme='dark'] .monitor-ledger { --data-highlight-position: 57%; }
html[data-theme='dark'] .monitor-card__title :deep(.anticon) {
  color: var(--aeg-color-terrain-accent);
  filter: drop-shadow(0 2px 7px var(--aeg-color-command-glow));
}
html[data-theme='dark'] .monitor-heading .monitor-report {
  border-color: var(--aeg-color-terrain-accent);
  background: linear-gradient(180deg, var(--aeg-color-terrain-accent), var(--data-ice) 48%, var(--aeg-color-command-selected) 140%);
  color: var(--aeg-color-command-deep);
  box-shadow: inset 0 0 6px color-mix(in srgb, white 36%, transparent), 0 5px 15px var(--aeg-color-command-glow);
}
html[data-theme='dark'] .monitor-heading .monitor-report:hover {
  background: linear-gradient(180deg, var(--aeg-color-text), var(--data-ice));
}
html[data-theme='dark'] .monitor-filter :deep(.ant-select-selector),
html[data-theme='dark'] .monitor-trend :deep(.ant-select-selector),
html[data-theme='dark'] .monitor-filter .monitor-refresh {
  border-color: color-mix(in srgb, var(--data-ice) 45%, var(--aeg-color-border));
  background: var(--data-inset);
}

/* 右上角：最新有效读数 + 观测时刻 */
.chart-latest {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 2px;
}
.chart-latest__value {
  font-family: var(--aeg-font-display);
  font-variant-numeric: tabular-nums;
  font-size: clamp(23px, 1.85vw, 32px);
  font-weight: var(--aeg-weight-bold);
  line-height: var(--aeg-lh-tight);
  color: var(--aeg-color-text);
}
.chart-latest__unit {
  margin-left: var(--aeg-space-s1);
  font-size: var(--aeg-font-xs);
  font-weight: var(--aeg-weight-semibold);
  opacity: 0.85;
}
.chart-latest__time {
  font-size: var(--aeg-font-sm);
  color: var(--aeg-color-text-secondary);
}
html[data-theme='dark'] .chart-latest__value {
  text-shadow: 0 1px 10px var(--aeg-color-command-glow);
}

/* 明细表收紧档：字号 12px + 单元格 8px，表头允许换行；
   七列在版块里换行排完，不再出现横向滚动条 */
.monitor-table :deep(.ant-table) {
  font-size: var(--aeg-font-sm);
}
.monitor-table :deep(.ant-table-content) {
  max-height: 283px;
  overflow: auto;
  scrollbar-gutter: stable;
  border: 1px solid var(--data-edge, var(--aeg-color-border));
  border-radius: 9px;
}
.monitor-table :deep(.ant-table-pagination.ant-pagination) { margin-block: 9px 0; margin-inline-end: 50px; }
/* 列宽按列配置走，不再被内容撑开 */
.monitor-table :deep(table) {
  table-layout: fixed;
}
.monitor-table :deep(.ant-table.ant-table-small .ant-table-thead > tr > th) {
  position: sticky;
  top: 0;
  z-index: 1;
  padding: 8px 10px;
  font-size: var(--aeg-font-sm);
  line-height: var(--aeg-lh-snug);
  white-space: normal;
}
.monitor-table :deep(.ant-table.ant-table-small .ant-table-tbody > tr > td) {
  height: 47px;
  padding: 6px 10px;
  font-size: var(--aeg-font-sm);
  line-height: 1.24;
  overflow-wrap: anywhere;
}
/* 表格不再自带白底白框：透出玻璃卡本身的底，只留发丝分隔线（令牌色，不是纯白） */
.monitor-table :deep(.ant-table),
.monitor-table :deep(.ant-table-container),
.monitor-table :deep(.ant-table-content),
.monitor-table :deep(.ant-table-thead > tr > th),
.monitor-table :deep(.ant-table-tbody > tr > td) {
  background: transparent;
}
.monitor-table :deep(.ant-table-thead > tr > th) {
  /* 表头压一层比卡片略深的底色，和明细行分开；仍是令牌色，不是纯白 */
  background: var(--aeg-color-sunken);
  border-bottom-color: var(--aeg-color-border);
}
html[data-theme='dark'] .monitor-table :deep(.ant-table-thead > tr > th) {
  background: linear-gradient(var(--aeg-color-command-panel), var(--data-inset));
}
.monitor-table :deep(.ant-table-tbody > tr > td) {
  border-bottom-color: var(--aeg-color-border-subtle);
}
.monitor-table :deep(.ant-table-tbody > tr.ant-table-row:hover > td) {
  background: var(--aeg-color-brand-soft);
}
@container monitor (min-width: 1300px) {
  .monitor-filter .ops-filters__field,
  .monitor-filter :deep(.fresh),
  .monitor-table :deep(.ant-table.ant-table-small .ant-table-thead > tr > th),
  .monitor-table :deep(.ant-table.ant-table-small .ant-table-tbody > tr > td) { font-size: 14px; }
}
</style>
