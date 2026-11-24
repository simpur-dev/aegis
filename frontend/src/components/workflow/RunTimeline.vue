<script setup lang="ts">
import { computed } from 'vue'

import type { InstanceNodeRun } from '@/api/workflow'
import { stateStyle, type JsonValue } from '@/utils/graph'

const props = defineProps<{
  runs: InstanceNodeRun[]
  names?: Record<string, string>
  outputs?: Record<string, Record<string, JsonValue>> | null
  includeLiveOutput?: boolean
}>()

const emit = defineEmits<{ select: [nodeId: string] }>()

const MAX_OUTPUT_CHARS = 1_200
const STAGE_MODES = new Map([
  ['agent', '智能体执行'],
  ['local', '本地执行'],
  ['hybrid', '混合执行'],
  ['skipped', '跳过'],
])

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

function formatOutput(output: Record<string, JsonValue> | undefined): string {
  if (output === undefined || Object.keys(output).length === 0) return ''
  const text = JSON.stringify(output, null, 2)
  return text.length > MAX_OUTPUT_CHARS ? `${text.slice(0, MAX_OUTPUT_CHARS)}\n…（已截断）` : text
}

const rows = computed(() => props.runs.map((run, index) => {
  const output = props.outputs?.[run.node_id] ?? (props.includeLiveOutput ? run.output : undefined)
  const stageMode = run.type === 'chain_stage' && typeof output?.mode === 'string' ? STAGE_MODES.get(output.mode) : undefined
  const stageNote = run.type === 'chain_stage' && typeof output?.note === 'string' ? output.note : ''
  return { run, index, tone: toneOf(run.state), meta: metaOf(run), output: formatOutput(output), stageMode, stageNote }
}))
</script>

<template>
  <ol v-if="rows.length > 0" class="wf-tl" data-testid="run-timeline">
    <li v-for="row in rows" :key="row.run.node_id" class="wf-tl__item">
      <button
        type="button"
        class="wf-tl__step"
        :class="`is-${row.tone}`"
        :title="`在画布上定位 ${row.run.node_id}`"
        @click="emit('select', row.run.node_id)"
      >
        <span class="wf-tl__axis" aria-hidden="true">
          <span class="wf-tl__dot" :style="{ background: styleOf(row.run.state).color }"></span>
          <span v-if="row.index < rows.length - 1" class="wf-tl__line"></span>
        </span>
        <span class="wf-tl__body">
          <span class="wf-tl__head">
            <span class="wf-tl__index">{{ row.index + 1 }}</span>
            <span class="wf-tl__name" :title="row.run.node_id">{{ nameOf(row.run.node_id) }}</span>
            <span class="wf-tl__state">{{ styleOf(row.run.state).label }}</span>
          </span>
          <span v-if="row.meta !== ''" class="wf-tl__meta">{{ row.meta }}</span>
          <span v-if="row.stageMode || row.stageNote" class="wf-tl__evidence">
            <span v-if="row.stageMode" class="wf-tl__mode">{{ row.stageMode }}</span>
            <span v-if="row.stageNote" class="wf-tl__note" :title="row.stageNote">{{ row.stageNote }}</span>
          </span>
          <span v-if="row.run.error !== null" class="wf-tl__err" :title="row.run.error">{{ row.run.error }}</span>
        </span>
      </button>
      <details v-if="row.output !== ''" class="wf-tl__output">
        <summary class="wf-tl__output-label">节点产出</summary>
        <pre class="wf-tl__pre">{{ row.output }}</pre>
      </details>
    </li>
  </ol>
  <p v-else class="wf-tl__empty">暂无步骤。也可在实例记录中点一条实例查看。</p>
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
.wf-tl__item {
  margin: 0;
  padding: 0;
}
.wf-tl__step {
  display: grid;
  grid-template-columns: 24px minmax(0, 1fr);
  gap: 8px;
  width: 100%;
  padding: 10px 12px;
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-md);
  background: var(--aeg-color-overview-tile);
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}
.wf-tl__step:hover {
  border-color: var(--aeg-color-brand-line);
}
.wf-tl__step:focus-visible {
  outline: 2px solid var(--aeg-color-brand);
  outline-offset: 1px;
}
.wf-tl__step.is-active {
  border-color: var(--aeg-color-brand-line);
  background: var(--aeg-color-brand-soft);
}
.wf-tl__step.is-bad {
  border-color: color-mix(in srgb, var(--aeg-color-status-danger) 45%, transparent);
  background: color-mix(in srgb, var(--aeg-color-status-danger) 8%, transparent);
}
/* 没走到的步骤：透明底 + dashed 边框。实心色块在界面上意味着"这是测出来的状态" */
.wf-tl__step.is-todo {
  border-style: dashed;
  border-color: var(--aeg-color-border);
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
  border: 2px solid var(--aeg-color-card);
  border-radius: 50%;
  z-index: 1;
  flex: 0 0 auto;
}
.is-active .wf-tl__dot {
  box-shadow: 0 0 0 3px var(--aeg-color-brand-soft);
}
.wf-tl__line {
  width: 2px;
  flex: 1;
  margin-top: -2px;
  background: var(--aeg-color-border);
}
.wf-tl__body {
  display: block;
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
  color: var(--aeg-color-text-secondary);
}
.wf-tl__name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  font-weight: 600;
  color: var(--aeg-color-text);
}
.wf-tl__state {
  flex: 0 0 auto;
  margin-left: auto;
  font-size: 11px;
  font-weight: 600;
  color: var(--aeg-color-text-secondary);
}
.wf-tl__meta {
  display: block;
  margin: 4px 0 0;
  font-size: 11px;
  color: var(--aeg-color-text-secondary);
  font-variant-numeric: tabular-nums;
}
.wf-tl__evidence {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 6px;
  align-items: baseline;
  margin-top: 6px;
  font-size: 12px;
}
.wf-tl__mode {
  padding: 1px 6px;
  border-radius: var(--aeg-radius-sm);
  background: var(--aeg-color-brand-soft);
  color: var(--aeg-color-brand-hover);
}
.wf-tl__note {
  color: var(--aeg-color-text-secondary);
  overflow-wrap: anywhere;
}
.wf-tl__err {
  display: block;
  margin: 4px 0 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11px;
  color: var(--aeg-color-status-danger);
}
.wf-tl__output {
  padding: 0 8px 0 44px;
}
.wf-tl__output-label {
  min-height: 32px;
  padding-block: 7px;
  color: var(--aeg-color-text-secondary);
  font-size: 12px;
  cursor: pointer;
}
.wf-tl__pre {
  max-height: 220px;
  margin: 4px 0 0;
  padding: 6px 8px;
  overflow: auto;
  border-radius: 6px;
  background: var(--aeg-color-sunken);
  color: var(--aeg-color-text-secondary);
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}
.wf-tl__empty {
  margin: 0;
  font-size: 12px;
  color: var(--aeg-color-text-secondary);
}
</style>
