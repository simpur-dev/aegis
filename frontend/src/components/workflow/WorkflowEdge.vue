<script setup lang="ts">
/**
 * 空闲连线沿节点类型渐变，执行时沿用运行态实色。条件标签和中点仍用于编辑。
 */
import { BaseEdge, EdgeLabelRenderer, getBezierPath, getSmoothStepPath, type EdgeProps } from '@vue-flow/core'
import { computed, useId } from 'vue'

import type { EdgeDef, NodeState } from '@/utils/graph'

import { useEditorContext } from './editor/context'
import { useBlockInsertion } from './editor/insertion'
import { resolveNodePresentation, type NodeAccent } from './presentation'

const props = defineProps<EdgeProps<EdgeDef>>()

const { document: documentStore, run } = useEditorContext()
const insertion = useBlockInsertion()
const edgePaintId = useId()
const gradientId = `wf-gradient-${edgePaintId}`
const glowGradientId = `wf-glow-${edgePaintId}`
const arrowId = `wf-arrow-${edgePaintId}`
const gradientLayers = [{ id: gradientId, glow: false }, { id: glowGradientId, glow: true }]

const condition = computed(() => (typeof props.label === 'string' ? props.label.trim() : ''))

function stateOf(nodeId: string): NodeState | null {
  return run.canvasRuns.find((node) => node.node_id === nodeId)?.state ?? null
}

type EdgeTone = 'default' | 'running' | 'awaiting' | 'failed' | 'done'

const tone = computed<EdgeTone>(() => {
  const ends = [stateOf(props.source), stateOf(props.target)]
  if (ends.includes('running')) return 'running'
  if (ends.includes('failed') || ends.includes('timeout')) return 'failed'
  if (ends.includes('awaiting_human')) return 'awaiting'
  if (ends.every((state) => state === 'succeeded' || state === 'degraded' || state === 'bypassed')) return 'done'
  return 'default'
})
const executionColors = {
  running: 'var(--aeg-color-editor-state-running)',
  awaiting: 'var(--aeg-color-editor-state-awaiting)',
  failed: 'var(--aeg-color-status-danger)',
  done: 'var(--aeg-color-status-success)',
} as const
const sourceAccent = computed(() => resolveNodePresentation(props.sourceNode.data?.def?.type ?? '').accent)
const targetAccent = computed(() => resolveNodePresentation(props.targetNode.data?.def?.type ?? '').accent)
const lavenderTransition = computed(() =>
  (sourceAccent.value === 'blue' && targetAccent.value === 'amber') ||
  (sourceAccent.value === 'amber' && targetAccent.value === 'blue'),
)
const edgeStroke = computed(() => tone.value === 'default' ? `url(#${gradientId})` : executionColors[tone.value])
const haloStroke = computed(() => tone.value === 'default' ? `url(#${glowGradientId})` : edgeStroke.value)
const arrowFill = computed(() => tone.value === 'default'
  ? accentPaint(targetAccent.value, false)
  : executionColors[tone.value])

function accentPaint(accent: NodeAccent, glow: boolean): string {
  const tone = `var(--aeg-color-editor-accent-${accent})`
  return glow ? `var(--aeg-color-editor-bloom-${accent}, ${tone})` : tone
}

/** EdgeProps 不带算好的 path/label 坐标，几何要按两端锚点自己求。 */
const geometry = computed(() => {
  const anchors = {
    sourceX: props.sourceX,
    sourceY: props.sourceY,
    sourcePosition: props.sourcePosition,
    targetX: props.targetX,
    targetY: props.targetY,
    targetPosition: props.targetPosition,
  }
  const gap = props.targetX - props.sourceX
  const rise = Math.abs(props.targetY - props.sourceY)
  if (gap >= 32 && rise >= Math.max(80, gap * 1.5)) {
    return getSmoothStepPath({ ...anchors, borderRadius: 18, offset: 10 })
  }
  return getBezierPath({ ...anchors, curvature: props.curvature })
})

const labelStyle = computed(() => {
  const [, labelX, labelY] = geometry.value
  return { transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)` }
})

function openSelector(event: MouseEvent): void {
  event.stopPropagation()
  const rect = (event.currentTarget as HTMLElement).getBoundingClientRect()
  insertion?.open({
    x: rect.right,
    y: rect.top,
    edge: { id: props.id, source: props.source, target: props.target },
  })
}
</script>

<template>
  <defs>
    <template v-if="tone === 'default'">
      <linearGradient v-for="layer in gradientLayers" :id="layer.id" :key="layer.id" gradientUnits="userSpaceOnUse"
        :x1="props.sourceX" :y1="props.sourceY" :x2="props.targetX" :y2="props.targetY">
        <stop offset="0%" :stop-color="accentPaint(sourceAccent, layer.glow)" />
        <stop v-if="lavenderTransition" offset="62%" :stop-color="accentPaint('violet', layer.glow)" />
        <stop offset="100%" :stop-color="accentPaint(targetAccent, layer.glow)" />
      </linearGradient>
    </template>
    <marker v-if="props.markerEnd" :id="arrowId" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="5.6" markerHeight="5.6" orient="auto"
      :class="{ 'wf-edge__lit-arrow': tone === 'default' }" :style="{ '--wf-arrow-fill': arrowFill, '--wf-arrow-glow': accentPaint(targetAccent, true) }">
      <path d="M0 0 10 5 0 10 2.6 5Z" :fill="arrowFill" />
    </marker>
  </defs>
  <path v-if="tone === 'default'" class="wf-edge__bloom" :d="geometry[0]" :stroke="haloStroke" aria-hidden="true" />
  <path class="wf-edge__halo" :class="{ 'is-idle': tone === 'default' }" :d="geometry[0]" :stroke="haloStroke" aria-hidden="true" />
  <BaseEdge
    :path="geometry[0]"
    :marker-end="props.markerEnd ? `url(#${arrowId})` : undefined"
    :marker-start="props.markerStart"
    :style="{ stroke: edgeStroke, ...props.style }"
    class="wf-edge__path"
    :class="{ 'is-selected': props.selected }"
  />
  <path v-if="tone === 'default'" class="wf-edge__shine" :d="geometry[0]" aria-hidden="true" />
  <EdgeLabelRenderer>
    <div class="wf-edge__label" :class="`is-${tone}`" :style="labelStyle">
      <span v-if="condition !== ''" class="wf-edge__condition" :title="condition">{{ condition }}</span>
      <button
        v-if="insertion !== null && documentStore.canEdit"
        type="button"
        class="wf-edge__insert nodrag nopan"
        :aria-label="`在 ${props.source} 与 ${props.target} 之间插入节点`"
        :data-testid="`edge-insert-${props.id}`"
        @click="openSelector"
      >
        +
      </button>
    </div>
  </EdgeLabelRenderer>
</template>

<style scoped>
.wf-edge__halo,
.wf-edge__bloom,
.wf-edge__shine {
  display: none;
  fill: none;
  stroke-linecap: round;
  pointer-events: none;
}
.wf-edge__halo {
  stroke-width: calc(4.5px * var(--canvas-stroke-k, 1));
  filter: blur(2.8px);
  opacity: .73;
}
.wf-edge__halo.is-idle {
  stroke-width: 12px;
  filter: blur(3.5px);
  opacity: .94;
}
.wf-edge__bloom {
  stroke-width: 24px;
  filter: blur(9px);
  opacity: .72;
}
.wf-edge__shine {
  stroke: var(--aeg-color-text);
  stroke-width: calc(1.25px * var(--canvas-stroke-k, 1));
  opacity: .95;
}
html[data-theme='dark'] .wf-edge__halo,
html[data-theme='dark'] .wf-edge__bloom,
html[data-theme='dark'] .wf-edge__shine { display: inline; }
html[data-theme='dark'] .wf-edge__lit-arrow { overflow: visible; }
html[data-theme='dark'] .wf-edge__lit-arrow path {
  stroke: color-mix(in srgb, var(--wf-arrow-fill) 25%, var(--aeg-color-text));
  stroke-width: .8;
  filter: drop-shadow(0 0 2px var(--wf-arrow-glow));
}
:global(.ed-canvas .wf-edge__path) {
  stroke-width: calc(var(--wf-edge-selected-width, var(--wf-edge-default-width, 1.5px)) * var(--canvas-stroke-k, 1));
}
:global(html[data-theme='dark'] .ed-canvas .wf-edge__path) { --wf-edge-default-width: 2.45px; }
:global(.ed-canvas .wf-edge__path.is-selected) { --wf-edge-selected-width: 2.85px; }
.wf-edge__label {
  position: absolute;
  display: flex;
  align-items: center;
  gap: 6px;
  pointer-events: none;
}
.wf-edge__condition {
  max-width: 160px;
  overflow: hidden;
  padding: 1px 8px;
  border: 1px solid var(--aeg-color-border);
  border-radius: 999px;
  background: var(--aeg-color-editor-panel);
  color: var(--aeg-color-text-secondary);
  font-size: 11px;
  line-height: 18px;
  text-overflow: ellipsis;
  white-space: nowrap;
  pointer-events: all;
}
.wf-edge__insert {
  display: grid;
  place-items: center;
  width: 20px;
  height: 20px;
  padding: 0;
  border: 1px solid var(--aeg-color-brand-line);
  border-radius: 999px;
  background: var(--aeg-color-editor-panel);
  color: var(--aeg-color-brand);
  font: inherit;
  font-size: 14px;
  line-height: 1;
  box-shadow: var(--aeg-shadow-sm);
  cursor: pointer;
  pointer-events: all;
  transition:
    transform var(--aeg-motion-base) ease,
    background var(--aeg-motion-base) ease,
    color var(--aeg-motion-base) ease;
}
.wf-edge__insert:hover,
.wf-edge__insert:focus-visible {
  background: var(--aeg-color-brand);
  color: var(--aeg-color-text-inverse);
  transform: scale(1.25);
}
</style>
