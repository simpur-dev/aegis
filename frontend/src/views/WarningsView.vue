<script setup lang="ts">
import { message } from 'ant-design-vue'
import { onMounted, ref } from 'vue'

import api, { ApiError } from '@/api/client'
import type { TaskUnit, WarningRecord } from '@/api/types'
import { HAZARD_LABELS, RISK_COLORS, RISK_LABELS } from '@/api/types'
import { formatOperatingTime } from '@/utils/clock'

const warnings = ref<WarningRecord[]>([])
const loading = ref(false)
const open = ref(false)
const current = ref<WarningRecord | null>(null)
const currentTasks = ref<TaskUnit[]>([])
const tasksLoading = ref(false)

const columns = [
  { title: '预警标题', dataIndex: 'title_zh', key: 'title_zh' },
  { title: '灾种', key: 'hazard' },
  { title: '等级', key: 'level' },
  { title: '区域', key: 'regions' },
  { title: '通道', key: 'channels' },
  { title: '触达', key: 'reach' },
  { title: '生成时间（UTC+8）', dataIndex: 'generated_at', key: 'generated_at' },
  { title: '操作', key: 'action' },
]

async function load(): Promise<void> {
  loading.value = true
  try {
    const data = await api.warnings({ limit: 100 })
    warnings.value = data.items
  } catch (error) {
    message.error(error instanceof ApiError ? `加载预警失败：${error.message}` : '加载预警失败')
  } finally {
    loading.value = false
  }
}

async function inspect(record: WarningRecord): Promise<void> {
  current.value = record
  open.value = true
  currentTasks.value = []
  tasksLoading.value = true
  try {
    // 任务单元通过 event_id 关联：逐个按 ID 取回（后端提供 /tasks/{id}）
    const events = await api.events(50)
    const chain = events.items.find((item) => item.warning_id === record.warning_id)
    const units = await Promise.all((chain?.task_units ?? []).map((id) => api.task(id).catch(() => null)))
    currentTasks.value = units.filter((unit): unit is TaskUnit => unit !== null)
  } finally {
    tasksLoading.value = false
  }
}

function reachText(record: WarningRecord): string {
  const delivered = record.deliveries.filter((d) => d.status === 'delivered' || d.status === 'retried').length
  return `${delivered}/${record.deliveries.length} 通道成功`
}

onMounted(load)
</script>

<template>
  <div>
    <a-card size="small" title="预警发布与靶向触达" :loading="loading">
      <template #extra>
        <a-space>
          <a-button @click="load">刷新</a-button>
          <span style="font-size: 12px; color: #8c8c8c">红色预警含北斗短报文兜底通道</span>
        </a-space>
      </template>
      <a-table :columns="columns" :data-source="warnings" row-key="warning_id" :pagination="{ pageSize: 10 }" size="small">
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'hazard'">{{ HAZARD_LABELS[record.hazard_type as keyof typeof HAZARD_LABELS] }}</template>
          <template v-else-if="column.key === 'level'">
            <a-tag :color="RISK_COLORS[record.risk_level as keyof typeof RISK_COLORS]">
              {{ RISK_LABELS[record.risk_level as keyof typeof RISK_LABELS] }}
            </a-tag>
          </template>
          <template v-else-if="column.key === 'regions'">
            <a-space wrap>
              <a-tag v-for="code in record.region_codes" :key="code">{{ code }}</a-tag>
            </a-space>
          </template>
          <template v-else-if="column.key === 'generated_at'">
            {{ formatOperatingTime(record.generated_at) }}
          </template>
          <template v-else-if="column.key === 'channels'">
            <a-space wrap>
              <a-tag v-for="channel in record.channels" :key="channel">{{ channel }}</a-tag>
            </a-space>
          </template>
          <template v-else-if="column.key === 'reach'">{{ reachText(record) }}</template>
          <template v-else-if="column.key === 'action'">
            <a-button type="link" size="small" @click="inspect(record)">详情</a-button>
          </template>
        </template>
      </a-table>
    </a-card>

    <a-drawer v-model:open="open" :width="720" title="预警详情与处置任务">
      <template v-if="current">
        <a-descriptions :column="1" bordered size="small">
          <a-descriptions-item label="标题">{{ current.title_zh }}</a-descriptions-item>
          <a-descriptions-item label="中文正文">{{ current.body_zh }}</a-descriptions-item>
          <a-descriptions-item label="藏文正文">
            <span v-if="current.body_bo">{{ current.body_bo }}</span>
            <a-tag v-else-if="current.translation_pending" color="orange">待译（未接入可信翻译，不伪造译文）</a-tag>
            <span v-else>—</span>
          </a-descriptions-item>
          <a-descriptions-item label="受众">
            <a-tag v-for="audience in current.audiences" :key="audience">{{ audience }}</a-tag>
          </a-descriptions-item>
          <a-descriptions-item label="发布时刻">{{ current.released_at ?? '未发布' }}</a-descriptions-item>
        </a-descriptions>

        <h4>通道投递与回执</h4>
        <a-table
          :data-source="current.deliveries"
          :columns="[
            { title: '通道', dataIndex: 'channel', key: 'channel' },
            { title: '状态', dataIndex: 'status', key: 'status' },
            { title: '覆盖人数', dataIndex: 'audience_count', key: 'audience_count' },
            { title: '回执时刻', dataIndex: 'receipt_at', key: 'receipt_at' },
          ]"
          row-key="(r: { channel: string; attempted_at: string }) => r.channel + r.attempted_at"
          :pagination="false"
          size="small"
        />

        <h4>关联任务单元</h4>
        <a-table
          :data-source="currentTasks"
          :loading="tasksLoading"
          :columns="[
            { title: '任务', dataIndex: 'objective', key: 'objective' },
            { title: '时限', dataIndex: 'sla_seconds', key: 'sla_seconds' },
            { title: '责任角色', dataIndex: 'owner_role', key: 'owner_role' },
            { title: '产出方', dataIndex: 'created_by', key: 'created_by' },
          ]"
          row-key="task_unit_id"
          :pagination="false"
          size="small"
        />
      </template>
    </a-drawer>
  </div>
</template>
