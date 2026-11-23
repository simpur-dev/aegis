<script setup lang="ts">
import { message } from 'ant-design-vue'
import { ReloadOutlined } from '@ant-design/icons-vue'
import { useMediaQuery } from '@vueuse/core'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import api, { ApiError, type TelemetryQuery } from '@/api/client'
import type { TelemetryReading } from '@/api/types'
import mapApi, { normalizeAnchors } from '@/api/map'
import EChart from '@/components/EChart.vue'
import PageHero from '@/components/PageHero.vue'
import SystemTerminal from '@/components/SystemTerminal.vue'
import FreshnessBar from '@/components/FreshnessBar.vue'
import ReportForm from '@/components/reports/ReportForm.vue'
import type { ChartOption } from '@/components/echarts'
import { echarts } from '@/components/echarts'
import { chartHero } from '@/theme/chart'
import { useTheme } from '@/theme/useTheme'
import { formatOperatingTime, operatingClock } from '@/utils/clock'
import { ingestLatencyLabel } from '@/utils/ingestLatency'

const REFRESH_MS = 15_000
const WINDOW_LIMIT = 2_000
const PAGE_SIZE = 10
const compact = useMediaQuery('(max-width: 720px)')

const readings = ref<TelemetryReading[]>([])
const region = ref<string>('')
const metric = ref<string>('rain_10min')
const loading = ref(false)
const loadError = ref('')
/** 最近一次**成功**取遥测的时刻（UTC ISO）；null = 还没取到过。 */
const fetchedAt = ref<string | null>(null)
const drilling = ref(false)
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
  const hero = chartHero(themeMode.value)
  const samples = chartReadings.value
  return {
    // 标题由卡片头承担，画布里不再重复一行
    tooltip: {
      trigger: 'axis',
      axisPointer: {
        type: 'line',
        lineStyle: { color: hero.line, type: 'dashed', width: 1 },
        label: { formatter: ({ value }: { value: number }) => formatOperatingTime(new Date(value).toISOString()) },
      },
    },
    grid: { left: 56, right: 20, top: 30, bottom: 36 },
    xAxis: {
      type: 'time',
      axisLine: { lineStyle: { color: hero.axis } },
      axisTick: { show: false },
      axisLabel: {
        color: hero.text,
        hideOverlap: true,
        formatter: (value: number) => operatingClock(new Date(value).toISOString()),
      },
    },
    yAxis: {
      type: 'value',
      name: samples[0]?.unit ?? '',
      nameTextStyle: { color: hero.text },
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: hero.text },
      splitLine: { lineStyle: { color: hero.grid } },
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
        itemStyle: { color: hero.line },
        // 线细到 1px（不要阴影：阴影会把线加粗成一团）；亮底上靠纯白和深色蒙层拉开对比
        lineStyle: { color: hero.line, width: 1 },
        // 白色低透明度面积：顶部稍亮、往下化开，压在蓝底上不遮网格
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: hero.areaFrom },
            { offset: 1, color: hero.areaTo },
          ]),
        },
        data: samples.map((r) => [Date.parse(r.observed_at), r.quality_flag === 'ok' ? r.value : null] as const),
        connectNulls: false,
      },
    ],
  }
})

const columns = [
  { title: '站点', dataIndex: 'station_id', key: 'station_id', width: 104 },
  { title: '区域', dataIndex: 'region_code', key: 'region_code', width: 56 },
  { title: '指标', dataIndex: 'metric', key: 'metric', width: 74 },
  { title: '数值', key: 'value', width: 88 },
  { title: '质量', dataIndex: 'quality_flag', key: 'quality_flag', width: 56 },
  { title: '观测时刻（UTC+8，最新在上）', dataIndex: 'observed_at', key: 'observed_at', width: 150 },
  { title: '接入时延', key: 'latency', width: 140 },
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
  try {
    const result = await api.drill({ scenario, ticks: 1 })
    const acted = result.chains.filter((chain) => chain.acted)
    message.success(
      `演练完成：${result.ingest.readings} 条读数，${result.regions} 个区域，${acted.length} 条链路触发预警`,
    )
    await load()
  } catch (error) {
    message.error(error instanceof ApiError ? `演练失败：${error.message}` : '演练失败')
  } finally {
    drilling.value = false
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
const tablePagination = computed(() => ({ current: tablePage.value, pageSize: PAGE_SIZE }))
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

onMounted(() => {
  void loadCatalog()
  void load()
  // 这是盯实时遥测的页面，只取一次数就等于把"最新读数"冻在打开那一刻：
  // 演练发完，表里还是旧数据，而页面标题写着"最新"。与态势总览同一口径——
  // 15 秒一轮，页签切到后台就不打接口，卸载停表。
  timer = setInterval(() => {
    if (document.visibilityState !== 'visible' || loading.value) return
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
  <div class="monitor-page">
    <PageHero surface title="监测预警" badge="实时监测" caption="查看站点读数与接入质量，或提交现场险情。">
      <template #actions>
        <router-link class="monitor-link" to="/warnings">预警台账</router-link>
        <a-button type="primary" data-testid="open-report" @click="reportOpen = true">人工上报</a-button>
      </template>
    </PageHero>

    <a-card class="monitor-card ops-surface" size="small" title="数据筛选">
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
        style="margin-top: 10px"
      />
      <a-alert
        v-if="loadError"
        type="error"
        show-icon
        data-testid="monitor-load-error"
        :message="`读取遥测失败：${loadError}${readings.length ? '。当前显示上次成功读取的数据' : ''}`"
      />
    </a-card>

    <div class="monitor-grid">
        <a-card
          class="monitor-card"
          :class="{ 'monitor-chart-hero': readings.length > 0 }"
          size="small"
          title="站点读数趋势"
        >
          <!-- 右上角报"最新一条有效读数"：画布上可能全是断点，这块只报能用的数 -->
          <template #extra>
            <div v-if="activeSource" class="chart-latest">
              <span class="chart-latest__value">
                {{ latestOk ? latestOk.value.toFixed(2) : '—'
                }}<span class="chart-latest__unit">{{ latestOk?.unit ?? '' }}</span>
              </span>
              <span class="chart-latest__time">{{ latestOk ? operatingClock(latestOk.observed_at) : '尚无有效读数' }}</span>
            </div>
          </template>
          <div v-if="activeSource" class="chart-source">
            <a-select :value="activeSource.id" aria-label="曲线站点" @update:value="chartSource = String($event)">
              <a-select-option v-for="source in chartSources" :key="source.id" :value="source.id">{{ source.label }}</a-select-option>
            </a-select>
            <p>无效读数在曲线上留空。时间为 UTC+8。</p>
          </div>
          <!-- 没读数时不挂蓝底重点卡：一块空色板看着像坏了，空态才告诉值班员下一步做什么 -->
          <EChart v-if="activeSource" :option="chartOption" :height="compact ? '255px' : '320px'" />
          <div v-else class="empty-hero">
            <span class="empty-hero__icon" aria-hidden="true">◇</span>
            <div>
              <p class="empty-hero__title">{{ loading ? '正在读取监测数据' : loadError ? '暂时无法读取趋势' : '当前筛选没有站点读数' }}</p>
              <p v-if="!loading && !loadError" class="empty-hero__desc">可更换监测区域或指标。</p>
            </div>
          </div>
        </a-card>
        <a-card class="monitor-card ops-surface" size="small" title="遥测记录">
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
                <span class="empty-hero__icon" aria-hidden="true">◇</span>
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

    <a-card class="monitor-card monitor-drills ops-surface" size="small" title="处置链演练">
      <div class="monitor-drills__actions">
        <a-button :loading="drilling" :disabled="drilling" data-testid="drill-surge" @click="drill('surge')">发起演练：雨强激增</a-button>
        <a-button :loading="drilling" :disabled="drilling" data-testid="drill-normal" @click="drill('normal')">发起演练：正常背景</a-button>
        <router-link class="monitor-link" to="/simulation">参数推演沙盒</router-link>
      </div>
    </a-card>

    <a-modal v-model:open="reportOpen" title="人工上报（群防群治 / 巡查）" :footer="null" width="720px" data-testid="report-modal">
      <ReportForm :initial-region-code="region" />
    </a-modal>

    <SystemTerminal />
  </div>
</template>

<style scoped>
.monitor-page {
  container: monitor / inline-size;
}
.monitor-grid {
  display: grid;
  grid-template-columns: minmax(0, 5fr) minmax(0, 7fr);
  align-items: start;
  gap: var(--aeg-space-s4);
  margin-block: var(--aeg-space-s4);
}
.monitor-refresh {
  justify-self: start;
}
.monitor-page .ops-filters {
  max-width: 900px;
  grid-template-columns: repeat(2, minmax(0, 1fr)) auto;
}
.monitor-link {
  display: inline-flex;
  align-items: center;
  min-height: 36px;
  padding-inline: var(--aeg-space-s3);
  border: 1px solid var(--aeg-color-border-strong);
  border-radius: var(--aeg-radius-md);
  background: var(--aeg-color-sunken);
  color: var(--aeg-color-text);
}
.monitor-link:hover {
  color: var(--aeg-color-brand-hover);
  border-color: var(--aeg-color-brand-line);
}
.monitor-drills__actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--aeg-space-s2);
}
.chart-source {
  display: grid;
  justify-items: start;
  gap: var(--aeg-space-s1);
  min-width: 0;
  color: var(--aeg-color-chart-hero-text);
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
@container monitor (max-width: 1040px) {
  .monitor-grid {
    grid-template-columns: minmax(0, 1fr);
  }
}
@container monitor (max-width: 580px) {
  .monitor-page .ops-filters {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .monitor-card :deep(.ant-card-head),
  .monitor-card :deep(.ant-card-body) {
    padding-inline: var(--aeg-space-s3);
  }
  .monitor-drills__actions :is(button, a) {
    min-height: 40px;
  }
}
.monitor-card {
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

/*
 * 「时序曲线」重点卡：底图换成蓝色几何光斑（本地资产，模糊 18px），再压一层深蓝渐变，
 * 让白色细线成为画面里最亮的东西。DOM 侧读 --aeg-color-chart-hero-*，
 * canvas 侧读 theme/chart.ts 的 chartHero()，两边同一批令牌。
 */
.monitor-chart-hero {
  position: relative;
  overflow: hidden;
  border: 1px solid var(--aeg-color-border);
  background: var(--aeg-color-chart-hero-from);
  box-shadow: var(--aeg-shadow-lg);
}
.monitor-chart-hero::before {
  content: '';
  position: absolute;
  inset: -40px;
  background-image: url('/backgrounds/monitor-chart-bg.webp');
  background-size: cover;
  background-position: center;
  filter: blur(10px) saturate(1.05);
  transform: scale(1.1);
  opacity: 0.95;
}
.monitor-chart-hero::after {
  content: '';
  position: absolute;
  inset: 0;
  background: linear-gradient(135deg, var(--aeg-color-chart-hero-shade-from), var(--aeg-color-chart-hero-shade-to));
}
.monitor-chart-hero :deep(.ant-card-head),
.monitor-chart-hero :deep(.ant-card-body) {
  position: relative;
  z-index: 1;
}
.monitor-chart-hero :deep(.ant-card-head) {
  border-bottom: 1px solid var(--aeg-color-chart-hero-grid);
  background: transparent;
}
.monitor-chart-hero :deep(.ant-card-head-title) {
  color: var(--aeg-color-chart-hero-text);
}
.monitor-chart-hero :deep(.ant-card-extra) {
  padding: 0;
}
.monitor-chart-hero :deep(.ant-card-body) {
  background: transparent;
}

/* 右上角：最新有效读数 + 观测时刻 */
.chart-latest {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 2px;
}
.chart-latest__value {
  font-family: var(--aeg-font-mono);
  font-variant-numeric: tabular-nums;
  font-size: var(--aeg-font-lg);
  font-weight: var(--aeg-weight-bold);
  line-height: var(--aeg-lh-tight);
  color: var(--aeg-color-chart-hero-text);
}
.chart-latest__unit {
  margin-left: var(--aeg-space-s1);
  font-size: var(--aeg-font-xs);
  font-weight: var(--aeg-weight-semibold);
  opacity: 0.85;
}
.chart-latest__time {
  font-size: var(--aeg-font-sm);
  opacity: 0.8;
  color: var(--aeg-color-chart-hero-text);
}

/* 明细表收紧档：字号 12px + 单元格 8px，表头允许换行；
   七列在版块里换行排完，不再出现横向滚动条 */
.monitor-table :deep(.ant-table) {
  font-size: var(--aeg-font-sm);
}
/* 列宽按列配置走，不再被内容撑开 */
.monitor-table :deep(table) {
  table-layout: fixed;
}
.monitor-table :deep(.ant-table-thead > tr > th) {
  padding: var(--aeg-space-s2);
  font-size: var(--aeg-font-sm);
  line-height: var(--aeg-lh-snug);
  white-space: normal;
}
.monitor-table :deep(.ant-table-tbody > tr > td) {
  padding: var(--aeg-space-s2);
  font-size: var(--aeg-font-sm);
  line-height: var(--aeg-lh-snug);
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
.monitor-table :deep(.ant-table-tbody > tr > td) {
  border-bottom-color: var(--aeg-color-border-subtle);
}
.monitor-table :deep(.ant-table-tbody > tr.ant-table-row:hover > td) {
  background: var(--aeg-color-brand-soft);
}
</style>
