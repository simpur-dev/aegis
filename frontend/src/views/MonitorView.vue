<script setup lang="ts">
import { message } from 'ant-design-vue'
import { computed, onMounted, ref } from 'vue'

import api, { ApiError } from '@/api/client'
import type { TelemetryReading } from '@/api/types'
import EChart from '@/components/EChart.vue'
import ReportForm from '@/components/reports/ReportForm.vue'
import type { ChartOption } from '@/components/echarts'

const readings = ref<TelemetryReading[]>([])
const region = ref<string>('')
const metric = ref<string>('rain_10min')
const loading = ref(false)
const drilling = ref(false)
/**
 * 人工上报（批次 B5）：这条腿是"接入 ≤5min"的第四个真实入口，
 * 表单与回执都在 `components/reports/ReportForm.vue` 一处，监测页只负责开门。
 */
const reportOpen = ref(false)

const regions = computed(() => [...new Set(readings.value.map((r) => r.region_code))].sort())
const metrics = computed(() => [...new Set(readings.value.map((r) => r.metric))].sort())

const filtered = computed(() =>
  readings.value.filter(
    (r) => (!region.value || r.region_code === region.value) && r.metric === metric.value,
  ),
)

const chartOption = computed<ChartOption>(() => ({
  title: { text: `${metric.value} 时序（${region.value || '全部区域'}）`, left: 'center', textStyle: { fontSize: 14 } },
  tooltip: { trigger: 'axis' },
  grid: { left: 56, right: 24, bottom: 48 },
  xAxis: { type: 'category', data: filtered.value.map((r) => r.observed_at.slice(11, 19)) },
  yAxis: { type: 'value', name: filtered.value[0]?.unit ?? '' },
  series: [
    {
      name: metric.value,
      type: 'line',
      smooth: true,
      showSymbol: filtered.value.length < 60,
      data: filtered.value.map((r) => (r.quality_flag === 'ok' ? r.value : null)),
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
  { title: '观测时刻', dataIndex: 'observed_at', key: 'observed_at' },
  { title: '接入时延', key: 'latency' },
]

async function load(): Promise<void> {
  loading.value = true
  try {
    const data = await api.telemetry({ limit: 2_000 })
    readings.value = data.items
  } catch (error) {
    message.error(error instanceof ApiError ? `读取遥测失败：${error.message}` : '读取遥测失败')
  } finally {
    loading.value = false
  }
}

async function drill(scenario: 'surge' | 'normal'): Promise<void> {
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
  const ms = (new Date(row.ingested_at).getTime() - new Date(row.observed_at).getTime()) || 0
  return `${ms.toFixed(0)} ms`
}

onMounted(load)
</script>

<template>
  <div>
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
        <a-button data-testid="open-report" @click="reportOpen = true">人工上报</a-button>
        <a-button type="primary" :loading="drilling" @click="drill('surge')">发起灾害演练（激增）</a-button>
        <a-button :loading="drilling" @click="drill('normal')">发起背景演练（正常）</a-button>
      </a-space>
    </a-card>

    <a-row :gutter="12" style="margin-top: 12px">
      <a-col :span="10">
        <a-card size="small" title="时序曲线（劣化读数以缺口显示，不参与判定）">
          <EChart :option="chartOption" height="360px" />
        </a-card>
      </a-col>
      <a-col :span="14">
        <a-card size="small" title="最新遥测明细" :loading="loading">
          <a-table
            :columns="columns"
            :data-source="filtered"
            :pagination="{ pageSize: 10 }"
            row-key="(r: TelemetryReading) => r.station_id + r.observed_at"
            size="small"
          >
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
            </template>
          </a-table>
        </a-card>
      </a-col>
    </a-row>

    <a-modal v-model:open="reportOpen" title="人工上报（群防群治 / 巡查）" :footer="null" width="720px" data-testid="report-modal">
      <ReportForm :initial-region-code="region" />
    </a-modal>
  </div>
</template>
