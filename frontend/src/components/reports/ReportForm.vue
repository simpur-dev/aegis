<script setup lang="ts">
/**
 * 人工上报入口（完善计划批次 B5）：群防群治/巡查上报的表单 + 提交回执。
 *
 * 为什么放在这里而不是"顺手在监测页写一段表单"：这条腿的价值在于**上报和监测事件走同一条链路**
 * （`container.py:544-550`），回执必须一次摊开——等级从哪一路来（`decided_by`）、
 * 每一路各自说了什么（`legs`）、哪里在降级（`degradations`）。只回一个"提交成功"
 * 就等于把平台已经说清楚的事实又咽回去了。
 *
 * 校验交给后端（`app.py:54-62` 的 `ReportIn`）：前端不另立一套判据，只把 422 的原文摊出来。
 * 唯一在这里做的判断是"只填了一个坐标"——后端会把整段坐标当没填（`app.py:431`），
 * 不提示就是骗用户"定位已发出"。
 */
import { message } from 'ant-design-vue'
import { computed, reactive, ref } from 'vue'

import type { LegFindingDto, ReportDraftDto, ReportOutcomeDto } from '@/api/reports'
import { describeValidationError, isParserUnavailable, legSourceLabel, reportsApi, toReportBody } from '@/api/reports'
import { HAZARD_LABELS, RISK_LABELS } from '@/api/types'

const props = withDefaults(
  defineProps<{ initialRegionCode?: string; initialReporter?: string; initialHazardHint?: string }>(),
  { initialRegionCode: '', initialReporter: '', initialHazardHint: '' },
)

const emit = defineEmits<{ (event: 'submitted', outcome: ReportOutcomeDto): void }>()

const form = reactive<ReportDraftDto>({
  reporter: props.initialReporter,
  region_code: props.initialRegionCode,
  hazard_hint: props.initialHazardHint,
  note: '',
  lat: null,
  lon: null,
})

const submitting = ref(false)
const outcome = ref<ReportOutcomeDto | null>(null)
/** 后端原文：422 的校验 detail 或 503 的装配事实，一律不改写。 */
const backendError = ref('')
const unavailable = ref(false)

/** 只填了半边坐标：后端会整段当"没定位"处理，这里如实提醒而不是静默丢弃。 */
const halfCoordinate = computed(() => {
  const hasLat = typeof form.lat === 'number' && Number.isFinite(form.lat)
  const hasLon = typeof form.lon === 'number' && Number.isFinite(form.lon)
  return hasLat !== hasLon
})

function levelLabel(level: number | null): string {
  if (level === null) return '未定级'
  return `${RISK_LABELS[level as keyof typeof RISK_LABELS] ?? level}（${level} 级）`
}

function legLevel(finding: LegFindingDto): string {
  return finding.risk_level === null ? '未给出等级' : levelLabel(finding.risk_level)
}

async function submit(): Promise<void> {
  submitting.value = true
  backendError.value = ''
  unavailable.value = false
  try {
    const result = await reportsApi.submit(toReportBody(form))
    outcome.value = result
    message.success(`上报已进链路：${result.chain.trace_id}`)
    emit('submitted', result)
  } catch (error) {
    outcome.value = null
    if (isParserUnavailable(error)) {
      unavailable.value = true
      backendError.value = error instanceof Error ? error.message : String(error)
    } else {
      const detail = describeValidationError(error)
      backendError.value = detail || (error instanceof Error ? `上报失败：${error.message}` : '上报失败')
    }
  } finally {
    submitting.value = false
  }
}

function reset(): void {
  outcome.value = null
  backendError.value = ''
  unavailable.value = false
}
</script>

<template>
  <div class="report-form">
    <a-alert
      v-if="unavailable"
      type="error"
      show-icon
      banner
      message="解析腿未装配：上报入口当前不可用"
      :description="backendError"
      data-testid="report-unavailable"
    />

    <a-form layout="vertical" data-testid="report-fields">
      <a-row :gutter="12">
        <a-col :span="12">
          <a-form-item label="上报人（2..64 字，后端校验）">
            <a-input v-model:value="form.reporter" placeholder="如：巡护员扎西" data-testid="field-reporter" />
          </a-form-item>
        </a-col>
        <a-col :span="12">
          <a-form-item label="区划代码（6..24 位大写字母/数字）">
            <a-input v-model:value="form.region_code" placeholder="如：540121" data-testid="field-region" />
          </a-form-item>
        </a-col>
      </a-row>
      <a-form-item label="灾种提示（可空，仅辅助规则词表）">
        <a-input v-model:value="form.hazard_hint" placeholder="如：泥石流" data-testid="field-hazard-hint" />
      </a-form-item>
      <a-form-item label="险情描述（4..2000 字，进三路融合解析）">
        <a-textarea v-model:value="form.note" :rows="3" placeholder="如：24 小时累计降雨 95 毫米，沟道泥位抬升 1.2 米" data-testid="field-note" />
      </a-form-item>
      <a-row :gutter="12">
        <a-col :span="12">
          <a-form-item label="纬度（可选，只进证据链不改判据）">
            <a-input-number v-model:value="form.lat" style="width: 100%" :step="0.000001" data-testid="field-lat" />
          </a-form-item>
        </a-col>
        <a-col :span="12">
          <a-form-item label="经度（可选）">
            <a-input-number v-model:value="form.lon" style="width: 100%" :step="0.000001" data-testid="field-lon" />
          </a-form-item>
        </a-col>
      </a-row>
      <div v-if="halfCoordinate" class="hint" data-testid="coordinate-warning">
        只填了半边坐标：后端按「未定位」处理（app.py:431），这一条不会带上经纬度证据。
      </div>
      <a-space>
        <a-button type="primary" :loading="submitting" data-testid="report-submit" @click="submit">提交上报</a-button>
        <a-button data-testid="report-reset" @click="reset">清空回执</a-button>
      </a-space>
    </a-form>

    <p v-if="backendError && !unavailable" class="backend-error" data-testid="report-error">{{ backendError }}</p>

    <div v-if="outcome" class="outcome" data-testid="report-outcome">
      <a-space wrap>
        <a-tag color="red" data-testid="outcome-level">{{ levelLabel(outcome.parse.risk_level) }}</a-tag>
        <a-tag data-testid="outcome-confidence">置信 {{ (outcome.parse.confidence * 100).toFixed(0) }}%</a-tag>
        <a-tag color="blue" data-testid="outcome-decided-by">
          定级来源：{{ legSourceLabel(outcome.parse.decided_by) }}
        </a-tag>
        <a-tag color="volcano" data-testid="outcome-hazard">{{ HAZARD_LABELS[outcome.parse.hazard_type] ?? outcome.parse.hazard_type }}</a-tag>
        <a-tag :color="outcome.human_review_required ? 'red' : 'green'" data-testid="outcome-review">
          {{ outcome.human_review_required ? '需人工核签' : '无需人工核签' }}
        </a-tag>
        <a-tag data-testid="outcome-intake">接入 {{ (outcome.intake_seconds * 1000).toFixed(0) }} ms</a-tag>
      </a-space>

      <div class="chain" data-testid="outcome-chain">
        链路 {{ outcome.chain.trace_id }} ｜ 预警 {{ outcome.chain.warning_id ?? '未生成' }} ｜
        任务单元 {{ outcome.chain.task_units.length }} 个 ｜
        感知段 {{ outcome.chain.stages[0]?.note || '—' }}
      </div>

      <div class="legs" data-testid="outcome-legs">
        <div v-for="finding in outcome.parse.legs" :key="finding.leg" class="leg" :data-testid="`leg-${finding.leg}`">
          <a-tag :color="finding.used ? 'green' : 'default'">{{ finding.used ? '采纳' : '未采纳' }}</a-tag>
          <span class="leg-name">{{ legSourceLabel(finding.leg) }}</span>
          <span class="leg-fact">等级={{ legLevel(finding) }}</span>
          <span class="leg-fact">置信={{ (finding.confidence * 100).toFixed(0) }}%</span>
          <span class="leg-rationale">{{ finding.rationale }}</span>
        </div>
      </div>

      <ul v-if="outcome.parse.conflicts.length" class="verbatim" data-testid="outcome-conflicts">
        <li v-for="line in outcome.parse.conflicts" :key="line">{{ line }}</li>
      </ul>
      <ul v-if="outcome.parse.degradations.length" class="verbatim" data-testid="outcome-degradations">
        <li v-for="line in outcome.parse.degradations" :key="line">{{ line }}</li>
      </ul>
      <ul v-if="outcome.chain.errors.length" class="verbatim" data-testid="chain-errors">
        <li v-for="line in outcome.chain.errors" :key="line">{{ line }}</li>
      </ul>
    </div>
  </div>
</template>

<style scoped>
.hint {
  color: #d46b08;
  font-size: 12px;
  margin-bottom: 8px;
}
.backend-error {
  color: #cf1322;
  font-size: 13px;
  margin-top: 8px;
  white-space: pre-wrap;
}
.outcome {
  margin-top: 12px;
  border-top: 1px solid rgba(0, 0, 0, 0.08);
  padding-top: 10px;
}
.chain {
  color: rgba(0, 0, 0, 0.65);
  font-size: 12px;
  margin-top: 8px;
}
.legs {
  margin-top: 8px;
}
.leg {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: baseline;
  padding: 4px 0;
  border-bottom: 1px solid rgba(0, 0, 0, 0.06);
  font-size: 12px;
}
.leg-name {
  font-weight: 600;
}
.leg-fact {
  color: rgba(0, 0, 0, 0.55);
}
.leg-rationale {
  color: rgba(0, 0, 0, 0.65);
  flex: 1;
  min-width: 160px;
}
.verbatim {
  margin: 8px 0 0;
  padding-left: 20px;
  color: #d46b08;
  font-size: 12px;
}
</style>
