<script setup lang="ts">
import { message } from 'ant-design-vue'
import { computed, onMounted, ref } from 'vue'

import api, { ApiError } from '@/api/client'
import type { LatencyReport } from '@/api/types'
import EChart from '@/components/EChart.vue'
import type { ChartOption } from '@/components/echarts'

const report = ref<LatencyReport | null>(null)
const loading = ref(false)

/** 与后端 latency_report() 的阈值口径保持一致，避免前端自造指标定义。 */
const INDICATOR_LABELS: Record<string, string> = {
  sync_agent_to_gateway_ms: '智能体→平台 共享同步时延',
  collab_txn: '协同事务往返时延',
  stage_perceive_ms: '感知段耗时',
  stage_assess_ms: '研判段耗时',
  stage_plan_ms: '决策段耗时',
  stage_execute_ms: '执行段耗时',
  stage_feedback_ms: '反馈段耗时',
  warning_generation_ms: '预警生成耗时',
  warning_reach_ms: '预警触达耗时',
  ingest_end_to_end_seconds: '数据接入端到端时延（秒制）',
  ingest_store_ms: '观测→入库',
  ingest_publish_ms: '入库→上总线',
  ingest_store_to_query_ms: '入库→可查询',
}

const rows = computed(() =>
  Object.entries(report.value?.metrics ?? {}).map(([name, stats]) => ({
    name,
    label: INDICATOR_LABELS[name] ?? name,
    ...stats,
    pass: stats.budget_ms == null ? null : stats.p95_ms <= stats.budget_ms,
  })),
)

const chartOption = computed<ChartOption>(() => ({
  tooltip: { trigger: 'axis' },
  legend: { data: ['P50', 'P95'] },
  grid: { left: 70, right: 24, bottom: 90 },
  xAxis: { type: 'category', data: rows.value.map((r) => r.label), axisLabel: { rotate: 35, fontSize: 10 } },
  yAxis: { type: 'log', logBase: 10, name: 'ms（对数轴）' },
  series: [
    { name: 'P50', type: 'bar', data: rows.value.map((r) => Math.max(r.p50_ms, 0.1)) },
    { name: 'P95', type: 'bar', data: rows.value.map((r) => Math.max(r.p95_ms, 0.1)) },
  ],
}))

async function load(): Promise<void> {
  loading.value = true
  try {
    report.value = await api.latency()
  } catch (error) {
    message.error(error instanceof ApiError ? `读取指标失败：${error.message}` : '读取指标失败')
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div>
    <a-card size="small" title="考核指标实测（数据来自运行时埋点，非人工填写）" :loading="loading">
      <template #extra><a-button @click="load">重新读取</a-button></template>
      <a-alert
        type="info"
        show-icon
        message="口径说明"
        description="时延类指标以 P95 判定；协同成功率取事务台账（request→response）。预警准确率需 5 灾种历史案例回放数据集方可测得，当前显式标注为未测，不以估算充数。"
      />
      <a-row :gutter="12" style="margin-top: 12px">
        <a-col :span="8">
          <a-statistic
            title="协同事务总数"
            :value="report?.collaboration.transactions ?? 0"
          />
        </a-col>
        <a-col :span="8">
          <a-statistic
            title="协同成功率"
            :value="report?.collaboration.success_rate == null ? '—' : (report.collaboration.success_rate * 100).toFixed(1)"
            :suffix="report?.collaboration.success_rate == null ? '' : '%'"
            :value-style="{ color: report?.collaboration.pass ? '#3f8600' : '#cf1322' }"
          />
        </a-col>
        <a-col :span="8">
          <a-statistic title="越限项数" :value="Object.keys(report?.violations ?? {}).length" :value-style="{ color: Object.keys(report?.violations ?? {}).length ? '#cf1322' : '#3f8600' }" />
        </a-col>
      </a-row>
    </a-card>

    <a-card size="small" title="时延分布" style="margin-top: 12px">
      <EChart :option="chartOption" height="360px" />
    </a-card>

    <a-card size="small" title="指标明细" style="margin-top: 12px">
      <a-table
        :data-source="rows"
        row-key="name"
        :pagination="false"
        size="small"
        :columns="[
          { title: '指标', dataIndex: 'label', key: 'label' },
          { title: '埋点名', dataIndex: 'name', key: 'name' },
          { title: '样本', dataIndex: 'count', key: 'count' },
          { title: 'P50(ms)', dataIndex: 'p50_ms', key: 'p50_ms' },
          { title: 'P95(ms)', dataIndex: 'p95_ms', key: 'p95_ms' },
          { title: '最大(ms)', dataIndex: 'max_ms', key: 'max_ms' },
          { title: '阈值(ms)', dataIndex: 'budget_ms', key: 'budget_ms' },
          { title: '判定', key: 'pass' },
        ]"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'pass'">
            <a-tag v-if="record.pass === null" color="default">未设阈值</a-tag>
            <a-tag v-else :color="record.pass ? 'green' : 'red'">{{ record.pass ? '达标' : '超标' }}</a-tag>
          </template>
        </template>
      </a-table>
    </a-card>
  </div>
</template>
