<script setup lang="ts">
/**
 * 竖向运行时间线：一条实例走到哪一步、卡在哪一步、每步花了多久。
 *
 * 形状移植自 NexusMind `Step4Report.vue:3627-3740`（`grid-template-columns:24px 1fr`、10px 圆点带
 * 2px 白 knockout 描边、2px 连接线、未开始的一档用 dashed 边框 + 透明底）。它的 token 属于
 * nm-teal 那一支（`--wf-active-dot: #006680`），我们只移植蓝系：底纹与描边一律走 #2563eb 系。
 *
 * 状态文案与色值**不另立表**——直接取 `utils/graph.ts` 的 `NODE_STATE_STYLES`（画布节点卡用的就是它），
 * 免得界面上一处说"待人工核签"另一处说"等待人工决策"。圆点色只作装饰：状态同时以文字写出，
 * 所以它不是唯一信息载体（WCAG 1.4.11 的例外），画布与这条也永远同色。
 */
import { computed } from 'vue'

import type { InstanceNodeRun } from '@/api/workflow'
import { stateStyle } from '@/utils/graph'

const props = defineProps<{ runs: InstanceNodeRun[]; names?: Record<string, string> }>()

type Tone = 'todo' | 'active' | 'done' | 'bad'

const TONES: Record<string, Tone> = {
  pending: 'todo',
  ready: 'todo',
  running: 'active',
  awaiting_human: 'active',
  succeeded: 'done',
  degraded: 'done',
  bypassed: 'done',
  skipped: 'done',
  cancelled: 'done',
  failed: 'bad',
  timeout: 'bad',
}

const toneOf = (state: string): Tone => TONES[state] ?? 'todo'
const styleOf = (state: string) => stateStyle(state as InstanceNodeRun['state'])
const nameOf = (nodeId: string): string => props.names?.[nodeId] ?? nodeId

function durationOf(ms: number): string {
  return ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(1)}s`
}

/** 一行小字：耗时 / 排队 / 重试。全都没有就返回空串，不占行。 */
function metaOf(run: InstanceNodeRun): string {
  const bits: string[] = []
  if (run.duration_ms !== null) bits.push(`耗时 ${durationOf(run.duration_ms)}`)
  if (run.schedule_latency_ms !== null) bits.push(`排队 ${durationOf(run.schedule_latency_ms)}`)
  if (run.attempts > 1) bits.push(`第 ${run.attempts} 次尝试`)
  return bits.join(' ｜ ')
}

const rows = computed(() => props.runs.map((run, index) => ({ run, index, tone: toneOf(run.state) })))
</script>

<template>
  <ol v-if="rows.length > 0" class="wf-tl" data-testid="run-timeline">
    <li v-for="row in rows" :key="row.run.node_id" class="wf-tl__step" :class="`is-${row.tone}`">
      <span class="wf-tl__axis" aria-hidden="true">
        <span class="wf-tl__dot" :style="{ background: styleOf(row.run.state).color }"></span>
        <span v-if="row.index < rows.length - 1" class="wf-tl__line"></span>
      </span>
      <div class="wf-tl__body">
        <div class="wf-tl__head">
          <span class="wf-tl__index">{{ row.index + 1 }}</span>
          <span class="wf-tl__name" :title="row.run.node_id">{{ nameOf(row.run.node_id) }}</span>
          <span class="wf-tl__state">{{ styleOf(row.run.state).label }}</span>
        </div>
        <p v-if="metaOf(row.run) !== ''" class="wf-tl__meta">{{ metaOf(row.run) }}</p>
        <p v-if="row.run.error !== null" class="wf-tl__err" :title="row.run.error">{{ row.run.error }}</p>
      </div>
    </li>
  </ol>
  <p v-else class="wf-tl__empty">还没选中实例——在上方点一条实例，这里就列出它每一步走到哪了。</p>
</template>

<style scoped>
.wf-tl {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.wf-tl__step {
  display: grid;
  grid-template-columns: 24px 1fr;
  gap: 12px;
  padding: 10px 12px;
  border: 1px solid #eef4f6;
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.72);
}
.wf-tl__step.is-active {
  border-color: rgba(37, 99, 235, 0.3);
  background: linear-gradient(135deg, rgba(239, 246, 255, 0.95) 0%, rgba(219, 234, 254, 0.8) 100%);
}
.wf-tl__step.is-bad {
  border-color: rgba(185, 28, 28, 0.28);
  background: rgba(239, 68, 68, 0.06);
}
/* 没走到的步骤：透明底 + dashed 边框。实心色块在界面上意味着"这是测出来的状态" */
.wf-tl__step.is-todo {
  border-style: dashed;
  border-color: rgba(37, 99, 235, 0.28);
  background: transparent;
}
.wf-tl__axis {
  display: flex;
  flex-direction: column;
  align-items: center;
  flex: 0 0 auto;
  width: 24px;
}
.wf-tl__dot {
  box-sizing: border-box;
  width: 10px;
  height: 10px;
  border: 2px solid #fff;
  border-radius: 50%;
  z-index: 1;
  flex: 0 0 auto;
}
.is-active .wf-tl__dot {
  box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.16);
}
.wf-tl__line {
  width: 2px;
  flex: 1;
  margin-top: -2px;
  background: #e6e8ef;
}
.wf-tl__body {
  min-width: 0;
}
.wf-tl__head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
}
.wf-tl__index {
  flex: 0 0 auto;
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  color: #5a6072;
}
.wf-tl__name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  font-weight: 600;
  color: #1d2129;
}
.wf-tl__state {
  flex: 0 0 auto;
  margin-left: auto;
  font-size: 11px;
  font-weight: 600;
  color: #334155;
}
.wf-tl__meta {
  margin: 4px 0 0;
  font-size: 11px;
  color: #5a6072;
  font-variant-numeric: tabular-nums;
}
.wf-tl__err {
  margin: 4px 0 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11px;
  color: #b91c1c;
}
.wf-tl__empty {
  margin: 0;
  font-size: 12px;
  color: #5a6072;
}
</style>
