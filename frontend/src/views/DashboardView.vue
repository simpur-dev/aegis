<script setup lang="ts">
import { message } from 'ant-design-vue'
import { computed, onMounted, ref } from 'vue'

import api, { ApiError } from '@/api/client'
import type { AgentInfo, ChainSummary, LatencyReport, WarningRecord } from '@/api/types'
import { HAZARD_LABELS, RISK_COLORS, RISK_LABELS } from '@/api/types'
import { useEventStream } from '@/composables/useEventStream'

const ready = ref<{ agents_online: number; store: Record<string, number> } | null>(null)
const agents = ref<AgentInfo[]>([])
const warnings = ref<WarningRecord[]>([])
const chains = ref<ChainSummary[]>([])
const latency = ref<LatencyReport | null>(null)
const loading = ref(false)
const { events } = useEventStream()

const kpis = computed(() => [
  { label: '在线智能体', value: ready.value?.agents_online ?? 0, suffix: '个' },
  { label: '已发布预警', value: warnings.value.length, suffix: '条' },
  { label: '任务单元', value: ready.value?.store?.task_count ?? 0, suffix: '个' },
  {
    label: '协同成功率',
    value: latency.value?.collaboration.success_rate == null ? '—' : `${(latency.value.collaboration.success_rate * 100).toFixed(1)}%`,
    suffix: latency.value?.collaboration.pass ? '达标' : '样本不足',
  },
])

const regionGrid = computed(() => {
  const byRegion = new Map<string, ChainSummary>()
  for (const chain of chains.value) {
    const region = chain.risk?.region_code ?? chain.task_units[0]?.slice(4, 10) ?? '未知区域'
    const previous = byRegion.get(region)
    if (!previous || (chain.risk && previous.risk && chain.risk.risk_level < previous.risk.risk_level)) {
      byRegion.set(region, chain)
    }
  }
  return [...byRegion.entries()].map(([region, chain]) => ({
    region,
    hazard: chain.risk ? HAZARD_LABELS[chain.risk.hazard_type] : '无风险',
    level: chain.risk?.risk_level ?? 5,
    confidence: chain.risk?.confidence ?? 0,
    degraded: chain.degradations.length > 0,
  }))
})

async function refresh(): Promise<void> {
  loading.value = true
  try {
    const [readyInfo, agentInfo, warningList, eventList, latencyReport] = await Promise.all([
      api.ready(),
      api.agents(),
      api.warnings({ limit: 50 }),
      api.events(20),
      api.latency(),
    ])
    ready.value = { agents_online: readyInfo.agents_online, store: readyInfo.store }
    agents.value = agentInfo.items
    warnings.value = warningList.items
    chains.value = eventList.items
    latency.value = latencyReport
  } catch (error) {
    message.error(error instanceof ApiError ? `加载失败：${error.message}` : '加载失败：未知错误')
  } finally {
    loading.value = false
  }
}

onMounted(refresh)

const stageColumns = [
  { title: '事件', dataIndex: 'trace_id', key: 'trace_id', ellipsis: true },
  { title: '结果', key: 'result', width: 120 },
  { title: '各段执行方式', key: 'stages' },
  { title: '任务', dataIndex: 'task_units', key: 'task_units', customRender: ({ text }: { text: string[] }) => `${text.length} 个` },
]

const agentColumns = [
  { title: '智能体', dataIndex: 'agent_id', key: 'agent_id' },
  { title: '类型', dataIndex: 'agent_type', key: 'agent_type' },
  { title: '能力', dataIndex: 'capabilities', key: 'capabilities', customRender: ({ text }: { text: string[] }) => text.join(', ') },
  { title: '状态', key: 'healthy' },
  { title: '在途/上限', key: 'load' },
]
</script>

<template>
  <div>
    <a-row :gutter="12">
      <a-col v-for="kpi in kpis" :key="kpi.label" :span="6">
        <a-card size="small">
          <a-statistic :title="kpi.label" :value="kpi.value" :suffix="kpi.suffix" />
        </a-card>
      </a-col>
    </a-row>

    <a-row :gutter="12" style="margin-top: 12px">
      <a-col :span="10">
        <a-card title="风险区域网格" size="small" :loading="loading">
          <div class="grid">
            <div v-for="cell in regionGrid" :key="cell.region" class="cell" :style="{ borderColor: RISK_COLORS[cell.level] }">
              <div class="region">{{ cell.region }}</div>
              <div class="hazard">{{ cell.hazard }}</div>
              <a-tag :color="RISK_COLORS[cell.level]">{{ RISK_LABELS[cell.level] }}</a-tag>
              <div v-if="cell.degraded" class="degraded">降级：平台规则兜底</div>
            </div>
            <a-empty v-if="!regionGrid.length" description="暂无研判结论，可在监测页发起演练" />
          </div>
        </a-card>
      </a-col>

      <a-col :span="14">
        <a-card title="链路执行记录（感知→研判→决策→执行→反馈）" size="small" :loading="loading">
          <a-table :columns="stageColumns" :data-source="chains" :pagination="{ pageSize: 6 }" row-key="trace_id" size="small">
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'result'">
                <a-tag :color="record.errors.length ? 'red' : record.ok ? 'green' : 'orange'">
                  {{ record.errors.length ? '失败' : record.ok ? '成功' : '进行中' }}
                </a-tag>
                <a-tag v-if="!record.acted">未触发</a-tag>
              </template>
              <template v-else-if="column.key === 'stages'">
                <a-tag v-for="stage in record.stages" :key="stage.name" :color="stage.mode === 'agent' ? 'blue' : 'default'">
                  {{ stage.name }}:{{ stage.mode }}
                </a-tag>
              </template>
            </template>
          </a-table>
        </a-card>
      </a-col>
    </a-row>

    <a-row :gutter="12" style="margin-top: 12px">
      <a-col :span="14">
        <a-card title="智能体在线状态（经总线契约注册）" size="small">
          <a-table :columns="agentColumns" :data-source="agents" :pagination="false" row-key="agent_id" size="small">
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'healthy'">
                <a-tag :color="record.healthy ? 'green' : 'red'">{{ record.healthy ? '在线' : '失联' }}</a-tag>
              </template>
              <template v-else-if="column.key === 'load'">{{ record.inflight }} / {{ record.max_concurrency }}</template>
            </template>
          </a-table>
        </a-card>
      </a-col>
      <a-col :span="10">
        <a-card title="实时事件流（SSE）" size="small">
          <a-timeline>
            <a-timeline-item v-for="event in events.slice(0, 8)" :key="event.trace_id + event.ts" color="blue">
              <div>{{ event.payload.title ?? event.payload.warning_id ?? event.subject }}</div>
              <small>{{ event.ts }}</small>
            </a-timeline-item>
            <a-empty v-if="!events.length" description="暂无事件" />
          </a-timeline>
        </a-card>
      </a-col>
    </a-row>
  </div>
</template>

<style scoped>
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 10px;
}
.cell {
  border: 2px solid #d9d9d9;
  border-radius: 6px;
  padding: 10px;
  background: #fff;
}
.region {
  font-weight: 600;
}
.hazard {
  font-size: 12px;
  color: #595959;
  margin-bottom: 4px;
}
.degraded {
  margin-top: 6px;
  font-size: 11px;
  color: #fa8c16;
}
</style>
