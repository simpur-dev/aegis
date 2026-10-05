<script setup lang="ts">
import { message } from 'ant-design-vue'
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
import { formatOperatingTime, operatingClock } from '@/utils/clock'
import { ingestLatencyLabel } from '@/utils/ingestLatency'

const REFRESH_MS = 15_000
const WINDOW_LIMIT = 2_000

const readings = ref<TelemetryReading[]>([])
const region = ref<string>('')
const metric = ref<string>('rain_10min')
const loading = ref(false)
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

/** 图表与表格用的就是 `readings`：筛选已经交给后端，这里不再本地二次过滤（见 `load()`）。 */
const chartOption = computed<ChartOption>(() => ({
  title: { text: `${metric.value} 时序（${region.value || '全部区域'}）`, left: 'center', textStyle: { fontSize: 14 } },
  tooltip: { trigger: 'axis' },
  grid: { left: 56, right: 24, bottom: 48 },
  xAxis: { type: 'category', data: readings.value.map((r) => operatingClock(r.observed_at)) },
  yAxis: { type: 'value', name: readings.value[0]?.unit ?? '' },
  series: [
    {
      name: metric.value,
      type: 'line',
      smooth: true,
      showSymbol: readings.value.length < 60,
      data: readings.value.map((r) => (r.quality_flag === 'ok' ? r.value : null)),
      connectNulls: false,
    },
  ],
}))

const columns = [
  { title: '站点', dataIndex: 'station_id', key: 'station_id' },
  { title: '区域', dataIndex: 'region_code', key: 'region_code' },
  { title: '指标', dataIndex: 'metric', key: 'metric' },
  { title: '数值', key: 'value' },
  { title: '质量', dataIndex: 'quality_flag', key: 'quality_flag' },
  { title: '观测时刻（UTC+8，最新在上）', dataIndex: 'observed_at', key: 'observed_at' },
  { title: '接入时延', key: 'latency' },
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
  } catch (error) {
    if (token !== requestToken) return
    message.error(error instanceof ApiError ? `读取遥测失败：${error.message}` : '读取遥测失败')
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
const tablePagination = computed(() => ({ current: tablePage.value, pageSize: 10 }))

function onTableChange(pagination: { current?: number }): void {
  tablePage.value = pagination.current ?? 1
}

/** 筛选条件是查询参数而不是本地过滤器：改了就得重取，否则界面显示的仍是上一个区域的数。 */
watch([region, metric], () => {
  tablePage.value = 1
  void load()
})

onMounted(() => {
  void loadCatalog()
  void load()
  // 这是盯实时遥测的页面，只取一次数就等于把"最新读数"冻在打开那一刻：
  // 演练发完，表里还是旧数据，而页面标题写着"最新"。与态势总览同一口径——
  // 15 秒一轮，页签切到后台就不打接口，卸载停表。
  timer = setInterval(() => {
    if (document.visibilityState !== 'visible') return
    void load()
  }, REFRESH_MS)
})

onBeforeUnmount(() => {
  if (timer !== null) clearInterval(timer)
  timer = null
})
</script>

<template>
  <div>
    <PageHero icon="测" title="监测与演练" badge="实时接入" caption="实时遥测与劣化缺口；演练一键触发五段链路。">
      <template #actions>
        <a-button data-testid="open-report" @click="reportOpen = true">人工上报</a-button>
        <a-button type="primary" :loading="drilling" :disabled="drilling" data-testid="drill-surge" @click="drill('surge')">发起灾害演练（激增）</a-button>
        <a-button :loading="drilling" :disabled="drilling" data-testid="drill-normal" @click="drill('normal')">发起背景演练（正常）</a-button>
      </template>
    </PageHero>

    <a-card size="small" title="数据接入与监测">
      <a-space wrap>
        <a-select v-model:value="region" style="width: 180px" placeholder="区域">
          <a-select-option value="">全部区域</a-select-option>
          <a-select-option v-for="code in regions" :key="code" :value="code">{{ code }}</a-select-option>
        </a-select>
        <a-select v-model:value="metric" style="width: 220px" placeholder="指标">
          <a-select-option v-for="name in metrics" :key="name" :value="name">{{ name }}</a-select-option>
        </a-select>
        <a-button @click="load">刷新</a-button>
      </a-space>
      <FreshnessBar
        :fetched-at="fetchedAt"
        :label="`窗口 最近 ${WINDOW_LIMIT} 条 ｜ 每 ${Math.round(REFRESH_MS / 1000)} 秒自刷`"
        :interval-ms="REFRESH_MS"
        style="margin-top: 10px"
      />
    </a-card>

    <a-row :gutter="12" style="margin-top: 12px">
      <a-col :xs="24" :lg="10">
        <a-card size="small" title="时序曲线（劣化读数以缺口显示，不参与判定）">
          <EChart :option="chartOption" height="360px" />
        </a-card>
      </a-col>
      <a-col :xs="24" :lg="14">
        <a-card size="small" title="最新遥测明细" :loading="loading">
          <a-table
            :columns="columns"
            :data-source="ledgerReadings"
            :pagination="tablePagination"
            row-key="(r: TelemetryReading) => r.station_id + r.observed_at"
            size="small"
            :scroll="{ x: 'max-content' }"
            @change="onTableChange"
          >
            <template #emptyText>
              <!-- 空态不能只说"暂无数据"：这一页的读数要么来自演练、要么来自真实上报，
                   值班员站在这儿想知道的是"那我做什么才会有数"。 -->
              <div class="empty-hero">
                <span class="empty-hero__icon" aria-hidden="true">◇</span>
                <p class="empty-hero__title">这个筛选下没有遥测读数</p>
                <p class="empty-hero__desc">
                  换区域或指标看看；也可以直接发起一次演练，链路跑起来就会往这里写读数。
                </p>
                <a class="empty-hero__action" href="#" @click.prevent="drill('surge')">发起一次激增演练 →</a>
              </div>
            </template>
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'value'">
                {{ record.value }} {{ record.unit }}
              </template>
              <template v-else-if="column.key === 'quality_flag'">
                <a-tag :color="record.quality_flag === 'ok' ? 'green' : 'orange'">{{ record.quality_flag }}</a-tag>
              </template>
              <template v-else-if="column.key === 'latency'">
                {{ latencyOf(record) }}
              </template>
              <template v-else-if="column.key === 'observed_at'">
                {{ formatOperatingTime(record.observed_at) }}
              </template>
            </template>
          </a-table>
        </a-card>
      </a-col>
    </a-row>

    <a-modal v-model:open="reportOpen" title="人工上报（群防群治 / 巡查）" :footer="null" width="720px" data-testid="report-modal">
      <ReportForm :initial-region-code="region" />
    </a-modal>

    <SystemTerminal />
  </div>
</template>
