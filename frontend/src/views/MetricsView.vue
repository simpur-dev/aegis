<script setup lang="ts">
import {
  AppstoreOutlined, BarChartOutlined, BlockOutlined, CheckCircleFilled, SafetyCertificateOutlined,
  DatabaseTwoTone, LineChartOutlined, MinusCircleFilled,
  ExclamationCircleFilled, ReloadOutlined, SnippetsOutlined,
} from '@ant-design/icons-vue'
import { useMediaQuery } from '@vueuse/core'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import api from '@/api/client'
import type { LatencyReport } from '@/api/types'
import EChart from '@/components/EChart.vue'
import PageHero from '@/components/PageHero.vue'
import ServiceTokenBar from '@/components/ServiceTokenBar.vue'
import SystemTerminal from '@/components/SystemTerminal.vue'
import FreshnessBar from '@/components/FreshnessBar.vue'
import { colorSlots } from '@/theme/tokens'
import { useTheme } from '@/theme/useTheme'
import MetricRing from '@/views/metrics/MetricRing.vue'
import { latencyChart, p95Trend, TREND_WINDOW_MS, type TrendSample } from '@/views/metrics/charts'
import { latencyRows, type LatencyRow } from '@/views/metrics/rows'
import { formatMetricValue, toMillis } from '@/utils/metricUnits'

/** 与其他页同一节奏：这页写着"数据来自运行时埋点"，不自己更新就成了摆旧账。 */
const REFRESH_MS = 15_000
const compact = useMediaQuery('(max-width: 720px)')

const report = ref<LatencyReport | null>(null)
const rows = ref<LatencyRow[]>([])
const tableFilter = ref<'all' | 'breached' | 'no-budget'>('all')
const loading = ref(false)
const trend = ref<TrendSample[]>([])
/** 这一屏数字是什么时候取的；没取到就还是 null，页面写"尚未取到数据"。 */
const updatedAt = ref<string | null>(null)
/** 取数失败的原因留在页面上：只弹一条三秒就消失的 toast，读数字的人会把"没读到"当成"没问题"。 */
const loadError = ref<string | null>(null)

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
  // 人工上报是第四条接入腿，语义交互另有一项：缺标签就会在表里露出裸埋点名，
  // 而"阈值 300"那一列对秒制指标的含义只能靠标签说清楚
  report_intake_seconds: '人工上报接入端到端时延（秒制）',
  assistant_reply_ms: '语义交互一轮耗时',
  mcp_tool_ms: 'MCP 工具面单次调用',
}

const collaboration = computed(() => report.value?.collaboration)
const successPercent = computed(() => collaboration.value?.success_rate == null ? null : collaboration.value.success_rate * 100)
const successfulTransactions = computed(() => {
  const stats = collaboration.value
  return stats?.success_rate == null ? null : Math.round(stats.success_rate * stats.transactions)
})
const successTone = computed(() => successPercent.value === null ? 'idle' : collaboration.value?.pass ? 'ok' : 'bad')
const violationCount = computed(() => report.value === null ? null : Object.keys(report.value.violations).length)
const violationsTone = computed(() => violationCount.value === null ? 'idle' : violationCount.value ? 'bad' : 'ok')
const compliance = computed(() => {
  const measured = rows.value.filter((row) => row.count > 0)
  const passing = measured.filter((row) => row.pass === true).length
  const failing = measured.filter((row) => row.pass === false).length
  return {
    passing,
    failing,
    unconfigured: rows.value.filter((row) => row.budget === null).length,
    unmeasured: rows.value.length - measured.length,
    rate: passing + failing > 0 ? 100 * passing / (passing + failing) : null,
  }
})

const visibleRows = computed(() => {
  if (tableFilter.value === 'all') return rows.value
  return rows.value.filter((row) =>
    tableFilter.value === 'breached' ? row.count > 0 && row.pass === false : row.budget === null,
  )
})
const chartRows = computed(() => {
  const measured = rows.value.filter((row) => row.count > 0 && row.p95 > 0)
  const limit = compact.value ? 5 : 8
  if (measured.length <= limit) return measured
  return measured.sort((a, b) => toMillis(b.unit, b.p95) - toMillis(a.unit, a.p95)).slice(0, limit)
})

const { resolved } = useTheme()
const chartOption = computed(() => latencyChart(chartRows.value, resolved.value, compact.value))
const hasThresholdLine = computed(() => chartRows.value.filter((row) => row.budget !== null && row.budget > 0).length === 1)
const chartHeight = computed(() => `${Math.max(218, chartRows.value.length * 39 + 62)}px`)
const trendOption = computed(() => p95Trend(trend.value, resolved.value))
const trendCount = computed(() => trend.value.filter(([, value]) => value !== null).length)

function recordP95(at: number, value: number | null): void {
  const previous = trend.value.at(-1)
  const samples = trend.value.filter(([time]) => time >= at - TREND_WINDOW_MS)
  if (previous && at - previous[0] > REFRESH_MS * 2 && previous[0] + REFRESH_MS >= at - TREND_WINDOW_MS) {
    samples.push([previous[0] + REFRESH_MS, null])
  }
  // 快速点刷新时，同一刷新周期保留最后一次观测。
  if (previous && Math.floor(previous[0] / REFRESH_MS) === Math.floor(at / REFRESH_MS)) samples.pop()
  trend.value = [...samples, [at, value]]
}

async function load(): Promise<void> {
  if (loading.value) return
  loading.value = true
  try {
    const nextReport = await api.latency()
    const nextRows = latencyRows(nextReport.metrics, INDICATOR_LABELS)
    const at = Date.now()
    const ingest = nextRows.find((row) => row.name === 'ingest_end_to_end_seconds')
    report.value = nextReport
    rows.value = nextRows
    loadError.value = null
    updatedAt.value = new Date(at).toISOString()
    recordP95(at, ingest?.count ? toMillis(ingest.unit, ingest.p95) : null)
  } catch (error) {
    // ApiError 与"账本没声明单位"都要把原因写出来：吞成一句"读取指标失败"就等于
    // 让人无从判断是后端没起、还是出口形状对不上
    loadError.value = error instanceof Error ? error.message : String(error)
    recordP95(Date.now(), null)
  } finally {
    loading.value = false
  }
}

let timer: ReturnType<typeof setInterval> | null = null

onMounted(() => {
  void load()
  // 页签在后台就不打接口：指标页常开在大屏副屏上，没人看的时候不必一直问账本。
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
  <div class="metrics-page">
    <PageHero
      class="metrics-hero"
      title="指标量测"
      badge="运行实测"
      caption="查看协同成功率、接入时延与超限指标，评估数据处理链路的运行性能。"
    >
      <template #actions>
        <a-button :loading="loading" @click="load">
          <template #icon><ReloadOutlined aria-hidden="true" /></template>
          重新获取
        </a-button>
      </template>
    </PageHero>

    <section class="metrics-overview" aria-label="运行概览">
      <div class="metrics-panel metrics-kpi metrics-kpi--transactions">
        <div class="metrics-symbol"><BlockOutlined aria-hidden="true" /></div>
        <a-statistic title="协同事务总数" :value="collaboration?.transactions ?? '—'" />
      </div>

      <div class="metrics-panel metrics-kpi metrics-kpi--success" :class="`is-${successTone}`">
        <MetricRing :value="successPercent">
          <a-statistic title="协同成功率" :value="successPercent?.toFixed(1) ?? '—'" :suffix="successPercent === null ? '' : '%'" />
        </MetricRing>
        <div class="metrics-kpi__copy">
          <span class="metrics-kpi__label">协同成功率</span>
          <span v-if="successfulTransactions !== null" class="metrics-kpi__detail">
            <CheckCircleFilled v-if="successTone === 'ok'" aria-hidden="true" />
            <ExclamationCircleFilled v-else aria-hidden="true" />
            {{ successfulTransactions }} / {{ collaboration?.transactions }} 成功
          </span>
          <span v-else class="metrics-kpi__detail">尚无成功率读数</span>
        </div>
      </div>

      <div class="metrics-panel metrics-kpi metrics-kpi--violations" :class="`is-${violationsTone}`">
        <div class="metrics-symbol"><SafetyCertificateOutlined aria-hidden="true" /></div>
        <div class="metrics-kpi__copy">
          <a-statistic title="越限项数" :value="violationCount ?? '—'" />
          <span v-if="violationCount !== null" class="metrics-kpi__detail">
            <CheckCircleFilled v-if="violationCount === 0" aria-hidden="true" />
            <ExclamationCircleFilled v-else aria-hidden="true" />
            {{ violationCount === 0 ? '无越限指标' : '存在越限样本' }}
          </span>
        </div>
      </div>

      <div class="metrics-panel metrics-kpi metrics-kpi--freshness">
        <div class="metrics-symbol"><DatabaseTwoTone :two-tone-color="colorSlots[resolved].brand" aria-hidden="true" /></div>
        <div class="metrics-kpi__copy">
          <h2 class="metrics-kpi__label">数据状态</h2>
          <FreshnessBar
            :fetched-at="updatedAt"
            :label="`${report ? `${violationCount} 项越限` : '尚无量测账本'} · 每 ${REFRESH_MS / 1000} 秒刷新`"
            :interval-ms="REFRESH_MS"
            stacked
          />
          <details class="metrics-definition">
            <summary>查量测口径</summary>
            <ul class="tip-list">
              <li>时延类指标以 P95 判定，越限项数按存在越限样本的指标统计。</li>
              <li>协同成功率取事务台账（请求 → 响应）。</li>
              <li>预警准确率需 5 灾种历史案例回放数据集，目前未测得。</li>
            </ul>
          </details>
        </div>
      </div>
    </section>

    <p v-if="loadError !== null" class="metrics__error" data-testid="metrics-error" role="status">
      本页当前读不到账本：{{ loadError }}{{ report === null ? '' : '（下面显示的是上一次成功取到的数字，不是现场实况）' }}
    </p>

    <div class="metrics-analysis">
      <section class="metrics-panel metrics-latency" aria-labelledby="metrics-latency-title">
        <header class="metrics-panel__head">
          <h2 id="metrics-latency-title"><BarChartOutlined aria-hidden="true" />关键链路时延分析</h2>
          <p>对比 P50 / P95，定位主要时延瓶颈。零值见明细。</p>
          <ul v-if="chartRows.length" class="metrics-legend" aria-label="时延图例">
            <li><i class="metrics-legend__p50" aria-hidden="true" />P50<span>（中位时延）</span></li>
            <li><i class="metrics-legend__p95" aria-hidden="true" />P95<span>（95 分位时延）</span></li>
            <li v-if="hasThresholdLine"><i class="metrics-legend__limit" aria-hidden="true" />单项阈值</li>
          </ul>
        </header>
        <EChart
          v-if="chartRows.length"
          :option="chartOption"
          :height="chartHeight"
          role="img"
          aria-label="关键链路 P50 与 P95 时延对比，完整数据见指标明细"
        />
        <p v-else class="metrics-empty">{{ loading ? '正在读取量测…' : loadError ? '暂时无法读取量测。' : '尚无可绘制的时延样本。' }}</p>
      </section>

      <section class="metrics-panel metrics-compliance" aria-labelledby="metrics-compliance-title">
        <header class="metrics-panel__head">
          <h2 id="metrics-compliance-title"><SnippetsOutlined aria-hidden="true" />指标达标情况</h2>
        </header>
        <div class="metrics-compliance__body">
          <MetricRing :value="compliance.rate" :class="{ 'is-bad': compliance.failing > 0 }">
            <span class="metrics-dial__label">达标率</span>
            <strong>{{ compliance.rate === null ? '—' : `${compliance.rate.toFixed(1)}%` }}</strong>
          </MetricRing>
          <dl class="metrics-breakdown">
            <div><dt><CheckCircleFilled class="is-ok" aria-hidden="true" />达标项</dt><dd class="is-ok">{{ report ? compliance.passing : '—' }}</dd></div>
            <div><dt><ExclamationCircleFilled class="is-warn" aria-hidden="true" />超限项</dt><dd class="is-warn">{{ report ? compliance.failing : '—' }}</dd></div>
            <div><dt><MinusCircleFilled aria-hidden="true" />未设阈值</dt><dd>{{ report ? compliance.unconfigured : '—' }}</dd></div>
            <div v-if="compliance.unmeasured"><dt><MinusCircleFilled aria-hidden="true" />未测得</dt><dd>{{ compliance.unmeasured }}</dd></div>
          </dl>
        </div>
        <div class="metrics-trend">
          <h3><LineChartOutlined aria-hidden="true" />P95 时延趋势 <span>（近 1 小时）</span></h3>
          <p class="metrics-trend__caption">数据接入端到端 · 页面打开后采样</p>
          <EChart
            v-if="trendCount"
            :option="trendOption"
            height="76px"
            role="img"
            aria-label="页面打开后采集的接入端到端 P95 时延"
          />
          <p v-else class="metrics-empty metrics-empty--trend">等待接入时延样本</p>
        </div>
      </section>
    </div>

    <section class="metrics-panel metrics-detail" aria-labelledby="metrics-detail-title">
      <header class="metrics-panel__head">
        <h2 id="metrics-detail-title"><AppstoreOutlined aria-hidden="true" />指标明细</h2>
        <div class="metrics-filters" role="group" aria-label="指标筛选">
          <button type="button" :aria-pressed="tableFilter === 'all'" @click="tableFilter = 'all'">全部</button>
          <button type="button" :aria-pressed="tableFilter === 'breached'" @click="tableFilter = 'breached'">超限</button>
          <button type="button" :aria-pressed="tableFilter === 'no-budget'" @click="tableFilter = 'no-budget'">未设阈值</button>
        </div>
      </header>
      <template v-if="compact">
        <ul v-if="visibleRows.length" class="ops-list">
          <li v-for="row in visibleRows" :key="row.name" class="ops-record">
            <div class="ops-record__head">
              <span>{{ row.label }}</span>
              <a-tag v-if="!row.count || row.pass === null">{{ row.count ? '未设阈值' : '未测得' }}</a-tag>
              <a-tag v-else :color="row.pass ? 'green' : 'red'">{{ row.pass ? '达标' : '超标' }}</a-tag>
            </div>
            <div class="metrics-record-key">{{ row.name }}</div>
            <div class="ops-record__meta">
              <span>样本 {{ row.count }}</span>
              <span>P50 {{ formatMetricValue(row.unit, row.count ? row.p50 : null) }}</span>
              <span>P95 {{ formatMetricValue(row.unit, row.count ? row.p95 : null) }}</span>
              <span>最大 {{ formatMetricValue(row.unit, row.count ? row.max : null) }}</span>
              <span>阈值 {{ formatMetricValue(row.unit, row.budget) }}</span>
            </div>
          </li>
        </ul>
        <p v-else class="metrics-empty">{{ loading ? '正在读取指标…' : '当前筛选没有可显示的指标。' }}</p>
      </template>
      <div v-else class="metrics-table">
        <a-table
          :data-source="visibleRows"
          row-key="name"
          :pagination="false"
          size="small"
          :columns="[
            { title: '指标', dataIndex: 'label', key: 'label' },
            { title: '埋点名', dataIndex: 'name', key: 'name' },
            { title: '样本', dataIndex: 'count', key: 'count' },
            { title: 'P50', dataIndex: 'p50', key: 'p50' },
            { title: 'P95', dataIndex: 'p95', key: 'p95' },
            { title: '最大', dataIndex: 'max', key: 'max' },
            { title: '阈值', dataIndex: 'budget', key: 'budget' },
            { title: '判定', key: 'pass' },
          ]"
        >
          <template #emptyText>{{ loading ? '正在读取指标…' : '当前筛选没有可显示的指标。' }}</template>
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'pass'">
              <a-tag v-if="!record.count" color="default">未测得</a-tag>
              <a-tag v-else-if="record.pass === null" color="default">未设阈值</a-tag>
              <a-tag v-else :color="record.pass ? 'green' : 'red'">{{ record.pass ? '达标' : '超标' }}</a-tag>
            </template>
            <template v-else-if="['p50', 'p95', 'max', 'budget'].includes(String(column.key))">
              {{ !record.count && column.key !== 'budget' ? '—' : formatMetricValue(record.unit, record[column.key as 'p50']) }}
            </template>
          </template>
        </a-table>
      </div>
    </section>

    <div class="metrics-tools">
      <ServiceTokenBar class="metrics-token" />
      <SystemTerminal class="metrics-diagnostics" />
    </div>
  </div>
</template>

<style scoped>
.metrics-page {
  --metrics-accent: var(--aeg-color-overview-accent);
  --metrics-panel: var(--aeg-color-card);
  --metrics-line: var(--aeg-color-border);
  display: grid;
  gap: 13px;
  min-width: 0;
}

.metrics-hero {
  --aeg-heading-ridge-position: center 55%;
  position: relative;
  isolation: isolate;
  margin: 0;
  padding: 0 78px 0 42px;
}
.metrics-hero::before {
  position: absolute;
  inset: 7px auto auto 9px;
  width: 7px;
  height: 27px;
  transform: skew(-25deg);
  background: var(--metrics-accent);
  box-shadow: 11px 0 color-mix(in srgb, var(--metrics-accent) 34%, transparent);
  content: '';
}
.metrics-hero :deep(.page-head__row) { gap: 10px; }
.metrics-hero :deep(.page-head__title) { font-size: clamp(24px, 2vw, 30px); }
.metrics-hero :deep(.page-head__badge) { border-radius: var(--aeg-radius-pill); }
.metrics-hero :deep(.page-head__caption) { margin-block: 0; }
.metrics-hero :deep(.ant-btn) {
  border-color: var(--aeg-color-brand-line);
  border-radius: 12px;
  color: var(--aeg-color-text);
}

.metrics-overview,
.metrics-analysis {
  display: grid;
  gap: 16px;
  min-width: 0;
}
.metrics-overview { grid-template-columns: minmax(0, 1.08fr) minmax(0, 1.06fr) minmax(0, .94fr) minmax(0, 1.27fr); }
.metrics-analysis { grid-template-columns: minmax(0, 3fr) minmax(275px, 1.03fr); }
.metrics-panel {
  position: relative;
  isolation: isolate;
  min-width: 0;
  border: 1px solid var(--metrics-line);
  border-radius: 12px;
  background: var(--metrics-panel);
  box-shadow: var(--aeg-shadow-sm);
}

.metrics-kpi {
  display: grid;
  grid-template-columns: minmax(68px, 36%) minmax(0, 1fr);
  align-items: center;
  justify-items: center;
  gap: 10px;
  min-height: 106px;
  padding: 6px 14px;
}
.metrics-kpi > :last-child { justify-self: stretch; }
.metrics-kpi--success { --metrics-dial-size: 92px; grid-template-columns: minmax(85px, 43%) minmax(0, 1fr); }
.metrics-kpi--freshness { grid-template-columns: minmax(48px, 21%) minmax(0, 1fr); }
.metrics-kpi__copy { min-width: 0; }
.metrics-kpi__label,
.metrics-kpi :deep(.ant-statistic-title) {
  margin: 0;
  color: var(--aeg-color-text);
  font-size: 14px;
  font-weight: var(--aeg-weight-semibold);
}
.metrics-kpi :deep(.ant-statistic-content) {
  color: var(--metrics-status, var(--aeg-color-text));
  font-family: var(--aeg-font-display);
  font-size: 36px;
  font-weight: var(--aeg-weight-bold);
  font-variant-numeric: tabular-nums;
  line-height: 1.1;
}
.metrics-kpi .metric-ring :deep(.ant-statistic-title) { display: none; }
.metrics-kpi .metric-ring :deep(.ant-statistic-content) { color: var(--aeg-color-text); font-size: 19px; }
.metrics-kpi .metric-ring :deep(.ant-statistic-content-suffix) { margin: 0; font-size: 12px; }
.metrics-kpi__detail {
  display: block;
  margin-top: 3px;
  color: var(--aeg-color-text-secondary);
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}
.metrics-kpi__detail :deep(.anticon) { color: var(--metrics-status); margin-inline-end: 4px; }
.metrics-kpi--success .metrics-kpi__detail { margin-top: 10px; }
.metrics-symbol {
  display: grid;
  width: 100%;
  min-height: 61px;
  place-items: center;
  border-inline-end: 1px solid var(--metrics-line);
  color: var(--metrics-status, var(--aeg-color-brand));
  font-size: 39px;
}
.is-ok { --metrics-status: var(--aeg-color-status-success); }
.is-bad { --metrics-status: var(--aeg-color-status-danger); }
.is-warn { --metrics-status: var(--aeg-color-status-warning); }

.metrics-kpi :deep(.fresh) { margin: 3px 0; font-size: 12px; }
.metrics-kpi :deep(.fresh__rule) { display: none; }
.metrics-definition { position: relative; font-size: 13px; }
.metrics-definition summary {
  width: fit-content;
  color: var(--aeg-color-brand-hover);
  font-weight: var(--aeg-weight-semibold);
  cursor: pointer;
}
.metrics-definition .tip-list {
  position: absolute;
  z-index: 1;
  inset: 100% 0 auto auto;
  width: min(310px, calc(100vw - 42px));
  margin: 5px 0 0;
  padding: 10px 13px;
  border: 1px solid var(--metrics-line);
  border-radius: 10px;
  background: var(--aeg-color-elevated);
  box-shadow: var(--aeg-shadow-md);
  color: var(--aeg-color-text-secondary);
}
.metrics-kpi--freshness:has(details[open]) { z-index: 2; }

.metrics-latency, .metrics-compliance, .metrics-detail { padding: 10px 15px 12px; }
.metrics-panel__head { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 16px; }
.metrics-panel__head h2 {
  display: flex;
  align-items: center;
  gap: 9px;
  flex: 0 0 auto;
  margin: 0;
  color: var(--aeg-color-text);
  font-size: clamp(17px, 1.45vw, 23px);
  font-weight: var(--aeg-weight-bold);
  line-height: 1.3;
}
.metrics-panel__head h2 :deep(.anticon), .metrics-trend h3 :deep(.anticon) {
  color: var(--aeg-color-brand-hover);
}
.metrics-panel__head p,
.metrics-legend {
  margin: 0;
  color: var(--aeg-color-text-secondary);
  font-size: 12px;
}
.metrics-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 13px;
  margin-inline-start: auto;
  padding: 0;
  list-style: none;
}
.metrics-legend li { display: inline-flex; align-items: center; gap: 5px; white-space: nowrap; }
.metrics-legend li span { margin-inline-start: -5px; }
.metrics-legend i { width: 11px; height: 10px; border-radius: 3px; }
.metrics-legend__p50 { background: linear-gradient(var(--aeg-color-chart-bar-p50-to), var(--aeg-color-chart-bar-p50-from)); }
.metrics-legend__p95 { background: linear-gradient(var(--aeg-color-chart-bar-p95-to), var(--aeg-color-chart-bar-p95-from)); }
.metrics-legend .metrics-legend__limit { width: 16px; height: 0; border-top: 2px dashed var(--aeg-color-status-danger); }

.metrics-compliance { --metrics-dial-size: 100px; }
.metrics-compliance h2 { font-size: 16px; }
.metrics-compliance__body {
  display: grid;
  grid-template-columns: minmax(100px, 47%) minmax(0, 1fr);
  align-items: center;
  justify-items: center;
  gap: 12px;
  min-height: 109px;
  padding-block: 6px;
}
.metrics-dial__label { display: block; font-size: 12px; color: var(--aeg-color-text-secondary); }
.metrics-compliance strong { font-size: 18px; font-variant-numeric: tabular-nums; }
.metrics-breakdown {
  justify-self: stretch;
  margin: 0;
  padding-inline-start: 10px;
  border-inline-start: 1px solid var(--metrics-line);
  font-size: 13px;
}
.metrics-breakdown div { display: flex; justify-content: space-between; gap: 5px; padding: 4px 0; }
.metrics-breakdown div + div { border-top: 1px solid var(--aeg-color-border-subtle); }
.metrics-breakdown dt { display: inline-flex; align-items: center; gap: 6px; }
.metrics-breakdown dd { margin: 0; font-weight: 700; font-variant-numeric: tabular-nums; }
.metrics-breakdown :is(dd, .anticon) { color: var(--metrics-status, var(--aeg-color-text-secondary)); }
.metrics-trend { border-top: 1px solid var(--metrics-line); padding-top: 6px; }
.metrics-trend h3 { margin: 0; font-size: 14px; font-weight: var(--aeg-weight-bold); }
.metrics-trend h3 span { color: var(--aeg-color-text-secondary); font-weight: 400; }
.metrics-trend__caption { margin: 1px 0 3px; color: var(--aeg-color-text-secondary); font-size: 11px; }
.metrics-empty {
  display: grid;
  min-height: 90px;
  place-items: center;
  margin: 0;
  color: var(--aeg-color-text-secondary);
  font-size: 13px;
  text-align: center;
}
.metrics-empty--trend { min-height: 76px; }

.metrics-detail .metrics-panel__head { margin-bottom: 8px; }
.metrics-filters {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 2px;
  padding: 2px;
  border: 1px solid var(--aeg-color-brand-line);
  border-radius: 8px;
  background: var(--aeg-color-sunken);
}
.metrics-filters button {
  min-height: 28px;
  padding: 2px 14px;
  border: 1px solid transparent;
  border-radius: 6px;
  background: transparent;
  color: var(--aeg-color-text-secondary);
  font: inherit;
  cursor: pointer;
}
.metrics-filters button:hover { color: var(--aeg-color-text); }
.metrics-filters [aria-pressed='true'],
.metrics-filters [aria-pressed='true']:hover {
  border-color: var(--aeg-color-brand-line);
  background: var(--aeg-color-masthead-tab);
  color: var(--aeg-color-masthead-tab-ink);
  font-weight: var(--aeg-weight-semibold);
}
.metrics-filters button:focus-visible,
.metrics-definition summary:focus-visible { outline: 2px solid var(--aeg-color-brand); outline-offset: 2px; }

.metrics-table { overflow: auto; border: 1px solid var(--metrics-line); border-radius: 7px; }
.metrics-table :deep(.ant-table) { min-width: 705px; background: transparent; font-variant-numeric: tabular-nums; }
.metrics-table :deep(.ant-table.ant-table-small .ant-table-cell) {
  border-color: var(--metrics-line);
  padding: 5px 13px;
  font-size: 13px;
  line-height: 1.45;
  overflow-wrap: anywhere;
}
.metrics-table :deep(.ant-table-thead .ant-table-cell) {
  color: var(--aeg-color-text);
  background: var(--aeg-color-overview-tile);
}
.metrics-table :deep(.ant-table-tbody > tr:nth-child(even) > td) { background: color-mix(in srgb, var(--metrics-accent) 4%, transparent); }
.metrics-table :deep(.ant-table-tbody > tr > td.ant-table-cell-row-hover) { background: var(--aeg-color-brand-soft); }
.metrics-table :deep(.ant-table-tbody > tr:last-child > td) { border-bottom: 0; }
.metrics-table :deep(.ant-tag) { margin-inline-end: 0; }
.metrics-record-key { color: var(--aeg-color-text-muted); font-size: 12px; overflow-wrap: anywhere; }
.metrics-tools { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 9px 16px; align-items: start; }
.metrics-tools .metrics-token, .metrics-tools .metrics-diagnostics { margin: 0; }
.metrics-tools .metrics-token { padding-top: 2px; }

.metrics__error {
  margin: 0;
  padding: 7px 12px;
  border: 1px solid var(--aeg-tag-red-line);
  border-radius: 8px;
  background: var(--aeg-tag-red-bg);
  color: var(--aeg-tag-red-text);
  font-size: 13px;
}

html[data-theme='dark'] .metrics-page {
  --metrics-panel: color-mix(in srgb, var(--aeg-color-command-deep) 91%, transparent);
  --metrics-line: color-mix(in srgb, var(--metrics-accent) 23%, var(--aeg-color-command-line));
}
html[data-theme='dark'] .metrics-hero :deep(.ant-btn) {
  background: linear-gradient(180deg, color-mix(in srgb, var(--aeg-color-command-raised) 64%, transparent), var(--aeg-color-command-deep));
}
html[data-theme='dark'] .metrics-panel {
  --metrics-flare: var(--metrics-accent);
  --metrics-highlight: color-mix(in srgb, var(--metrics-flare) 58%, white);
  background:
    radial-gradient(ellipse at 0 0, color-mix(in srgb, var(--metrics-flare) 21%, transparent), transparent 49%),
    radial-gradient(ellipse at 98% 100%, color-mix(in srgb, var(--metrics-flare) 20%, transparent), transparent 55%),
    linear-gradient(162deg, color-mix(in srgb, var(--aeg-color-command-raised) 43%, var(--metrics-panel)), var(--metrics-panel) 70%);
  box-shadow: inset 0 1px 12px color-mix(in srgb, var(--metrics-flare) 12%, transparent),
    0 7px 19px -8px color-mix(in srgb, var(--metrics-flare) 38%, transparent);
}
html[data-theme='dark'] .metrics-panel::before {
  position: absolute;
  inset: -1px;
  border-radius: inherit;
  padding: 1px;
  background:
    radial-gradient(ellipse 52% 83% at 0 0, var(--metrics-highlight), transparent 86%),
    radial-gradient(ellipse 49% 76% at 98% 100%, var(--metrics-highlight), transparent 83%);
  mask: linear-gradient(black 0 0) content-box, linear-gradient(black 0 0);
  mask-composite: exclude;
  pointer-events: none;
  content: '';
}
html[data-theme='dark'] .metrics-kpi--success {
  --metrics-flare: var(--metrics-status, var(--metrics-accent));
  --metrics-line: color-mix(in srgb, var(--metrics-flare) 44%, var(--aeg-color-command-line));
}
html[data-theme='dark'] .metrics-kpi--violations.is-bad { --metrics-flare: var(--metrics-status); }
html[data-theme='dark'] .metrics-kpi--transactions::after,
html[data-theme='dark'] .metrics-kpi--success::after {
  position: absolute;
  z-index: -1;
  inset: auto 0 0 auto;
  width: 45%;
  height: 48px;
  border-end-end-radius: inherit;
  background: url('/backgrounds/aegis-glacier-ridge.png') right bottom -9px / contain no-repeat;
  opacity: .65;
  pointer-events: none;
  content: '';
}
html[data-theme='dark'] .metrics-kpi--success::after { filter: hue-rotate(-65deg); }
html[data-theme='dark'] .metrics-symbol {
  color: var(--metrics-status, var(--metrics-accent));
  filter: drop-shadow(0 3px 8px color-mix(in srgb, currentColor 55%, transparent));
}
html[data-theme='dark'] .metrics-panel__head h2 :deep(.anticon),
html[data-theme='dark'] .metrics-trend h3 :deep(.anticon) { color: var(--metrics-accent); }
html[data-theme='dark'] .metrics-filters [aria-pressed='true'] {
  background: linear-gradient(160deg, color-mix(in srgb, white 22%, var(--metrics-accent)), var(--metrics-accent));
  box-shadow: inset 0 1px 4px color-mix(in srgb, white 35%, transparent);
}
html[data-theme='dark'] .metrics-table :deep(.ant-table-thead .ant-table-cell) {
  background: linear-gradient(180deg, var(--aeg-color-command-raised), var(--aeg-color-command-panel));
}

@media (max-width: 1100px) {
  .metrics-overview { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .metrics-legend li span { display: none; }
}
@media (max-width: 850px) {
  .metrics-analysis { grid-template-columns: minmax(0, 1fr); }
  .metrics-compliance__body { grid-template-columns: 130px minmax(0, 1fr); }
  .metrics-legend { margin-inline-start: 0; }
  .metrics-tools { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 540px) {
  .metrics-page { gap: 11px; }
  .metrics-hero { padding-inline: 27px 0; }
  .metrics-hero::before { inset-inline-start: 4px; }
  .metrics-hero :deep(.page-head__caption) { font-size: 12px; }
  .metrics-overview, .metrics-analysis { gap: 9px; }
  .metrics-kpi { grid-template-columns: minmax(0, 1fr); gap: 6px; padding: 9px; text-align: center; }
  .metrics-kpi--success { --metrics-dial-size: 64px; }
  .metrics-kpi .metric-ring :deep(.ant-statistic-content) { font-size: 14px; }
  .metrics-kpi .metric-ring :deep(.ant-statistic-content-suffix) { font-size: 10px; }
  .metrics-kpi__label, .metrics-kpi :deep(.ant-statistic-title) { font-size: 13px; }
  .metrics-kpi--success .metrics-kpi__detail { margin-top: 2px; }
  .metrics-kpi--freshness { grid-column: 1 / -1; grid-row: 3; grid-template-columns: 58px minmax(0, 1fr); text-align: start; }
  .metrics-kpi--violations { grid-column: 1 / -1; grid-row: 2; grid-template-columns: 58px minmax(0, 1fr); text-align: start; }
  .metrics-symbol { min-height: 42px; border-inline-end: 0; font-size: 27px; }
  .metrics-kpi--violations, .metrics-kpi--freshness { min-height: 0; }
  .metrics-latency, .metrics-compliance, .metrics-detail { padding-inline: 10px; }
  .metrics-detail .metrics-panel__head { gap: 8px; }
  .metrics-filters button { min-height: 34px; padding-inline: 9px; }
}
</style>
