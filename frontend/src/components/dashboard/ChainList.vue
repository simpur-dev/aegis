<script setup lang="ts">
/**
 * 链路执行记录（spike：dashboard 标杆页）。
 *
 * 与旧 a-table 的差别：五段不再是五个会换行的标签（真机里每行被撑成三行高，
 * 一屏只装得下四条链），而是**一行一段的分段条**——每段一颗色块，
 * 颜色说"这段是谁做的"（智能体/平台兜底/混合/跳过），红说"这段砸了"。
 * 扫一列色块就能看出"哪条链是兜底跑完的"，这正是值班员要的那一眼。
 *
 * 继承的诚实性规矩：最新在上（调用方排好序）、窗口口径写在卡标题、
 * 空表给中文说明与出口，不落 antd 的 "No data"。
 */
import type { ChainSummary } from '@/api/types'

const STAGE_LABELS: Record<string, string> = {
  perceive: '感知',
  assess: '研判',
  plan: '决策',
  execute: '执行',
  feedback: '反馈',
}

defineProps<{ chains: ChainSummary[] }>()

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
    <div class="chain-list__legend" aria-hidden="true">
      <span class="seg seg--swatch is-agent"></span>智能体
      <span class="seg seg--swatch is-local"></span>平台兜底
      <span class="seg seg--swatch is-hybrid"></span>混合
      <span class="seg seg--swatch is-skipped"></span>跳过
      <span class="seg seg--swatch is-fail"></span>失败
    </div>
    <!-- 图例钉住、行区自己滚：窗口是 20 条，全部铺开会把这张卡撑到一屏多，
         把下面的智能体板与事件流顶出首屏——那是"这一页讲什么"的次序被行数劫持。 -->
    <div class="chain-list__rows">
      <div
        v-for="chain in chains"
        :key="chain.trace_id"
        class="chain"
        data-testid="chain-row"
        :data-trace="chain.trace_id"
      >
        <span class="chain__result" :class="resultOf(chain).cls">
          <span class="chain__dot" aria-hidden="true"></span>{{ resultOf(chain).label }}
        </span>
        <span class="chain__trace" :title="chain.trace_id">{{ chain.trace_id }}</span>
        <span class="chain__stages">
          <span v-for="stage in chain.stages" :key="stage.name" class="seg" :class="stageClass(stage)" :title="stageTitle(stage)">
            {{ stageLabel(stage) }}
          </span>
        </span>
        <span class="chain__tasks" :title="`拆解出的任务单元数`">{{ chain.task_units.length }} 个</span>
        <span v-if="!chain.acted" class="chain__idle pill-dashed">未触发</span>
        <span v-if="chain.degradations.length" class="chain__degraded" :title="chain.degradations.join('；')">降级</span>
      </div>
    </div>
    <div v-if="!chains.length" class="empty-hero empty-hero--compact" data-testid="chains-empty">
      <span class="empty-hero__icon" aria-hidden="true">◇</span>
      <div>
        <div class="empty-hero__title">还没有链路执行记录</div>
        <div class="empty-hero__desc">在监测页发起一次演练（激增/背景任一），五段链路就会跑起来并出现在这里。</div>
        <router-link class="empty-hero__action empty-hero__cta" to="/monitor">去监测页发起演练 →</router-link>
      </div>
    </div>
  </div>
</template>

<style scoped>
.chain-list {
  display: flex;
  flex-direction: column;
}
.chain-list__legend {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--aeg-space-s1) var(--aeg-space-s2);
  margin-bottom: var(--aeg-space-s2);
  font-size: var(--aeg-font-xs);
  color: var(--aeg-color-text-muted);
}
.chain-list__rows {
  /* 20 条窗口 ≈ 820px 高；给到 380px 的可视区（约 9 条），其余在区内滚 */
  max-height: 380px;
  overflow-y: auto;
}
.chain {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--aeg-space-s2) var(--aeg-space-s3);
  padding: var(--aeg-space-s2) var(--aeg-space-s1);
  border-bottom: 1px solid var(--aeg-color-border-subtle);
}
.chain:last-child {
  border-bottom: none;
}
.chain__result {
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 6px;
  min-width: 52px;
  font-size: var(--aeg-font-sm);
  font-weight: var(--aeg-weight-semibold);
}
.chain__dot {
  width: 8px;
  height: 8px;
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
.chain__trace {
  flex: 0 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--aeg-font-mono);
  font-size: var(--aeg-font-xs);
  color: var(--aeg-color-text-secondary);
}
.chain__stages {
  display: flex;
  flex: 0 0 auto;
  gap: 4px;
}
.seg {
  padding: 1px 6px;
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-sm);
  font-size: var(--aeg-font-xs);
  font-weight: var(--aeg-weight-medium);
  line-height: var(--aeg-lh-snug);
  white-space: nowrap;
}
.seg--swatch {
  display: inline-block;
  width: 14px;
  height: 10px;
  padding: 0;
}
.seg.is-agent {
  border-color: var(--aeg-color-brand);
  background: var(--aeg-color-brand);
  color: var(--aeg-color-text-inverse);
}
.seg.is-local {
  background: var(--aeg-color-sunken);
  color: var(--aeg-color-text-secondary);
}
.seg.is-hybrid {
  border-color: var(--aeg-tag-orange-line);
  background: var(--aeg-tag-orange-bg);
  color: var(--aeg-tag-orange-text);
}
.seg.is-skipped {
  border-style: dashed;
  background: transparent;
  color: var(--aeg-color-text-muted);
}
.seg.is-fail {
  border-color: var(--aeg-color-status-danger);
  background: var(--aeg-color-status-danger);
  color: var(--aeg-color-text-inverse);
}
.chain__tasks {
  margin-left: auto;
  font-size: var(--aeg-font-sm);
  font-variant-numeric: tabular-nums;
  color: var(--aeg-color-text-secondary);
}
.chain__degraded {
  font-size: var(--aeg-font-xs);
  color: var(--aeg-color-status-warning);
}
</style>
