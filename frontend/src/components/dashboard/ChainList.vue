<script setup lang="ts">
/**
 * 链路执行记录：一行一条链，先给结论（成功/失败 + 走到哪一段），再给细节（trace、任务数、降级）。
 *
 * 密度取舍：底部三卡等高，这张表默认只露 3 条（卡内滚动 + 底部渐隐），其余等"查看全部"。
 * 段位不再用五个实心胶囊：改成"圆点 + 段名 + 连接线"的轻重轨，颜色只说这段是谁做的/有没有砸。
 * 顶部图例改成可点的筛选页签：值班员常问"这轮是智能体接的还是平台兜底的"，
 * 图例只能说"颜色是什么"，筛选能直接把不是它的排掉。
 */
import { computed, ref } from 'vue'

import type { ChainSummary } from '@/api/types'

const STAGE_LABELS: Record<string, string> = {
  perceive: '感知',
  assess: '研判',
  plan: '决策',
  execute: '执行',
  feedback: '反馈',
}

const props = defineProps<{ chains: ChainSummary[] }>()

/** 底部三卡等高：默认露出 3 张卡，点"查看全部"后放开到卡内滚动 */
const VISIBLE_ROWS = 3
const expanded = ref(false)

type FilterKey = 'all' | 'agent' | 'local' | 'hybrid' | 'skipped' | 'failed'
const FILTERS: { key: FilterKey; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'agent', label: '智能体' },
  { key: 'local', label: '平台兜底' },
  { key: 'hybrid', label: '混合' },
  { key: 'skipped', label: '跳过' },
  { key: 'failed', label: '失败' },
]
const filter = ref<FilterKey>('all')

function isFailed(chain: ChainSummary): boolean {
  return chain.errors.length > 0 || chain.stages.some((stage) => !stage.ok)
}

function matches(chain: ChainSummary, key: FilterKey): boolean {
  if (key === 'all') return true
  if (key === 'failed') return isFailed(chain)
  return chain.stages.some((stage) => stage.mode === key)
}

const filtered = computed<ChainSummary[]>(() => props.chains.filter((chain) => matches(chain, filter.value)))
const canExpand = computed(() => filtered.value.length > VISIBLE_ROWS)

function countOf(key: FilterKey): number {
  return key === 'all' ? props.chains.length : props.chains.filter((chain) => matches(chain, key)).length
}

function stageClass(stage: ChainSummary['stages'][number]): string {
  if (!stage.ok) return 'is-fail'
  return `is-${stage.mode}`
}

function stageLabel(stage: ChainSummary['stages'][number]): string {
  return STAGE_LABELS[stage.name] ?? stage.name
}

function stageTitle(stage: ChainSummary['stages'][number]): string {
  const who = stage.mode === 'agent' ? '智能体' : stage.mode === 'local' ? '平台兜底' : stage.mode === 'hybrid' ? '混合' : '跳过'
  return `${stageLabel(stage)} · ${who} · ${stage.latency_ms}ms${stage.note ? ` · ${stage.note}` : ''}`
}

function resultOf(chain: ChainSummary): { label: string; cls: string } {
  if (chain.errors.length) return { label: '失败', cls: 'is-bad' }
  if (chain.ok) return { label: '成功', cls: 'is-ok' }
  return { label: '进行中', cls: 'is-warn' }
}
</script>

<template>
  <div class="chain-list">
    <div class="chain-list__filters" role="tablist" aria-label="按执行方式筛选链路">
      <button
        v-for="entry in FILTERS"
        :key="entry.key"
        type="button"
        class="chain-filter"
        :class="{ 'is-active': filter === entry.key }"
        role="tab"
        :aria-selected="filter === entry.key"
        :data-testid="`chain-filter-${entry.key}`"
        @click="filter = entry.key"
      >
        {{ entry.label }}<span class="chain-filter__count">{{ countOf(entry.key) }}</span>
      </button>
    </div>

    <div class="chain-list__rows" :class="{ 'is-expanded': expanded }">
      <div
        v-for="chain in filtered"
        :key="chain.trace_id"
        class="chain"
        :class="resultOf(chain).cls"
        data-testid="chain-row"
        :data-trace="chain.trace_id"
      >
        <div class="chain__top">
          <span class="chain__result" :class="resultOf(chain).cls">
            <span class="chain__dot" aria-hidden="true"></span>{{ resultOf(chain).label }}
          </span>
          <span class="chain__trace" :title="chain.trace_id">{{ chain.trace_id }}</span>
          <span class="chain__meta">
            <span v-if="!chain.acted" class="chain__idle">未触发</span>
            <span class="chain__tasks" :title="`拆解出的任务单元数`">{{ chain.task_units.length }} 个</span>
            <span v-if="chain.degradations.length" class="chain__degraded" :title="chain.degradations.join('；')">降级</span>
          </span>
        </div>

        <span class="chain__stages">
          <span
            v-for="stage in chain.stages"
            :key="stage.name"
            class="stage"
            :class="stageClass(stage)"
            :title="stageTitle(stage)"
          >
            <i class="stage__dot" aria-hidden="true"></i>
            <span class="stage__label">{{ stageLabel(stage) }}</span>
          </span>
        </span>
      </div>
      <div v-if="chains.length && !filtered.length" class="chain-list__nomatch" data-testid="chains-nomatch">
        没有符合「{{ FILTERS.find((entry) => entry.key === filter)?.label }}」的链路
      </div>
    </div>

    <button
      v-if="canExpand"
      type="button"
      class="chain-list__more"
      data-testid="chains-more"
      @click="expanded = !expanded"
    >
      {{ expanded ? '收起' : `查看全部 ${filtered.length} 条` }}<span aria-hidden="true"> →</span>
    </button>

    <div v-if="!chains.length" class="empty-hero empty-hero--compact" data-testid="chains-empty">
      <span class="empty-hero__icon" aria-hidden="true">◇</span>
      <div>
        <div class="empty-hero__title">还没有处置记录</div>
        <div class="empty-hero__desc">在监测页发起一次演练（激增/背景任一），整条处置链就会跑起来并出现在这里。</div>
        <router-link class="empty-hero__action empty-hero__cta" to="/monitor">去监测页发起演练 →</router-link>
      </div>
    </div>
  </div>
</template>

<style scoped>
.chain-list {
  display: flex;
  flex-direction: column;
  gap: var(--aeg-space-s2);
}

/* 筛选页签：一行小胶囊，选中的那枚品牌色描边；计数用等宽数字，切换时宽度不抖 */
.chain-list__filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--aeg-space-s1);
}
.chain-filter {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-pill);
  background: transparent;
  color: var(--aeg-color-text-muted);
  font-size: var(--aeg-font-xs);
  cursor: pointer;
  transition:
    border-color var(--aeg-motion-base) ease,
    color var(--aeg-motion-base) ease,
    background var(--aeg-motion-base) ease;
}
.chain-filter:hover {
  border-color: var(--aeg-color-brand);
  color: var(--aeg-color-text);
}
.chain-filter.is-active {
  border-color: var(--aeg-color-brand);
  background: var(--aeg-color-brand-soft);
  color: var(--aeg-color-brand-hover);
}
.chain-filter__count {
  font-variant-numeric: tabular-nums;
  opacity: 0.8;
}
.chain-list__nomatch {
  padding: var(--aeg-space-s3) 0;
  text-align: center;
  font-size: var(--aeg-font-sm);
  color: var(--aeg-color-text-muted);
}

/* 每张卡片 = 一条链；默认露 3 张（3×~62px + gap），底部渐隐提示还有更多 */
.chain-list__rows {
  display: flex;
  flex-direction: column;
  gap: var(--aeg-space-s2);
  max-height: 220px;
  overflow-y: auto;
  -webkit-mask-image: linear-gradient(to bottom, black calc(100% - 22px), transparent);
  mask-image: linear-gradient(to bottom, black calc(100% - 22px), transparent);
}
.chain-list__rows.is-expanded {
  max-height: 420px;
  -webkit-mask-image: none;
  mask-image: none;
}
/* 滚动条细一点，别在卡片里长出一条粗灰带 */
.chain-list__rows::-webkit-scrollbar {
  width: 6px;
}
.chain-list__rows::-webkit-scrollbar-thumb {
  border-radius: var(--aeg-radius-pill);
  background: var(--aeg-color-border);
}

/* 一条链一张卡：底色比外层玻璃再淡一档，只用描边 + 悬停描边，不叠第二层模糊 */
.chain {
  display: flex;
  flex-direction: column;
  gap: var(--aeg-space-s2);
  padding: 10px 12px;
  border: 1px solid var(--aeg-color-border);
  border-left: 3px solid transparent;
  border-radius: var(--aeg-radius-md);
  background: var(--aeg-color-sunken);
  transition:
    border-color var(--aeg-motion-base) ease,
    box-shadow var(--aeg-motion-base) ease;
}
.chain:hover {
  border-color: var(--aeg-color-brand);
  box-shadow: var(--aeg-shadow-sm);
}
/* 失败卡只多一条左侧红条，不再整卡染色 */
.chain.is-bad {
  border-left-color: var(--aeg-color-status-danger);
}

.chain__top {
  display: flex;
  align-items: center;
  gap: var(--aeg-space-s2);
  min-width: 0;
}
.chain__result {
  display: inline-flex;
  flex: 0 0 46px;
  align-items: center;
  gap: 5px;
  font-size: var(--aeg-font-xs);
  font-weight: var(--aeg-weight-semibold);
}
.chain__dot {
  width: 7px;
  height: 7px;
  border-radius: var(--aeg-radius-pill);
  background: var(--aeg-color-border-strong);
}
.chain__result.is-ok {
  color: var(--aeg-color-status-success);
}
.chain__result.is-ok .chain__dot {
  background: var(--aeg-color-status-success);
}
.chain__result.is-warn {
  color: var(--aeg-color-status-warning);
}
.chain__result.is-warn .chain__dot {
  background: var(--aeg-color-status-warning);
}
.chain__result.is-bad {
  color: var(--aeg-color-status-danger);
}
.chain__result.is-bad .chain__dot {
  background: var(--aeg-color-status-danger);
}

/* 五段轨：圆点说状态、段名说阶段、连接线把它们串起来 */
.chain__stages {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  min-width: 0;
}
.stage {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: var(--aeg-font-xs);
  color: var(--aeg-color-text-muted);
  white-space: nowrap;
}
.stage:not(:last-child)::after {
  content: '';
  width: 10px;
  height: 1px;
  margin: 0 4px;
  background: var(--aeg-color-border);
}
.stage__dot {
  width: 8px;
  height: 8px;
  border: 1.5px solid var(--aeg-color-border-strong);
  border-radius: var(--aeg-radius-pill);
  background: transparent;
}
.stage.is-agent .stage__dot {
  border-color: var(--aeg-color-brand);
  background: var(--aeg-color-brand);
}
.stage.is-local .stage__dot {
  border-color: var(--aeg-color-text-muted);
  background: var(--aeg-color-text-muted);
}
.stage.is-hybrid .stage__dot {
  border-color: var(--aeg-color-status-warning);
  background: var(--aeg-color-status-warning);
}
.stage.is-skipped .stage__dot {
  border-style: dashed;
}
.stage.is-fail {
  color: var(--aeg-color-status-danger);
}
.stage.is-fail .stage__dot {
  border-color: var(--aeg-color-status-danger);
  background: var(--aeg-color-status-danger);
}

.chain__meta {
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  gap: var(--aeg-space-s2);
  margin-left: auto;
  font-size: var(--aeg-font-xs);
  color: var(--aeg-color-text-muted);
}
.chain__tasks {
  font-variant-numeric: tabular-nums;
}
.chain__idle {
  color: var(--aeg-color-text-muted);
}
.chain__degraded {
  color: var(--aeg-color-status-warning);
}

/* trace id 退到第二行：等宽、可省略、悬停看全 */
.chain__trace {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--aeg-font-mono);
  font-size: 10px;
  opacity: 0.72;
  color: var(--aeg-color-text-muted);
}

.chain-list__more {
  align-self: flex-end;
  padding: 0;
  border: none;
  background: transparent;
  color: var(--aeg-color-brand);
  font-size: var(--aeg-font-xs);
  cursor: pointer;
}
.chain-list__more:hover {
  text-decoration: underline;
}
</style>
