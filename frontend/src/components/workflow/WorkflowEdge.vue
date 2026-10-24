<script setup lang="ts">
/**
 * 画布连线（移植 Dify `custom-edge` 的 2px 直线 + 中点运算符，适配 AEGIS 的运行态）。
 *
 * 条件标签常驻，中点 “+” 打开块选择器并把新节点插进这条边；
 * 线色按两端节点的运行态取：运行中 / 待人工 / 失败 / 已完成，其余走默认边色。
 */
import { BaseEdge, EdgeLabelRenderer, getBezierPath, type EdgeProps } from '@vue-flow/core'
import { computed } from 'vue'

import type { EdgeDef, NodeState } from '@/utils/graph'

import { useEditorContext } from './editor/context'
import { useBlockInsertion } from './editor/insertion'

const props = defineProps<EdgeProps<EdgeDef>>()

const { run } = useEditorContext()
const insertion = useBlockInsertion()

const condition = computed(() => (typeof props.label === 'string' ? props.label.trim() : ''))

function stateOf(nodeId: string): NodeState | null {
  return run.instance?.nodes.find((node) => node.node_id === nodeId)?.state ?? null
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

/** EdgeProps 不带算好的 path/label 坐标，几何要按两端锚点自己求。 */
const geometry = computed(() =>
  getBezierPath({
    sourceX: props.sourceX,
    sourceY: props.sourceY,
    sourcePosition: props.sourcePosition,
    targetX: props.targetX,
    targetY: props.targetY,
    targetPosition: props.targetPosition,
    curvature: props.curvature,
  }),
)

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
  <BaseEdge
    :path="geometry[0]"
    :marker-end="props.markerEnd"
    :marker-start="props.markerStart"
    :style="props.style"
    class="wf-edge__path"
    :class="`is-${tone}`"
  />
  <EdgeLabelRenderer>
    <div class="wf-edge__label" :class="`is-${tone}`" :style="labelStyle">
      <span v-if="condition !== ''" class="wf-edge__condition" :title="condition">{{ condition }}</span>
      <button
        v-if="insertion !== null"
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
/* BaseEdge 渲染的 <path> 不带本组件的 scope 属性，scoped 选择器命不中它——这几条必须全局。
   这里只管**颜色**：粗细由 `theme.css` 的 `.ed-canvas .vue-flow__edge-path` 按 `--canvas-stroke-k`
   （1/zoom，封顶 3）补偿——zoom 作用在整个变换层上，写死 2px 再叠 `vector-effect` 会让
   "看全局"那一档的线与节点描边各按各的比例缩，门禁量的设备像素与代码里的写法就对不上了。 */
:global(.wf-edge__path) {
  stroke: var(--aeg-color-editor-edge);
}
:global(.wf-edge__path.is-running) {
  stroke: var(--aeg-color-editor-state-running);
}
:global(.wf-edge__path.is-awaiting) {
  stroke: var(--aeg-color-editor-state-awaiting);
}
:global(.wf-edge__path.is-failed) {
  stroke: var(--aeg-color-status-danger);
}
:global(.wf-edge__path.is-done) {
  stroke: var(--aeg-color-status-success);
}
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
