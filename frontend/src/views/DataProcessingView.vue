<script setup lang="ts">
import {
  ApiOutlined, CheckCircleOutlined, DatabaseOutlined, DownOutlined, ExperimentOutlined, FilterOutlined,
  OrderedListOutlined, ReloadOutlined, SendOutlined, WarningOutlined,
} from '@ant-design/icons-vue'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import api, { ApiError } from '@/api/client'
import type { ProcessingDetail, ProcessingList, ProcessingStageKey, ProcessingStatus } from '@/api/types'
import PageHero from '@/components/PageHero.vue'
import ProcessingPayload from '@/components/ProcessingPayload.vue'
import { formatOperatingTime, operatingClock } from '@/utils/clock'

const stageInfo = {
  collect: { label: '采集', icon: ApiOutlined, note: '连接器并行读取各自的数据源。单源超时或解析失败，其他源仍可继续。' },
  normalize: { label: '标准化', icon: FilterOutlined, note: '连接器解析原始字段，平台校验有限数值和带时区的时间戳。质量标记随读数保留。' },
  store: { label: '写入查询视图', icon: DatabaseOutlined, note: '标准读数写入平台查询视图，供监测页和下游读取。时延在读数可查时计算。' },
  durability: { label: '数据库提交', icon: CheckCircleOutlined, note: 'PostgreSQL 在后台独立提交。可查看本批入队、提交、重试和丢弃结果。' },
  publish: { label: '事件投递', icon: SendOutlined, note: '逐条投递遥测事件。成功样本含消息 ID，单条投递失败会单独记录。' },
  analytics: { label: '分析旁路', icon: ExperimentOutlined, note: '已配置的分析消费者接收遥测事实，独立于查询视图和事件投递。' },
}
const statusLabels: Record<ProcessingStatus, string> = {
  ok: '完成', partial: '有异常', failed: '失败', empty: '无数据', skipped: '未执行', pending: '等待提交',
}
const sourceLabels: Record<string, string> = {
  sim_field_nodes: '监测场景模拟器', mqtt: 'MQTT 站端遥测', weather_api: '气象接口',
  precip_hourly: '逐小时降水 CSV', precip_daily: '逐日降水 CSV',
}

const history = ref<ProcessingList | null>(null)
const detail = ref<ProcessingDetail | null>(null)
const selectedId = ref('')
const selectedStage = ref<ProcessingStageKey>('collect')
const onlyIssues = ref(false)
const followLatest = ref(true)
const updating = ref(false)
const listError = ref('')
const detailError = ref('')
const visibleRounds = computed(() => (history.value?.items ?? []).filter(
  (round) => !onlyIssues.value || round.status === 'partial' || round.status === 'failed',
))
const stage = computed(() => detail.value?.stages.find((step) => step.key === selectedStage.value))
const stageOrder: ProcessingStageKey[] = ['collect', 'normalize', 'store', 'publish', 'durability', 'analytics']
const rail = computed(() => stageOrder.flatMap((key) => detail.value?.stages.find((step) => step.key === key) ?? []))
const sharedTime = computed(() => detail.value ? formatOperatingTime(detail.value.started_at) : '')
const isSourceStage = computed(() => selectedStage.value === 'collect' || selectedStage.value === 'normalize')
let timer: ReturnType<typeof setInterval>
let listRequest: AbortController | undefined
let detailRequest: AbortController | undefined

async function inspectRound(id: string): Promise<void> {
  if (!id) return
  detailRequest?.abort()
  const request = new AbortController()
  detailRequest = request
  if (selectedId.value !== id) detail.value = null
  selectedId.value = id
  detailError.value = ''
  try {
    const result = await api.processingRound(id, request.signal)
    if (!request.signal.aborted) detail.value = result
  } catch (error) {
    if (request.signal.aborted) return
    detailError.value = error instanceof ApiError && error.status === 404
      ? '此批次已移出记录窗口，请查看其他批次。'
      : '读取批次明细失败，请刷新后重试。'
  } finally {
    if (detailRequest === request) detailRequest = undefined
  }
}

async function refresh(): Promise<void> {
  if (listRequest) return
  const request = new AbortController()
  listRequest = request
  updating.value = true
  try {
    const result = await api.processingRounds(request.signal)
    if (request.signal.aborted) return
    history.value = result
    listError.value = ''
    const id = followLatest.value ? visibleRounds.value[0]?.id ?? '' : selectedId.value
    if (id) await inspectRound(id)
    else if (followLatest.value) clearSelection()
  } catch (error) {
    if (request.signal.aborted) return
    listError.value = error instanceof ApiError && error.status === 404
      ? '当前后端没有提供处理记录接口，请更新后端服务。'
      : '读取处理记录失败，请检查后端连接或服务令牌。'
  } finally {
    if (listRequest === request) listRequest = undefined
    updating.value = false
  }
}

function clearSelection(): void {
  detailRequest?.abort()
  selectedId.value = ''
  detail.value = null
  detailError.value = ''
}

function selectRound(id: string): void {
  followLatest.value = false
  void inspectRound(id)
}

function goLatest(): void {
  followLatest.value = true
  const newest = visibleRounds.value[0]
  if (newest) void inspectRound(newest.id)
  else clearSelection()
}

function switchFilter(showIssues: boolean): void {
  onlyIssues.value = showIssues
  goLatest()
}

onMounted(() => {
  void refresh()
  timer = setInterval(() => {
    if (document.visibilityState === 'visible') void refresh()
  }, 5_000)
})

onBeforeUnmount(() => {
  clearInterval(timer)
  listRequest?.abort()
  detailRequest?.abort()
})
</script>

<template>
  <div class="processing">
    <PageHero
      surface title="数据处理流水线"
      caption="按批次检查遥测从原始输入到可查读数、数据库提交与事件投递的过程。"
    >
      <template #actions>
        <span v-if="history && !listError" class="live-mark">
          <span aria-hidden="true" />每 5 秒刷新
        </span>
        <a-button :loading="updating" data-testid="processing-refresh" @click="refresh">
          <template #icon><ReloadOutlined aria-hidden="true" /></template>
          刷新
        </a-button>
      </template>
    </PageHero>

    <p v-if="listError" class="processing-error" role="alert">
      {{ listError }}<template v-if="history"> 当前保留上次成功读取的记录。</template>
    </p>
    <div class="processing-grid">
      <aside class="processing-pane batch-pane" aria-labelledby="processing-rounds">
        <div class="batch-heading">
          <h2 id="processing-rounds"><OrderedListOutlined aria-hidden="true" /> 接入批次</h2>
          <button type="button" class="latest-button" :aria-pressed="followLatest" @click="goLatest">跟随最新</button>
        </div>
        <div class="batch-filter" role="group" aria-label="批次筛选">
          <button type="button" :aria-pressed="!onlyIssues" @click="switchFilter(false)">全部</button>
          <button type="button" :aria-pressed="onlyIssues" @click="switchFilter(true)">有异常</button>
        </div>
        <select
          class="batch-select" aria-label="接入批次" :value="selectedId" :disabled="!visibleRounds.length"
          @change="selectRound(($event.target as HTMLSelectElement).value)"
        >
          <option v-if="!visibleRounds.some((round) => round.id === selectedId)" :value="selectedId">
            {{ selectedId && detail ? formatOperatingTime(detail.started_at) : '暂无可选批次' }}
          </option>
          <option v-for="round in visibleRounds" :key="round.id" :value="round.id">
            {{ formatOperatingTime(round.started_at) }} · {{ statusLabels[round.status] }} · {{ round.readings }} 条
          </option>
        </select>
        <ul class="batch-list">
          <li v-for="round in visibleRounds" :key="round.id">
            <button
              type="button" class="batch-row" :aria-pressed="round.id === selectedId" :data-testid="`processing-batch-${round.id}`"
              :title="formatOperatingTime(round.started_at)"
              @click="selectRound(round.id)"
            >
              <span class="batch-row__title">
                <strong>{{ operatingClock(round.started_at) }}</strong>
                <span class="processing-status" :data-tone="round.status">{{ statusLabels[round.status] }}</span>
              </span>
              <span class="batch-row__meta">{{ round.sources }} 个源 · {{ round.readings }} 条可查 · {{ round.duration_ms }} ms</span>
            </button>
          </li>
        </ul>
        <p v-if="!visibleRounds.length" class="batch-empty">{{ onlyIssues ? '当前记录中没有异常批次' : '暂无接入记录' }}</p>
        <p v-if="history" class="batch-foot">当前服务进程最近 {{ history.capacity }} 批</p>
      </aside>

      <div class="processing-workspace">
        <div v-if="detailError" class="processing-error" role="alert">{{ detailError }}</div>
        <section v-if="detail && stage" class="processing-pane trace-pane" aria-labelledby="processing-trace">
          <div class="trace-heading">
            <div>
              <h2 id="processing-trace">批次过程 <span class="processing-status" :data-tone="detail.status">{{ statusLabels[detail.status] }}</span></h2>
              <p>{{ sharedTime }} <span aria-hidden="true">·</span> <code>{{ detail.trace_id }}</code></p>
            </div>
            <span class="trace-count"><strong>{{ detail.published }}</strong> / {{ detail.readings }} 条已投递</span>
          </div>

          <ol class="stage-rail" aria-label="处理步骤">
            <li v-for="(step, index) in rail" :key="step.key" :class="{ 'stage-branch': index > 3 }">
              <button type="button" :aria-pressed="stage.key === step.key" @click="selectedStage = step.key">
                <span class="stage-rail__top" aria-hidden="true">
                  <component :is="stageInfo[step.key].icon" />
                  <span v-if="index < 4">{{ String(index + 1).padStart(2, '0') }}</span>
                  <span v-else>{{ step.key === 'durability' ? '异步' : '可选' }}</span>
                </span>
                <strong>{{ stageInfo[step.key].label }}</strong>
                <span class="stage-state" :data-tone="step.status"><i aria-hidden="true" />{{ statusLabels[step.status] }}</span>
              </button>
            </li>
          </ol>

          <section :key="stage.key" class="stage-detail" :aria-label="stageInfo[stage.key].label">
            <div class="stage-heading">
              <div>
                <h3>{{ stageInfo[stage.key].label }}</h3>
                <p>{{ stageInfo[stage.key].note }}</p>
              </div>
              <span v-if="stage.duration_ms !== null" class="stage-duration"
                :title="stage.key === 'normalize' ? '平台校验耗时，连接器解析耗时见分源记录' : undefined"
              >{{ stage.duration_ms }} ms</span>
            </div>
            <div class="stage-payloads">
              <div class="payload-card">
                <h4>输入</h4>
                <ProcessingPayload :fields="stage.inputs" :samples="stage.input_samples" sample-label="查看输入样本" />
              </div>
              <div class="payload-card">
                <h4>结果</h4>
                <ProcessingPayload :fields="stage.results" :samples="stage.result_samples" sample-label="查看结果样本" />
                <p v-if="stage.key === 'durability' && stage.status === 'skipped'" class="payload-note">当前批次未进入 PostgreSQL 提交队列。</p>
              </div>
            </div>
            <div class="issue-card" :class="{ 'has-issues': stage.issues.length }">
              <h4><WarningOutlined aria-hidden="true" /> 异常</h4>
              <ul v-if="stage.issues.length">
                <li v-for="(issue, index) in stage.issues" :key="`${issue.code}-${index}`">
                  <div class="issue-line">
                    <strong>{{ issue.message }}</strong><span>{{ issue.count }} 次</span>
                  </div>
                  <code class="issue-code">{{ issue.code }}</code>
                  <ProcessingPayload v-if="issue.samples.length" :samples="issue.samples" sample-label="查看异常样本" />
                </li>
              </ul>
              <p v-else>{{ stage.status === 'skipped' ? '此步未执行' : '当前步骤未记录异常' }}</p>
            </div>
          </section>

          <section v-if="isSourceStage" class="source-list" aria-labelledby="processing-sources">
            <h3 id="processing-sources">分源处理记录</h3>
            <details v-for="source in detail.source_details" :key="source.name" class="source-entry">
              <summary>
                <span class="source-title">
                  <strong>{{ sourceLabels[source.name] ?? source.name }}</strong>
                  <code>{{ source.name }}</code>
                </span>
                <span class="source-result">{{ source.normalized }} 条 · {{ source.duration_ms }} ms</span>
                <span class="processing-status" :data-tone="source.status">{{ statusLabels[source.status] }}</span>
                <DownOutlined class="source-chevron" aria-hidden="true" />
              </summary>
              <div class="source-body">
                <div class="stage-payloads">
                  <div class="payload-card">
                    <h4>源端输入</h4>
                    <ProcessingPayload :fields="{ ...source.inputs, raw_items: source.received }" :samples="source.input_samples" sample-label="查看源端样本" />
                  </div>
                  <div class="payload-card">
                    <h4>标准化结果</h4>
                    <ProcessingPayload :fields="{ normalized: source.normalized }" :samples="source.result_samples" sample-label="查看标准读数" />
                  </div>
                </div>
                <p v-for="issue in source.issues" :key="issue.code" class="source-issue">{{ issue.message }} · {{ issue.count }} 次</p>
              </div>
            </details>
          </section>
        </section>

        <section v-else-if="!detailError" class="processing-pane trace-empty">
          <ApiOutlined aria-hidden="true" />
          <h2>{{ listError && !history ? '处理记录暂不可用' : selectedId ? '正在读取批次过程' : onlyIssues ? '没有符合筛选的批次' : '等待遥测接入' }}</h2>
          <p v-if="!selectedId && !onlyIssues && history">
            {{ history.sources.length ? `已配置 ${history.sources.length} 个数据源，首个批次完成后会显示处理记录。` : '当前服务未启用遥测数据源。' }}
          </p>
        </section>
      </div>
    </div>
  </div>
</template>

<style scoped>
.processing {
  display: grid;
  gap: var(--aeg-space-s4);
  min-width: 0;
}
.processing :deep(.page-head) { margin: 0; }
.processing-grid {
  display: grid;
  grid-template-columns: minmax(208px, 252px) minmax(0, 1fr);
  align-items: start;
  gap: var(--aeg-space-s4);
}
.processing-workspace { min-width: 0; }
.processing-pane {
  min-width: 0;
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-xl);
  background: var(--aeg-color-overview-panel);
  box-shadow: inset 0 1px 0 var(--aeg-color-glass-border), var(--aeg-shadow-sm);
}
.processing h2, .processing h3, .processing h4 { margin: 0; color: var(--aeg-color-text); }
.processing h2 { font-size: var(--aeg-font-md); }
.processing h3 { font-size: var(--aeg-font-base); }
.processing h4 { font-size: var(--aeg-font-sm); }
.live-mark, .stage-state {
  display: inline-flex;
  align-items: center;
  gap: var(--aeg-space-s1);
  white-space: nowrap;
  font-size: var(--aeg-font-xs);
}
.live-mark > span, .stage-state i {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
}
.live-mark { color: var(--aeg-color-text-secondary); }
.live-mark > span { color: var(--aeg-color-status-success); }
.batch-pane, .trace-pane { padding: var(--aeg-space-s4); }
.batch-heading, .trace-heading, .stage-heading, .batch-row__title, .issue-line {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--aeg-space-s3);
  min-width: 0;
}
.batch-heading h2 { white-space: nowrap; }
.batch-heading h2 :deep(.anticon) { color: var(--aeg-color-overview-accent); }
.latest-button, .batch-filter button, .batch-row, .stage-rail button {
  font: inherit;
  color: var(--aeg-color-text);
  cursor: pointer;
}
.latest-button, .batch-filter button {
  min-height: 34px;
  padding: 0 var(--aeg-space-s2);
  border: 1px solid transparent;
  border-radius: var(--aeg-radius-md);
  background: transparent;
  color: var(--aeg-color-text-secondary);
  font-size: var(--aeg-font-xs);
  white-space: nowrap;
}
.latest-button[aria-pressed='true'], .batch-filter button[aria-pressed='true'] {
  border-color: var(--aeg-color-border);
  background: var(--aeg-color-brand-soft);
  color: var(--aeg-color-brand-hover);
}
.batch-filter {
  display: flex;
  gap: var(--aeg-space-s1);
  margin: var(--aeg-space-s3) 0;
}
.batch-list {
  display: grid;
  gap: var(--aeg-space-s1);
  max-height: min(510px, 55vh);
  padding: 0;
  margin: 0;
  overflow: auto;
  list-style: none;
}
.batch-row {
  width: 100%;
  display: grid;
  gap: var(--aeg-space-s1);
  min-height: 60px;
  padding: var(--aeg-space-s2);
  border: 1px solid transparent;
  border-radius: var(--aeg-radius-md);
  background: transparent;
  text-align: left;
}
.batch-row:hover, .batch-row[aria-pressed='true'] {
  border-color: var(--aeg-color-border);
  background: var(--aeg-color-sunken);
}
.batch-row[aria-pressed='true'] { border-inline-start: 2px solid var(--aeg-color-overview-accent); }
.batch-row strong, .trace-count strong {
  font-family: var(--aeg-font-mono);
  font-variant-numeric: tabular-nums;
}
.batch-row__meta, .batch-foot, .batch-empty, .trace-heading p, .stage-heading p, .trace-count, .issue-card p, .payload-note {
  margin: 0;
  color: var(--aeg-color-text-secondary);
  font-size: var(--aeg-font-xs);
}
.batch-foot { margin-top: var(--aeg-space-s3); }
.batch-select { display: none; }
.processing-status {
  display: inline-flex;
  padding: 2px var(--aeg-space-s2);
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-pill);
  color: var(--aeg-color-text-secondary);
  font-weight: var(--aeg-weight-semibold);
  font-size: var(--aeg-font-xs);
  white-space: nowrap;
}
[data-tone='ok'] { color: var(--aeg-color-status-success); }
[data-tone='partial'] { color: var(--aeg-color-status-warning); }
[data-tone='failed'] { color: var(--aeg-color-status-danger); }
[data-tone='pending'] { color: var(--aeg-color-brand-hover); }
.trace-heading { margin-bottom: var(--aeg-space-s4); }
.trace-heading h2 { display: flex; align-items: center; gap: var(--aeg-space-s2); }
.trace-heading p { margin-top: var(--aeg-space-s1); overflow-wrap: anywhere; }
.trace-count { padding: var(--aeg-space-s2); white-space: nowrap; }
.trace-count strong { color: var(--aeg-color-text); font-size: var(--aeg-font-lg); }
.stage-rail {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: var(--aeg-space-s2);
  padding: 0;
  margin: 0;
  list-style: none;
}
.stage-rail li { position: relative; min-width: 0; }
.stage-rail li:nth-child(-n+3)::after {
  position: absolute;
  top: 50%;
  left: 100%;
  width: var(--aeg-space-s2);
  border-top: 1px solid var(--aeg-color-border-strong);
  content: '';
}
.stage-rail .stage-branch:nth-child(5) { grid-column: 3; }
.stage-rail .stage-branch:nth-child(6) { grid-column: 4; }
.stage-rail .stage-branch::before {
  position: absolute;
  left: 50%;
  bottom: 100%;
  height: var(--aeg-space-s2);
  border-left: 1px dashed var(--aeg-color-border-strong);
  content: '';
}
.stage-rail button {
  width: 100%;
  min-width: 0;
  min-height: 84px;
  display: grid;
  justify-items: start;
  gap: var(--aeg-space-s1);
  padding: var(--aeg-space-s2);
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-lg);
  background: var(--aeg-color-sunken);
  text-align: left;
  transition: background-color var(--aeg-motion-base) ease, border-color var(--aeg-motion-base) ease;
}
.stage-rail .stage-branch button {
  min-height: 68px;
  border-style: dashed;
  background: color-mix(in srgb, var(--aeg-color-overview-tile) 80%, var(--aeg-color-sunken));
}
.stage-rail .stage-branch button[aria-pressed='true'] {
  background: var(--aeg-color-brand-soft);
}
.stage-rail button:hover, .stage-rail button[aria-pressed='true'] { border-color: var(--aeg-color-overview-accent); }
.stage-rail button[aria-pressed='true'] {
  background: linear-gradient(138deg, var(--aeg-color-brand-soft), var(--aeg-color-overview-tile));
  box-shadow: inset 0 1px 0 var(--aeg-color-glass-border);
}
.stage-rail button strong { font-size: var(--aeg-font-base); }
.stage-rail__top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  color: var(--aeg-color-text-secondary);
  font-family: var(--aeg-font-mono);
  font-size: var(--aeg-font-xs);
}
.stage-rail__top :deep(.anticon) { color: var(--aeg-color-brand-hover); font-size: 17px; }
.stage-state { color: var(--aeg-color-text-secondary); }
.stage-state[data-tone='ok'] { color: var(--aeg-color-status-success); }
.stage-state[data-tone='partial'] { color: var(--aeg-color-status-warning); }
.stage-state[data-tone='failed'] { color: var(--aeg-color-status-danger); }
.stage-state[data-tone='pending'] { color: var(--aeg-color-brand-hover); }
.stage-detail { margin-top: var(--aeg-space-s5); }
.stage-heading { align-items: start; margin-bottom: var(--aeg-space-s3); }
.stage-heading p { margin-top: var(--aeg-space-s1); }
.stage-duration {
  flex: none;
  color: var(--aeg-color-text-secondary);
  font-family: var(--aeg-font-mono);
  font-size: var(--aeg-font-xs);
}
.stage-payloads { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--aeg-space-s3); }
.payload-card, .issue-card {
  min-width: 0;
  padding: var(--aeg-space-s3);
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-lg);
  background: var(--aeg-color-overview-tile);
}
.payload-card h4, .issue-card h4 { margin-bottom: var(--aeg-space-s2); }
.issue-card { margin-top: var(--aeg-space-s3); }
.issue-card.has-issues { border-color: color-mix(in srgb, var(--aeg-color-status-warning) 45%, var(--aeg-color-border)); }
.issue-card.has-issues h4, .source-issue { color: var(--aeg-color-status-warning); }
.issue-card ul { padding: 0; margin: 0; list-style: none; }
.issue-card li + li { margin-top: var(--aeg-space-s2); }
.issue-line { font-size: var(--aeg-font-sm); }
.issue-line span { flex: none; color: var(--aeg-color-text-secondary); }
.issue-code { color: var(--aeg-color-text-secondary); font-size: var(--aeg-font-xs); }
.payload-note { margin-top: var(--aeg-space-s2); }
.source-list { display: grid; gap: var(--aeg-space-s2); margin-top: var(--aeg-space-s5); }
.source-entry { border: 1px solid var(--aeg-color-border); border-radius: var(--aeg-radius-lg); }
.source-entry summary {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto auto;
  align-items: center;
  gap: var(--aeg-space-s3);
  padding: var(--aeg-space-s2) var(--aeg-space-s3);
  min-height: 46px;
  cursor: pointer;
}
.source-title { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0 var(--aeg-space-s2); overflow-wrap: anywhere; }
.source-title code, .source-result { color: var(--aeg-color-text-secondary); font-size: var(--aeg-font-xs); }
.source-chevron { color: var(--aeg-color-text-secondary); font-size: var(--aeg-font-xs); }
.source-entry[open] .source-chevron { transform: rotate(180deg); }
.source-body { padding: 0 var(--aeg-space-s3) var(--aeg-space-s3); }
.source-issue { font-size: var(--aeg-font-sm); margin: var(--aeg-space-s2) 0 0; }
.processing-error {
  margin: 0 0 var(--aeg-space-s2);
  padding: var(--aeg-space-s3);
  border: 1px solid var(--aeg-color-status-danger);
  border-radius: var(--aeg-radius-lg);
  color: var(--aeg-color-status-danger);
  background: var(--aeg-color-overview-panel);
}
.trace-empty {
  padding: var(--aeg-space-s8) var(--aeg-space-s4);
  text-align: center;
  color: var(--aeg-color-text-secondary);
}
.trace-empty :deep(.anticon) { color: var(--aeg-color-overview-accent); font-size: var(--aeg-font-xl); }
.trace-empty h2 { margin-top: var(--aeg-space-s3); }
.trace-empty p { margin: var(--aeg-space-s2) 0 0; }
@media (max-width: 1020px) {
  .stage-rail button { min-height: 76px; }
}
@media (max-width: 760px) {
  .processing-grid { grid-template-columns: minmax(0, 1fr); gap: var(--aeg-space-s3); }
  .batch-list { display: none; }
  .batch-select {
    display: block;
    width: 100%;
    min-height: 40px;
    padding: var(--aeg-space-s2);
    border: 1px solid var(--aeg-color-border);
    border-radius: var(--aeg-radius-md);
    background: var(--aeg-color-sunken);
    color: var(--aeg-color-text);
    font: inherit;
  }
  .batch-foot { display: none; }
}
@media (max-width: 720px) {
  .processing :deep(.page-head__actions) {
    flex: 0 0 auto;
    margin-left: auto;
  }
  .live-mark { display: none; }
}
@media (max-width: 440px) {
  .batch-pane, .trace-pane { padding: var(--aeg-space-s3); }
  .stage-rail { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .stage-rail .stage-branch:nth-child(n) { grid-column: auto; }
  .stage-rail li:nth-child(2)::after { display: none; }
  .trace-heading { flex-wrap: wrap; gap: var(--aeg-space-s1); }
  .trace-count { padding: 0; }
  .stage-payloads { grid-template-columns: minmax(0, 1fr); }
  .source-entry summary { grid-template-columns: minmax(0, 1fr) auto auto; }
  .source-result { display: none; }
}
</style>
