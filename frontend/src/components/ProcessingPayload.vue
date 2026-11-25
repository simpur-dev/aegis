<script setup lang="ts">
import { computed } from 'vue'

import type { ProcessingValues } from '@/api/types'
import { formatOperatingTime } from '@/utils/clock'

const props = defineProps<{
  fields?: ProcessingValues
  samples?: ProcessingValues[]
  sampleLabel: string
}>()

const labels: Record<string, string> = {
  sources: '接入源', timeout_ms: '超时预算（ms）', completed_sources: '已响应', failed_sources: '采集失败',
  candidate_readings: '适配器已解析读数', raw_items: '原始条目', normalized: '标准读数', rejected_values: '无效数值',
  ok: '有效', suspect: '可疑', missing: '缺测标记', drift: '漂移',
  readings: '读数条数', queryable: '已写入查询视图', store: '存储驱动',
  max_observed_to_query_seconds: '最长观测到可查时延（s）', async: '后台提交', queued: '已入队',
  committed: '已提交', pending: '等待提交', dropped: '未持久化', retries: '重试次数',
  action: '事件类型', transport: '传输驱动', published: '投递成功', failed: '投递失败',
  enabled: '已配置', accepted: '接收条数', connector: '连接器', kind: '来源协议', raw_unit: '原始计量口径',
  method: '请求方法', http_status: 'HTTP 状态码', filter_date: '目标日（UTC）', filtered_history: '排除历史行',
  file: '输入文件', max_lag_hours: '最大滞后（h）', latest_observed_at: '最新观测时间',
  scenario: '模拟场景', tick: '模拟轮次', seed: '随机种子', connected: '连接状态', buffered: '待取读数',
  station_id: '站点', region_code: '区划', metric: '指标', value: '值', unit: '单位',
  observed_at: '观测时间', ingested_at: '接入时间', source: '来源', quality_flag: '质量标记',
  date: '日期', message_id: '事件 ID', reason: '原因',
}
const valueLabels: Record<string, string> = {
  simulator: '模拟生成', http: 'HTTP', csv: 'CSV', mqtt: 'MQTT', memory: '内存', postgres: 'PostgreSQL',
  stations: '站点行', rows: 'CSV 行', messages: '消息', readings: '读数', surge: '雨强激增', normal: '正常背景',
  ok: '有效', suspect: '可疑', missing: '缺测', drift: '漂移',
}
const preferredColumns = ['station_id', 'region_code', 'metric', 'value', 'unit', 'quality_flag', 'observed_at', 'ingested_at']
const columns = computed(() => {
  const keys = new Set(props.samples?.flatMap((row) => Object.keys(row)))
  const preferred = preferredColumns.filter((key) => keys.delete(key))
  return [...preferred, ...keys]
})

function show(key: string, value: ProcessingValues[string] | undefined): string {
  if (value == null) return '—'
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (typeof value === 'number') return value.toLocaleString('zh-CN', { maximumFractionDigits: 4 })
  if (key.endsWith('_at') && /^\d{4}-\d{2}-\d{2}T/.test(value)) return formatOperatingTime(value)
  return ['kind', 'store', 'transport', 'raw_unit', 'scenario', 'quality_flag'].includes(key) ? valueLabels[value] ?? value : value
}
</script>

<template>
  <dl v-if="fields && Object.keys(fields).length" class="payload-fields">
    <div v-for="(value, key) in fields" :key="key">
      <dt :title="key">{{ labels[key] ?? key }}</dt>
      <dd>{{ show(key, value) }}</dd>
    </div>
  </dl>
  <details v-if="samples?.length" class="payload-samples">
    <summary>{{ sampleLabel }} <span>{{ samples.length }} 条</span></summary>
    <div class="payload-scroll" tabindex="0" role="region" :aria-label="sampleLabel">
      <table>
        <thead>
          <tr><th v-for="key in columns" :key="key" scope="col" :title="key">{{ labels[key] ?? key }}</th></tr>
        </thead>
        <tbody>
          <tr v-for="(row, index) in samples" :key="index">
            <td v-for="key in columns" :key="key" :class="{ 'is-suspect': key === 'quality_flag' && row[key] !== 'ok' }">
              {{ show(key, row[key]) }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </details>
  <p v-if="!samples?.length && (!fields || !Object.keys(fields).length)" class="payload-empty">此步没有记录</p>
</template>

<style scoped>
.payload-fields {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 100px), 1fr));
  gap: var(--aeg-space-s3);
  margin: 0;
}
.payload-fields dt, .payload-empty {
  color: var(--aeg-color-text-secondary);
  font-size: var(--aeg-font-xs);
}
.payload-fields dd {
  margin: var(--aeg-space-s1) 0 0;
  color: var(--aeg-color-text);
  font-size: var(--aeg-font-base);
  font-weight: var(--aeg-weight-semibold);
  overflow-wrap: anywhere;
}
.payload-empty { margin: 0; }
.payload-samples {
  min-width: 0;
  margin-top: var(--aeg-space-s3);
  border-top: 1px solid var(--aeg-color-border);
}
.payload-samples summary {
  min-height: 36px;
  padding-block: var(--aeg-space-s2);
  color: var(--aeg-color-brand-hover);
  font-size: var(--aeg-font-sm);
  cursor: pointer;
}
.payload-samples summary span {
  margin-left: var(--aeg-space-s1);
  color: var(--aeg-color-text-secondary);
}
.payload-scroll {
  max-width: 100%;
  overflow-x: auto;
}
.payload-scroll table {
  min-width: 100%;
  border-collapse: collapse;
  font-size: var(--aeg-font-xs);
}
.payload-scroll th, .payload-scroll td {
  padding: var(--aeg-space-s2);
  text-align: left;
  white-space: nowrap;
}
.payload-scroll th {
  color: var(--aeg-color-text-secondary);
  background: var(--aeg-color-sunken);
}
.payload-scroll td {
  border-bottom: 1px solid var(--aeg-color-border);
  font-variant-numeric: tabular-nums;
}
.payload-scroll .is-suspect { color: var(--aeg-color-status-warning); }
</style>
