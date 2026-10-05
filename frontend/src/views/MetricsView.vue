<script setup lang="ts">
import { message } from 'ant-design-vue'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import api from '@/api/client'
import type { LatencyReport } from '@/api/types'
import EChart from '@/components/EChart.vue'
import PageHero from '@/components/PageHero.vue'
import FreshnessBar from '@/components/FreshnessBar.vue'
import type { ChartOption } from '@/components/echarts'
import { latencyRows } from '@/views/metrics/rows'
import { formatMetricValue, toMillis } from '@/utils/metricUnits'

/** 与其他页同一节奏：这页写着"数据来自运行时埋点"，不自己更新就成了摆旧账。 */
const REFRESH_MS = 15_000

const report = ref<LatencyReport | null>(null)
const loading = ref(false)
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
}

/** 横幅里那条计量条/环的宽度：没取到账本或算不出成功率时返回 null（条子留空，不画半条）。 */
const successPercent = computed<number | null>(() => {
  const rate = report.value?.collaboration.success_rate
  return rate == null ? null : Math.round(rate * 1000) / 10
})

/** r=18 的周长；dasharray 用它，offset = 周长 × (1 - 成功率)。 */
const RING_C = 2 * Math.PI * 18
const ringOffset = computed<number | null>(() =>
  successPercent.value === null ? null : RING_C * (1 - successPercent.value / 100),
)

const rows = computed(() => latencyRows(report.value?.metrics ?? {}, INDICATOR_LABELS))

const chartOption = computed<ChartOption>(() => ({
  tooltip: { trigger: 'axis' },
  legend: { data: ['P50', 'P95'] },
  grid: { left: 70, right: 24, bottom: 90 },
  xAxis: { type: 'category', data: rows.value.map((r) => r.label), axisLabel: { rotate: 35, fontSize: 10 } },
  yAxis: { type: 'log', logBase: 10, name: 'ms（对数轴）' },
  // 共用一根毫秒轴：秒制那几条先按出口声明的单位换算，否则轴标签说的"ms"对它们不成立
  series: [
    { name: 'P50', type: 'bar', data: rows.value.map((r) => Math.max(toMillis(r.unit, r.p50), 0.1)) },
    { name: 'P95', type: 'bar', data: rows.value.map((r) => Math.max(toMillis(r.unit, r.p95), 0.1)) },
  ],
}))

async function load(): Promise<void> {
  loading.value = true
  try {
    report.value = await api.latency()
    loadError.value = null
    updatedAt.value = new Date().toISOString()
  } catch (error) {
    // ApiError 与"账本没声明单位"都要把原因写出来：吞成一句"读取指标失败"就等于
    // 让人无从判断是后端没起、还是出口形状对不上
    loadError.value = error instanceof Error ? error.message : String(error)
    message.error(loadError.value)
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
  <div>
    <PageHero icon="标" title="考核指标" badge="实测" caption="数据来自运行时埋点，非人工填写。">
      <template #actions>
        <a-button @click="load">重新读取</a-button>
      </template>
      <template #stats>
        <div class="hero-stat">
          <a-statistic title="协同事务总数" :value="report === null ? '—' : report.collaboration.transactions" />
        </div>
        <div class="hero-stat">
          <a-statistic
            title="协同成功率"
            :value="report?.collaboration.success_rate == null ? '—' : (report.collaboration.success_rate * 100).toFixed(1)"
            :suffix="report?.collaboration.success_rate == null ? '' : '%'"
            :value-style="{ color: report?.collaboration.pass ? '#4ade80' : '#f87171' }"
          />
          <!-- 环形成品率（对标 NexusMind WorldStateHero 的 .ring-progress）：深色托盘里
               一条 4px 横条容易被看成装饰，环把"离满格还差多少"画成角度。
               没量到就留空环——不画一圈灰当"0%"，那是另一种谎 -->
          <svg
            class="ring"
            width="44"
            height="44"
            viewBox="0 0 44 44"
            role="progressbar"
            aria-valuemin="0"
            aria-valuemax="100"
            :aria-valuenow="successPercent ?? undefined"
            aria-label="协同成功率"
          >
            <circle class="ring__track" cx="22" cy="22" r="18" />
            <circle
              class="ring__fill"
              cx="22"
              cy="22"
              r="18"
              :stroke="report?.collaboration.pass ? '#4ade80' : '#f87171'"
              :stroke-dashoffset="ringOffset ?? RING_C"
            />
          </svg>
        </div>
        <div class="hero-stat">
          <!-- 没读到账本就是「—」：0 项越限是一条会让人安心的假消息，只有量出来的 0 才能这么写 -->
          <a-statistic
            title="越限项数"
            :value="report === null ? '—' : Object.keys(report.violations).length"
            :value-style="{ color: report === null ? 'rgba(255,255,255,0.65)' : Object.keys(report.violations).length ? '#f87171' : '#4ade80' }"
          />
        </div>
      </template>
    </PageHero>

    <a-card size="small" title="考核指标实测" :loading="loading">
      <FreshnessBar
        :fetched-at="updatedAt"
        :label="`本页 ${Object.keys(report?.violations ?? {}).length} 项越限 ｜ 每 ${Math.round(REFRESH_MS / 1000)} 秒自动取一次`"
        :interval-ms="REFRESH_MS"
      />
      <a-alert type="info" show-icon message="口径说明">
        <!-- 三条口径挤在一句里读要回头重读；按 NexusMind 的"使用提示"列表拆开，一条一句 -->
        <template #description>
          <ul class="tip-list">
            <li>时延类指标以 P95 判定</li>
            <li>协同成功率取事务台账（request→response）</li>
            <li>
              预警准确率需 5 灾种历史案例回放数据集方可测得
              <span class="pill-dashed">未测得</span>
              · 不以估算充数
            </li>
          </ul>
        </template>
      </a-alert>
      <p v-if="loadError !== null" class="metrics__error" data-testid="metrics-error">
        本页当前读不到账本：{{ loadError }}{{ report === null ? '' : '（下面显示的是上一次成功取到的数字，不是现场实况）' }}
      </p>
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
        :scroll="{ x: 'max-content' }"
        :columns="[
          { title: '指标', dataIndex: 'label', key: 'label' },
          { title: '埋点名', dataIndex: 'name', key: 'name' },
          { title: '样本', dataIndex: 'count', key: 'count' },
          // 列头不写单位：单位跟着每个值一起显示（`0.019 s` / `4200 ms`），
          // 因为这张表里既有毫秒埋点也有秒制埋点，一个列头盖不住两种单位
          { title: 'P50', dataIndex: 'p50', key: 'p50' },
          { title: 'P95', dataIndex: 'p95', key: 'p95' },
          { title: '最大', dataIndex: 'max', key: 'max' },
          { title: '阈值', dataIndex: 'budget', key: 'budget' },
          { title: '判定', key: 'pass' },
        ]"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'pass'">
            <a-tag v-if="record.pass === null" color="default">未设阈值</a-tag>
            <a-tag v-else :color="record.pass ? 'green' : 'red'">{{ record.pass ? '达标' : '超标' }}</a-tag>
          </template>
          <template v-else-if="['p50', 'p95', 'max', 'budget'].includes(String(column.key))">
            {{ formatMetricValue(record.unit, record[column.key as 'p50']) }}
          </template>
        </template>
      </a-table>
    </a-card>
  </div>
</template>

<style scoped>
/* 环形成品率：底环白 16%，填充环从 12 点起算，宽度变化带 0.6s 过渡（NexusMind 同款手感） */
.ring {
  margin-top: 6px;
}
.ring__track {
  fill: none;
  stroke: rgba(255, 255, 255, 0.16);
  stroke-width: 4;
}
.ring__fill {
  fill: none;
  stroke-width: 4;
  stroke-linecap: round;
  stroke-dasharray: 113.1;
  transform: rotate(-90deg);
  transform-origin: 22px 22px;
  transition: stroke-dashoffset 0.6s ease-out;
}
/* .tip-list 与 .pill-dashed 在 styles/theme.css（助手页共用一份） */
/* 指标大数字的等宽托盘样式（.hero-stat）在 styles/theme.css，与态势页共用一份。 */
/* 取数失败的原因要看得见：只有 toast 的话，三秒后页面就只剩一排看着正常的数字 */
.metrics__error {
  margin: 10px 0 0;
  padding: 6px 10px;
  border: 1px solid #ffccc7;
  border-radius: 4px;
  background: #fff2f0;
  color: #cf1322;
  font-size: 12px;
}
</style>
