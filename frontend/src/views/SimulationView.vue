<script setup lang="ts">
import { message } from 'ant-design-vue'
import { computed, ref } from 'vue'

import PageHero from '@/components/PageHero.vue'
import {
  PLAYBOOK_ACTIONS,
  SIMULATION_DEFAULTS,
  describeSimulationError,
  preflightSimulation,
  roleLabel,
  runSimulation,
  toSimulationBody,
  type SimulationReport,
} from '@/api/simulation'

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
  const body = toSimulationBody(draft.value)
  const blocked = preflightSimulation(body)
  if (blocked !== '') {
    failure.value = blocked
    message.error(blocked)
    return
  }
  running.value = true
  failure.value = null
  try {
    report.value = await runSimulation(body)
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
  <div>
    <PageHero title="推演沙盒" badge="演示" caption="多角色 × K 情景 × T 轮：数值由确定性模型推进，系数为未实测初值。">
      <template #actions>
        <a-button type="primary" :loading="running" data-testid="sim-run" @click="run">发起推演</a-button>
      </template>
      <template #stats>
        <div class="stat-cell">
          <a-statistic title="本次生效情景" :value="report === null ? '—' : report.cost.effective_scenarios" />
        </div>
        <div class="stat-cell">
          <a-statistic title="推演轮数" :value="report === null ? '—' : report.timeline.length" />
        </div>
        <div class="stat-cell" :class="`is-${policyTone}`">
          <a-statistic title="角色档位" :value="report === null ? '—' : report.roles.policy" />
        </div>
      </template>
    </PageHero>

    <a-card size="small" title="情景参数（全部由调用方给定，平台不臆造锚定遥测的默认）">
      <a-form layout="inline" class="sim-form" data-testid="sim-form">
        <a-form-item label="情景条数 K">
          <a-input-number v-model:value="draft.scenarios" :min="1" :max="64" />
        </a-form-item>
        <a-form-item label="轮数 T">
          <a-input-number v-model:value="draft.rounds" :min="1" :max="24" />
        </a-form-item>
        <a-form-item label="基准降雨 mm">
          <a-input-number v-model:value="draft.rain_base_mm" :min="0" :max="500" />
        </a-form-item>
        <a-form-item label="降雨浮动 mm">
          <a-input-number v-model:value="draft.rain_spread_mm" :min="0" :max="200" />
        </a-form-item>
        <a-form-item label="预案档位">
          <a-select v-model:value="draft.playbook" :options="PLAYBOOK_ACTIONS.map((item) => ({ value: item, label: item }))" style="width: 110px" />
        </a-form-item>
        <a-form-item label="令牌预算">
          <a-input-number v-model:value="draft.token_budget" :min="1" :max="10000000" :step="10000" />
        </a-form-item>
        <a-form-item label="随机种子">
          <a-input-number v-model:value="draft.seed" :controls="false" style="width: 130px" />
        </a-form-item>
      </a-form>
      <p class="sim-hint">
        超预算时后端自动降 K 并在下面写明"降到几"；同参数同种子逐键可复现。角色选档默认走确定性规则档，
        接 LLM 要部署侧开 <code>AEGIS_SIMULATION_ROLES_LLM</code>。
      </p>
      <p v-if="failure !== null" class="sim-error" data-testid="sim-error">这页现在跑不出报告：{{ failure }}</p>
    </a-card>

    <a-empty v-if="report === null" data-testid="sim-empty" description="尚未推演：上面填好参数再点「发起推演」。缺这一步时页面不显示任何概率数字。" style="margin-top: 16px" />

    <template v-else>
      <a-card size="small" title="成本与口径" style="margin-top: 12px" data-testid="sim-cost">
        <a-alert v-if="report.cost.downscaled" type="warning" show-icon message="情景数被预算降档">
          <template #description>
            请求 {{ report.cost.requested_scenarios }} 条，按令牌预算只跑得起 {{ report.cost.effective_scenarios }} 条——
            下面那张矩阵是降档后的样本，条数比声明的少。
          </template>
        </a-alert>
        <a-alert v-else type="info" show-icon :message="`K=${report.cost.effective_scenarios} 条情景全部跑满，未降档`" />
        <p class="sim-note" data-testid="sim-note">{{ report.note }}</p>
        <p class="sim-quiet">取到这份报告的时间：{{ startedAt }}</p>
      </a-card>

      <a-card size="small" title="角色档（谁在选、选了几次）" style="margin-top: 12px" data-testid="sim-roles">
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

      <a-card size="small" title="次生灾害触发概率矩阵" style="margin-top: 12px">
        <a-table :data-source="matrixRows" :columns="matrixColumns" :pagination="false" size="small" data-testid="sim-matrix" />
      </a-card>

      <a-card size="small" title="关键时间线（每轮四个角色各投一票，汇聚后生效）" style="margin-top: 12px">
        <a-table :data-source="report.timeline" :columns="timelineColumns" :pagination="false" size="small" row-key="round" data-testid="sim-timeline">
          <template #bodyCell="{ column, record }">
            <template v-if="String(column.key).startsWith('role:')">
              {{ record.actions[String(column.key).slice(5)] }}
            </template>
          </template>
        </a-table>
      </a-card>

      <a-card size="small" title="预案对比（同一强度下的反事实）" style="margin-top: 12px">
        <a-table :data-source="comparisonRows" :columns="comparisonColumns" :pagination="false" size="small" data-testid="sim-comparison" />
      </a-card>
    </template>
  </div>
</template>

<style scoped>
.sim-form {
  row-gap: var(--aeg-space-s2);
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
