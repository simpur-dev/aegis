<script setup lang="ts">
import { message } from 'ant-design-vue'
import { PlayCircleOutlined } from '@ant-design/icons-vue'
import { useMediaQuery } from '@vueuse/core'
import { computed, ref } from 'vue'

import PageHero from '@/components/PageHero.vue'
import {
  PLAYBOOK_ACTIONS,
  SIMULATION_DEFAULTS,
  SIMULATION_LIMITS,
  SIMULATION_FIELD_LABELS,
  describeSimulationError,
  preflightSimulation,
  roleLabel,
  runSimulation,
  toSimulationBody,
  type SimulationReport,
  type SimulationBody,
} from '@/api/simulation'
import { formatOperatingTime } from '@/utils/clock'

/**
 * 推演沙盒（§3.6 / §4.4）。
 *
 * 这页只做两件事：把情景参数交给沙盒、把报告原样摊开。数字全部来自后端——
 * 前端不做任何换算或"补一个看起来合理的值"，因为这一页的验收口径是
 * "概率矩阵正确性抽查 + 演示非 SLA"，掺进前端估算就再没人能抽查出什么。
 *
 * 不自动刷新：LLM 角色档一次 T=5 的推演要点 20 次模型，钱该由按下的那个人决定。
 */
const draft = ref({ ...SIMULATION_DEFAULTS })
const report = ref<SimulationReport | null>(null)
const running = ref(false)
/** 失败原因留在页面上：只有 toast 的话，三秒后这页只剩一张看起来正常的空表。 */
const failure = ref<string | null>(null)
const startedAt = ref<string | null>(null)
const reportInput = ref<SimulationBody | null>(null)
const compact = useMediaQuery('(max-width: 720px)')
const reportIsPrevious = computed(() =>
  reportInput.value !== null && (running.value || JSON.stringify(reportInput.value) !== JSON.stringify(toSimulationBody(draft.value))),
)
const formatRate = new Intl.NumberFormat('zh-CN', { style: 'percent', maximumFractionDigits: 1 }).format
const fieldGroups = [
  { name: 'rain', label: '降雨条件', fields: ['rain_base_mm', 'rain_spread_mm'] },
  { name: 'scenario', label: '情景设置', fields: ['scenarios', 'rounds'] },
  { name: 'budget', label: '预算与复现', fields: ['token_budget'] },
] as const

const matrixColumns = [
  { title: '降雨分档 (mm/10min)', dataIndex: 'rain_bucket_mm', key: 'rain_bucket_mm' },
  { title: '情景数', dataIndex: 'scenarios', key: 'scenarios' },
  { title: '平均触发率', dataIndex: 'mean_secondary_rate', key: 'mean_secondary_rate' },
  { title: '最高触发率', dataIndex: 'max_secondary_rate', key: 'max_secondary_rate' },
]

const comparisonColumns = [
  { title: '预案', dataIndex: 'playbook', key: 'playbook' },
  { title: '缓解系数', dataIndex: 'mitigation', key: 'mitigation' },
  { title: '平均触发率', dataIndex: 'mean_secondary_rate', key: 'mean_secondary_rate' },
  { title: '暴露度代理', dataIndex: 'exposure_proxy', key: 'exposure_proxy' },
]

/** 时间线里每个角色一票一列：列跟着报告的在场角色走，写死四列会在换角色清单时错位。 */
const timelineColumns = computed(() => {
  const roles = report.value?.roles.roles ?? []
  return [
    { title: '轮次', dataIndex: 'round', key: 'round' },
    { title: '雨强 (mm)', dataIndex: 'rain_mm', key: 'rain_mm' },
    { title: '泥位 (cm)', dataIndex: 'mudlevel_cm', key: 'mudlevel_cm' },
    ...roles.map((role) => ({ title: roleLabel(role), key: `role:${role.key}` })),
    { title: '本轮生效', dataIndex: 'applied_playbook', key: 'applied_playbook' },
    { title: '触发概率', dataIndex: 'trigger_probability', key: 'trigger_probability' },
  ]
})

const policyTone = computed(() => {
  const roles = report.value?.roles
  if (roles === undefined) return 'idle'
  if (roles.policy !== 'llm') return 'idle'
  return roles.fallbacks > 0 ? 'warn' : 'ok'
})

/** 那一格该说哪句话：把"该问几次、实问几次、几次是回落"翻成人话，但不改数。 */
const policySentence = computed(() => {
  const roles = report.value?.roles
  if (roles === undefined) return ''
  const head = roles.policy === 'llm' ? '角色档位：LLM 内环' : '角色档位：确定性规则（离线可复算）'
  const votes = `应问 ${roles.asked} 次、实问 ${roles.calls} 次、回落 ${roles.fallbacks} 次`
  if (roles.note !== undefined && roles.note !== '') return `${head}｜${votes}｜${roles.note}`
  // 两档都把票数写出来：规则档不报数就等于把"角色到底被问过没有"这一格藏进语气里
  return roles.policy === 'llm' ? `${head}｜${votes}` : `${head}｜${votes}｜票面即声明预案，不调模型`
})

const comparisonRows = computed(() => (report.value?.playbook_comparison ?? []).map((row) => ({ ...row, key: row.playbook })))
const matrixRows = computed(() =>
  (report.value?.probability_matrix ?? []).map((row, index) => ({ ...row, key: `${row.rain_bucket_mm}-${index}` })),
)

async function run(): Promise<void> {
  if (running.value) return
  const body = toSimulationBody(draft.value)
  const blocked = preflightSimulation(body)
  if (blocked !== '') {
    failure.value = blocked
    message.error(blocked)
    return
  }
  running.value = true
  failure.value = null
  draft.value = { ...body }
  try {
    report.value = await runSimulation(body)
    reportInput.value = body
    startedAt.value = new Date().toISOString()
  } catch (error) {
    failure.value = describeSimulationError(error)
    message.error(failure.value)
  } finally {
    running.value = false
  }
}
</script>

<template>
  <div class="simulation-page">
    <PageHero surface title="推演沙盒" badge="演示数据" caption="调整降雨、情景数量和预案，比较模型给出的触发概率。">
      <template #stats>
        <div class="stat-cell">
          <a-statistic title="本次生效情景" :value="report === null ? '—' : report.cost.effective_scenarios" />
        </div>
        <div class="stat-cell">
          <a-statistic title="推演轮数" :value="report === null ? '—' : report.timeline.length" />
        </div>
        <div class="stat-cell" :class="`is-${policyTone}`">
          <a-statistic title="角色档位" :value="report === null ? '—' : report.roles.policy === 'llm' ? '模型协同' : '规则推演'" />
        </div>
      </template>
    </PageHero>

    <a-card class="ops-surface" size="small" title="配置推演">
      <a-form layout="vertical" class="sim-form" data-testid="sim-form" @finish="run">
        <fieldset v-for="group in fieldGroups" :key="group.name" :disabled="running" class="sim-fieldset">
          <legend>{{ group.label }}</legend>
          <div class="sim-fields">
            <a-form-item v-for="field in group.fields" :key="field" :label="SIMULATION_FIELD_LABELS[field]">
              <a-input-number
                v-model:value="draft[field]"
                v-bind="SIMULATION_LIMITS[field]"
                :controls="false"
                :disabled="running"
                :aria-label="SIMULATION_FIELD_LABELS[field]"
                :placeholder="String(SIMULATION_DEFAULTS[field])"
                :addon-after="group.name === 'rain' ? 'mm' : undefined"
              />
            </a-form-item>
            <a-form-item v-if="group.name === 'scenario'" label="预案档位">
              <a-select v-model:value="draft.playbook" :options="PLAYBOOK_ACTIONS.map((item) => ({ value: item, label: item }))" :disabled="running" aria-label="预案档位" />
            </a-form-item>
            <a-form-item v-if="group.name === 'budget'" label="随机种子">
              <a-input-number v-model:value="draft.seed" :controls="false" :disabled="running" :placeholder="String(SIMULATION_DEFAULTS.seed)" aria-label="随机种子" />
            </a-form-item>
          </div>
        </fieldset>
      </a-form>
      <div class="sim-submit">
        <p class="sim-hint">
          留空字段采用占位符显示的系统默认值。预算不足时，报告会列出实际推演的情景数量。
        </p>
        <a-button type="primary" :loading="running" :disabled="running" data-testid="sim-run" @click="run">
          <template #icon><PlayCircleOutlined aria-hidden="true" /></template>
          发起推演
        </a-button>
      </div>
      <p v-if="failure !== null" class="sim-error" role="alert" data-testid="sim-error">{{ failure }}</p>
    </a-card>

    <div v-if="report === null" class="ops-record sim-empty" data-testid="sim-empty" role="status">
      {{ running ? '正在生成推演报告…' : '尚未推演。设置参数后点击「发起推演」。' }}
    </div>

    <template v-else>
      <a-card class="ops-surface sim-results" size="small" title="推演报告" data-testid="sim-cost">
        <p v-if="reportIsPrevious" class="sim-hint">以下是上一次完成的报告。</p>
        <a-alert v-if="report.cost.downscaled" type="warning" show-icon message="情景数被预算降档">
          <template #description>
            请求 {{ report.cost.requested_scenarios }} 条，按令牌预算只跑得起 {{ report.cost.effective_scenarios }} 条——
            下面那张矩阵是降档后的样本，条数比声明的少。
          </template>
        </a-alert>
        <a-alert v-else type="info" show-icon :message="`K=${report.cost.effective_scenarios} 条情景全部跑满，未降档`" />
        <p class="sim-note" data-testid="sim-note">{{ report.note }}</p>
        <p class="sim-quiet" v-if="reportInput">
          {{ formatOperatingTime(startedAt) }}（UTC+8） · {{ reportInput.playbook }} · 基准降雨 {{ reportInput.rain_base_mm }} mm · 种子 {{ reportInput.seed }}
        </p>
      </a-card>

      <a-card class="ops-surface sim-results" size="small" title="角色决策" data-testid="sim-roles">
        <p class="sim-policy">{{ policySentence }}</p>
        <a-table
          :data-source="report.roles.roles.map((role, index) => ({ ...role, key: `${role.key}-${index}` }))"
          :columns="[
            { title: '角色', dataIndex: 'title', key: 'title' },
            { title: '立场', dataIndex: 'concern', key: 'concern' },
          ]"
          :pagination="false"
          size="small"
        />
        <p class="sim-quiet">本轮实际出现过的票面：{{ report.roles.distinct_actions.join(' / ') || '（无）' }}</p>
      </a-card>

      <a-card class="ops-surface sim-results" size="small" title="次生灾害触发概率">
        <a-table :data-source="matrixRows" :columns="matrixColumns" :pagination="false" size="small" data-testid="sim-matrix">
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'mean_secondary_rate' || column.key === 'max_secondary_rate'">{{ formatRate(record[column.key]) }}</template>
          </template>
        </a-table>
      </a-card>

      <a-card class="ops-surface sim-results" size="small" title="推演时间线">
        <ul v-if="compact" class="ops-list" data-testid="sim-timeline">
          <li v-for="round in report.timeline" :key="round.round" class="ops-record">
            <div class="ops-record__head"><span>第 {{ round.round }} 轮 · {{ round.applied_playbook }}</span><span>{{ formatRate(round.trigger_probability) }}</span></div>
            <div class="ops-record__meta">雨强 {{ round.rain_mm }} mm · 泥位 {{ round.mudlevel_cm }} cm</div>
            <details class="sim-votes">
              <summary>查看各角色选择</summary>
              <ul><li v-for="role in report.roles.roles" :key="role.key">{{ roleLabel(role) }} · {{ round.actions[role.key] ?? '未记录' }}</li></ul>
            </details>
          </li>
        </ul>
        <a-table v-else :data-source="report.timeline" :columns="timelineColumns" :pagination="false" size="small" row-key="round" data-testid="sim-timeline">
          <template #bodyCell="{ column, record }">
            <template v-if="String(column.key).startsWith('role:')">
              {{ record.actions[String(column.key).slice(5)] ?? '未记录' }}
            </template>
            <template v-else-if="column.key === 'trigger_probability'">{{ formatRate(record.trigger_probability) }}</template>
          </template>
        </a-table>
      </a-card>

      <a-card class="ops-surface sim-results" size="small" title="同一雨强下的预案对比">
        <a-table :data-source="comparisonRows" :columns="comparisonColumns" :pagination="false" size="small" data-testid="sim-comparison">
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'mean_secondary_rate'">{{ formatRate(record.mean_secondary_rate) }}</template>
          </template>
        </a-table>
      </a-card>
    </template>
  </div>
</template>

<style scoped>
.sim-form {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: var(--aeg-space-s3);
}
.sim-fieldset {
  min-width: 0;
  margin: 0;
  padding: var(--aeg-space-s3);
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-lg);
  background: var(--aeg-color-overview-tile);
}
.sim-fieldset legend {
  width: auto;
  padding-inline: var(--aeg-space-s1);
  color: var(--aeg-color-text);
  font-size: var(--aeg-font-base);
  font-weight: var(--aeg-weight-semibold);
}
.sim-fields {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--aeg-space-s2);
}
.sim-fields :deep(.ant-form-item) {
  min-width: 0;
  margin: 0;
}
.sim-fields :deep(:is(.ant-input-number, .ant-input-number-group-wrapper, .ant-select)) {
  width: 100%;
}
.sim-fieldset:nth-child(2) .sim-fields :deep(.ant-form-item:last-child) {
  grid-column: 1 / -1;
}
.sim-results,
.sim-empty {
  margin-top: var(--aeg-space-s4);
}
.sim-submit {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  align-items: center;
  gap: var(--aeg-space-s2);
  margin-top: var(--aeg-space-s3);
}
.sim-submit .sim-hint {
  margin: 0;
}
.sim-submit :deep(.ant-btn) {
  min-height: 38px;
}
.sim-votes {
  margin-top: var(--aeg-space-s2);
  color: var(--aeg-color-text-secondary);
}
.sim-votes summary {
  cursor: pointer;
  color: var(--aeg-color-brand-hover);
}
.sim-results :deep(.ant-table-cell) {
  overflow-wrap: anywhere;
}
.simulation-page {
  container: simulation / inline-size;
}
@container simulation (max-width: 840px) {
  .sim-form {
    grid-template-columns: minmax(0, 1fr);
  }
}
@container simulation (max-width: 520px) {
  .simulation-page .stat-cell {
    min-width: 0;
    padding-inline: var(--aeg-space-s1);
  }
  .simulation-page :deep(.page-head__stats) {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    padding: var(--aeg-space-s2);
    gap: var(--aeg-space-s2);
  }
  .simulation-page :deep(.ant-statistic-title) {
    font-size: var(--aeg-font-sm);
  }
  .simulation-page :deep(.ant-statistic-content) {
    font-size: var(--aeg-font-md);
  }
}
.sim-hint {
  margin: var(--aeg-space-s2) 0 0;
  color: var(--aeg-color-text-muted);
  font-size: var(--aeg-font-sm);
}
.sim-error {
  margin: var(--aeg-space-s2) 0 0;
  padding: var(--aeg-space-s1) var(--aeg-space-s2);
  border: 1px solid var(--aeg-tag-red-line);
  border-radius: var(--aeg-radius-sm);
  background: var(--aeg-tag-red-bg);
  color: var(--aeg-tag-red-text);
  font-size: var(--aeg-font-sm);
}
.sim-note {
  margin: var(--aeg-space-s2) 0 0;
  font-size: var(--aeg-font-sm);
}
.sim-quiet {
  margin: var(--aeg-space-s1) 0 0;
  color: var(--aeg-color-text-muted);
  font-size: var(--aeg-font-xs);
}
.sim-policy {
  margin: 0 0 var(--aeg-space-s2);
  font-size: var(--aeg-font-sm);
}
.stat-cell.is-ok :deep(.ant-statistic-content) {
  color: var(--aeg-color-status-success);
}
.stat-cell.is-warn :deep(.ant-statistic-content) {
  color: var(--aeg-color-status-warning);
}
.stat-cell.is-idle :deep(.ant-statistic-content) {
  color: var(--aeg-color-text-muted);
}
</style>
