<script setup lang="ts">
import { DownOutlined, ReloadOutlined, RightOutlined } from '@ant-design/icons-vue'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, useId } from 'vue'

import {
  fetchIntegrations,
  legLabel,
  legState,
  legStateLabel,
  visibleDegradation,
  visibleDetail,
  type IntegrationSnapshot,
} from '@/api/integrations'

const props = withDefaults(defineProps<{ refreshMs?: number }>(), { refreshMs: 15_000 })

const states = ['degraded', 'enabled', 'disabled'] as const
type LegState = (typeof states)[number]
const tagColors: Record<LegState, string> = { degraded: 'orange', enabled: 'green', disabled: 'default' }

const snapshot = ref<IntegrationSnapshot | null>(null)
const error = ref<string | null>(null)
const loading = ref(false)
const selectedName = ref<string | null>(null)
const expanded = ref<Partial<Record<LegState, boolean>>>({})
const listPanel = ref<HTMLElement | null>(null)
const detailPanel = ref<HTMLElement | null>(null)
const panelId = useId()
let timer: ReturnType<typeof setInterval> | null = null

const rows = computed(() => (snapshot.value?.items ?? []).map((row) => ({ row, state: legState(row) })))
const groups = computed(() => states.map((state) => ({
  state,
  entries: rows.value.filter((entry) => entry.state === state),
  open: expanded.value[state] ?? (state !== 'disabled' || rows.value.every((entry) => entry.state === 'disabled')),
})))
const selected = computed(() => rows.value.find(({ row }) => row.name === selectedName.value)
  ?? groups.value.find((group) => group.entries.length)?.entries[0]
  ?? null)

const details = computed(() => {
  const current = selected.value
  if (!current) return null
  const { row, state } = current
  const raw = visibleDetail(row)
  const issue = state === 'degraded' ? visibleDegradation(row)[0] : undefined
  const missingKnowledgeCredential = row.name === 'knowledge' &&
    (issue?.hint || issue?.value)?.includes('案例入库需要 LLM 凭据')
  const reason = missingKnowledgeCredential ? '缺少 LLM 凭据，案例入库暂不可用。'
    : issue?.value ?? (state === 'degraded' ? '后端未提供可展示的降级原因。' : null)
  const fallback = row.name === 'knowledge' ? row.detail.fallback_cases : undefined
  const recallBudget = row.detail.recall_budget_ms

  return {
    raw,
    reason,
    fallback: typeof fallback === 'number' && Number.isFinite(fallback) && fallback >= 0 ? fallback : null,
    recallBudget: typeof recallBudget === 'number' && Number.isFinite(recallBudget) && recallBudget >= 0 ? recallBudget : null,
  }
})

async function selectRow(name: string): Promise<void> {
  selectedName.value = name
  await nextTick()
  const list = listPanel.value
  const detail = detailPanel.value
  // 单列时详情排在清单下面，选中后直接将读者带到详情。
  if (list && detail && detail.offsetTop > list.offsetTop) detail.focus()
}

async function refresh(): Promise<void> {
  if (loading.value) return
  loading.value = true
  try {
    const result = await fetchIntegrations()
    if (!result.items.some((row) => row.name === selectedName.value)) selectedName.value = null
    snapshot.value = result
    error.value = null
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : String(reason)
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  void refresh()
  timer = setInterval(() => {
    if (document.visibilityState === 'visible') void refresh()
  }, props.refreshMs)
})

onBeforeUnmount(() => {
  if (timer !== null) clearInterval(timer)
})
</script>

<template>
  <a-card class="leg-panel" size="small" :loading="loading && !snapshot" data-testid="integration-panel">
    <template #title>
      <h2 class="leg-heading">可选子系统状态</h2>
    </template>
    <template #extra>
      <a-space size="small" wrap>
        <a-tag v-if="error" color="red" data-testid="panel-state">读不到装配事实</a-tag>
        <a-button size="small" :loading="loading" data-testid="integrations-refresh" @click="refresh">
          <ReloadOutlined v-if="!loading" aria-hidden="true" /> 刷新
        </a-button>
      </a-space>
    </template>

    <a-alert
      v-if="error"
      type="error"
      show-icon
      :message="snapshot ? `最近刷新失败，以下为上次成功读取的数据。${error}` : error"
      data-testid="panel-error"
      class="leg-error"
    />

    <template v-if="rows.length">
      <div class="leg-summary" data-testid="leg-summary">
        <dl class="leg-summary__counts">
          <div v-for="group in groups" :key="group.state" class="leg-summary__count" :class="`is-${group.state}`">
            <dt><span class="leg-dot" aria-hidden="true"></span>{{ legStateLabel(group.state) }}</dt>
            <dd :data-testid="`leg-count-${group.state}`">{{ group.entries.length }}</dd>
          </div>
        </dl>
        <div class="leg-summary__bar" aria-hidden="true">
          <template v-for="group in groups" :key="group.state">
            <span v-if="group.entries.length" :class="`is-${group.state}`" :style="{ flexGrow: group.entries.length }"></span>
          </template>
        </div>
      </div>

      <div class="leg-layout">
        <section ref="listPanel" class="leg-surface leg-list" :aria-labelledby="`${panelId}-list`">
          <h3 :id="`${panelId}-list`" class="leg-surface__heading">子系统清单</h3>
          <template v-for="group in groups" :key="group.state">
            <div v-if="group.entries.length" class="leg-group" :class="`is-${group.state}`">
              <button
                v-if="group.state !== 'degraded'"
                type="button"
                class="leg-group__toggle"
                :data-testid="`leg-group-${group.state}`"
                :aria-expanded="group.open"
                :aria-controls="`${panelId}-${group.state}`"
                @click="expanded[group.state] = !group.open"
              >
                <component :is="group.open ? DownOutlined : RightOutlined" aria-hidden="true" />
                {{ legStateLabel(group.state) }} · {{ group.entries.length }} 项
              </button>
              <ul v-show="group.open" :id="`${panelId}-${group.state}`" class="leg-group__rows" :aria-label="legStateLabel(group.state)">
                <li v-for="{ row, state } in group.entries" :key="row.name">
                  <button
                    type="button"
                    class="leg-row"
                    :class="{ 'is-selected': selected?.row.name === row.name }"
                    :data-testid="`leg-${row.name}`"
                    :aria-pressed="selected?.row.name === row.name"
                    :aria-controls="`${panelId}-detail`"
                    @click="selectRow(row.name)"
                  >
                    <span class="leg-dot" aria-hidden="true"></span>
                    <span class="leg-name">{{ legLabel(row.name) }}</span>
                    <a-tag :color="tagColors[state]" :data-testid="`state-${row.name}`">{{ legStateLabel(state) }}</a-tag>
                  </button>
                </li>
              </ul>
            </div>
          </template>
        </section>

        <section
          v-if="selected && details"
          :id="`${panelId}-detail`"
          ref="detailPanel"
          class="leg-surface leg-inspector"
          :aria-labelledby="`${panelId}-selected`"
          data-testid="leg-inspector"
          tabindex="-1"
        >
          <header class="leg-inspector__head">
            <h3 :id="`${panelId}-selected`" class="leg-surface__heading">{{ legLabel(selected.row.name) }}</h3>
            <a-tag :color="tagColors[selected.state]">{{ legStateLabel(selected.state) }}</a-tag>
          </header>

          <dl v-if="details.reason || details.fallback !== null" class="leg-callouts">
            <div v-if="details.reason" class="leg-callout is-degraded" data-testid="leg-reason">
              <dt><span class="leg-dot" aria-hidden="true"></span>降级原因</dt>
              <dd>{{ details.reason }}</dd>
            </div>
            <div v-if="details.fallback !== null" class="leg-callout is-enabled" data-testid="leg-fallback">
              <dt><span class="leg-dot" aria-hidden="true"></span>{{ selected.state === 'degraded' ? '当前兜底' : '备用预案' }}</dt>
              <dd>内置预案 · {{ details.fallback }} 条</dd>
            </div>
          </dl>

          <dl class="leg-facts">
            <div>
              <dt>驱动</dt>
              <dd data-testid="leg-driver">{{ selected.row.driver || '—' }}</dd>
            </div>
            <div v-if="details.recallBudget !== null">
              <dt>召回预算</dt>
              <dd>{{ details.recallBudget }} ms</dd>
            </div>
          </dl>

          <details :key="selected.row.name" class="leg-raw" data-testid="leg-raw">
            <summary>
              查看原始参数 <DownOutlined aria-hidden="true" />
            </summary>
            <ul class="leg-raw__values">
              <li>driver={{ selected.row.driver || '—' }}</li>
              <li
                v-for="entry in details.raw"
                :key="entry.key"
                :title="entry.hint || undefined"
                :data-testid="`detail-${selected.row.name}-${entry.key}`"
              >{{ entry.key }}={{ entry.hint || entry.value }}</li>
            </ul>
          </details>
        </section>
      </div>
    </template>

    <p v-else-if="!loading" class="leg-empty" data-testid="legs-empty">
      {{ error ? '装配事实不可读' : '后端未上报任何可选子系统' }}
    </p>
  </a-card>
</template>

<style scoped>
.leg-panel {
  container-type: inline-size;
  --leg-face: var(--aeg-color-overview-tile);
  --leg-border: var(--aeg-color-border);
  --leg-degraded: var(--aeg-tag-orange-text);
  --leg-enabled: var(--aeg-color-status-success);
  --leg-disabled: var(--aeg-color-text-muted);
}
html[data-theme='dark'] .leg-panel {
  --leg-face: var(--dash-detail-face, var(--aeg-color-command-panel));
  --leg-border: var(--aeg-color-command-line);
}
.leg-heading,
.leg-surface__heading {
  margin: 0;
  font-size: var(--aeg-font-lg);
  font-weight: var(--aeg-weight-bold);
  line-height: var(--aeg-lh-snug);
}
.leg-heading {
  display: flex;
  align-items: center;
  gap: var(--aeg-space-s2);
  font-family: var(--aeg-font-display);
  font-size: var(--aeg-font-2xl);
}
.leg-heading::before {
  content: '';
  flex: 0 0 4px;
  height: 0.86em;
  border-radius: var(--aeg-radius-pill);
  background: var(--aeg-color-overview-accent);
}
.leg-error {
  margin-bottom: var(--aeg-space-s3);
}
.is-degraded { --leg-state: var(--leg-degraded); }
.is-enabled { --leg-state: var(--leg-enabled); }
.is-disabled { --leg-state: var(--leg-disabled); }
.leg-dot {
  display: inline-block;
  flex: none;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--leg-state);
}
.leg-summary,
.leg-surface,
.leg-callout,
.leg-facts,
.leg-raw {
  border: 1px solid var(--leg-border);
  border-radius: var(--aeg-radius-md);
  background: var(--leg-face);
}
.leg-summary,
.leg-surface {
  padding: var(--aeg-space-s4);
}
.leg-summary__counts {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: var(--aeg-space-s2);
  margin: 0 0 var(--aeg-space-s2);
}
.leg-summary__count {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: center;
  gap: var(--aeg-space-s2);
  padding: var(--aeg-space-s1);
  color: var(--aeg-color-text);
  font-size: var(--aeg-font-md);
  font-weight: var(--aeg-weight-semibold);
}
.leg-summary__count + .leg-summary__count {
  border-inline-start: 1px solid var(--leg-border);
}
.leg-summary__count dt {
  display: flex;
  align-items: center;
  gap: var(--aeg-space-s1);
}
.leg-summary__count dd {
  margin: 0;
  color: var(--aeg-color-text);
  font-size: var(--aeg-font-xl);
  font-variant-numeric: tabular-nums;
}
.leg-summary__bar {
  display: flex;
  overflow: hidden;
  height: 12px;
  border-radius: var(--aeg-radius-pill);
}
.leg-summary__bar > span {
  flex-basis: 0;
  background: var(--leg-state);
}
.leg-layout {
  position: relative;
  display: grid;
  grid-template-columns: minmax(0, 1.05fr) minmax(0, 1fr);
  gap: var(--aeg-space-s3);
  margin-top: var(--aeg-space-s3);
}
.leg-surface {
  min-width: 0;
}
html[data-theme='dark'] .leg-surface {
  background: radial-gradient(ellipse 75% 70px at left top, color-mix(in srgb, var(--aeg-color-command-glow) 62%, transparent), transparent),
    var(--leg-face);
  box-shadow: inset 0 1px 0 color-mix(in srgb, var(--aeg-color-overview-accent) 22%, transparent);
}
.leg-list,
.leg-inspector {
  display: grid;
  align-content: start;
  gap: var(--aeg-space-s3);
}
.leg-group {
  display: grid;
  gap: var(--aeg-space-s1);
}
.leg-group__toggle,
.leg-row,
.leg-raw summary {
  display: flex;
  align-items: center;
  gap: var(--aeg-space-s2);
  width: 100%;
  min-height: 39px;
  padding: var(--aeg-space-s1) var(--aeg-space-s2);
  border: 1px solid transparent;
  border-radius: var(--aeg-radius-sm);
  background: transparent;
  color: var(--aeg-color-text);
  font: inherit;
  text-align: start;
  cursor: pointer;
}
.leg-group__toggle {
  border-color: var(--leg-border);
  font-weight: var(--aeg-weight-semibold);
}
.leg-group__toggle > :first-child {
  color: var(--leg-state);
  font-size: var(--aeg-font-xs);
}
.leg-group__rows {
  display: grid;
  gap: var(--aeg-space-s1);
  max-height: 340px;
  overflow-y: auto;
  margin: 0;
  padding: 0;
  list-style: none;
}
.leg-row {
  border-color: var(--leg-border);
  background: color-mix(in srgb, var(--aeg-color-command-panel) 60%, transparent);
}
.leg-row.is-selected {
  border-color: var(--leg-state);
  background: color-mix(in srgb, var(--leg-state) 16%, var(--aeg-color-command-panel));
}
.leg-group__toggle:hover,
.leg-row:not(.is-selected):hover,
.leg-raw summary:hover {
  background: var(--aeg-color-brand-soft);
}
.leg-row:focus-visible,
.leg-group__toggle:focus-visible,
.leg-raw summary:focus-visible,
.leg-inspector:focus-visible {
  outline: 2px solid var(--aeg-color-overview-accent);
  outline-offset: 2px;
}
.leg-row:focus-visible {
  outline-offset: -2px;
}
.leg-name {
  flex: 1;
  min-width: 0;
  overflow-wrap: anywhere;
  font-weight: var(--aeg-weight-semibold);
}
.leg-row :deep(.ant-tag),
.leg-inspector__head :deep(.ant-tag) {
  flex: none;
  margin-inline-end: 0;
}
.leg-inspector__head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--aeg-space-s2);
}
.leg-callouts {
  display: grid;
  gap: var(--aeg-space-s2);
  margin: 0;
}
.leg-callout,
.leg-facts {
  padding: var(--aeg-space-s3) var(--aeg-space-s4);
}
.leg-callout {
  background: color-mix(in srgb, var(--leg-state) 7%, var(--aeg-color-command-panel));
}
.leg-callout dt {
  display: flex;
  align-items: center;
  gap: var(--aeg-space-s2);
}
.leg-callout dt,
.leg-facts dt {
  color: var(--aeg-color-text-secondary);
}
.leg-callout dd,
.leg-facts dd {
  margin: var(--aeg-space-s1) 0 0;
  color: var(--aeg-color-text);
  font-size: var(--aeg-font-md);
  font-weight: var(--aeg-weight-semibold);
  overflow-wrap: anywhere;
}
.leg-callout dd {
  font-size: var(--aeg-font-lg);
}
.leg-facts {
  display: grid;
  grid-auto-columns: minmax(0, 1fr);
  grid-auto-flow: column;
  gap: var(--aeg-space-s2);
  margin: 0;
}
.leg-facts > div + div {
  padding-inline-start: var(--aeg-space-s3);
  border-inline-start: 1px solid var(--leg-border);
}
.leg-raw summary {
  justify-content: space-between;
  padding-inline: var(--aeg-space-s3);
  list-style: none;
}
.leg-raw summary::-webkit-details-marker {
  display: none;
}
.leg-raw[open] summary > :last-child {
  transform: rotate(180deg);
}
.leg-raw__values {
  display: grid;
  gap: var(--aeg-space-s1);
  margin: 0;
  max-height: 240px;
  overflow-y: auto;
  padding: var(--aeg-space-s2) var(--aeg-space-s3);
  border-top: 1px solid var(--leg-border);
  color: var(--aeg-color-text-secondary);
  font: var(--aeg-font-sm) / var(--aeg-lh-normal) var(--aeg-font-mono);
  overflow-wrap: anywhere;
  list-style: none;
}
.leg-empty {
  margin: 0;
  padding: var(--aeg-space-s2) 0;
  color: var(--aeg-color-text-secondary);
}
@container (max-width: 650px) {
  .leg-layout { grid-template-columns: minmax(0, 1fr); }
}
@container (max-width: 380px) {
  .leg-heading { font-size: var(--aeg-font-lg); }
  .leg-summary, .leg-surface { padding: var(--aeg-space-s2); }
  .leg-callout dd { font-size: var(--aeg-font-md); }
  .leg-summary__counts { gap: var(--aeg-space-s1); }
  .leg-summary__count { column-gap: var(--aeg-space-s1); font-size: var(--aeg-font-sm); }
  .leg-summary__count dd { flex-basis: 100%; text-align: center; }
  .leg-panel :deep(.ant-card-head-wrapper) { flex-wrap: wrap; gap: var(--aeg-space-s1); }
}
</style>
