<script setup lang="ts">
/**
 * 全部画布节点共用的外框：分类图标、标题、状态点、SLA/超时读数与左右 Handle。
 * 类别差异通过默认插槽注入，避免 18 类节点各写一份近似重复的模板。
 *
 * 尺寸与层级对齐 Dify 工作流节点（240px 宽、16px 圆角、handle 顶部 16px），
 * 类型色走边框和图标，状态色走状态点，实际执行时再叠加状态描边。
 */
import { Handle, Position } from '@vue-flow/core'
import { computed } from 'vue'

import { stateStyle, type NodeState } from '@/utils/graph'

import NodeIcon from './NodeIcon.vue'
import { resolveNodePresentation } from './presentation'

const props = defineProps<{
  label: string
  nodeType: string
  known: boolean
  state: NodeState | null
  slaMs: number
  timeoutMs: number
  attempts: number
  selected: boolean
}>()

const style = computed(() => stateStyle(props.state ?? 'pending'))
const stateClass = computed(() => (props.state === null ? 'is-idle' : style.value.className))
const badge = computed(() => (props.state === null ? '未运行' : style.value.label))
const presentation = computed(() => resolveNodePresentation(props.nodeType))
</script>

<template>
  <div
    class="wf-node"
    :class="[stateClass, { 'is-selected': selected, 'is-unknown': !known }]"
    :data-accent="presentation.accent"
    :style="{
      '--wf-accent': props.state === null ? 'var(--aeg-color-text-muted)' : style.color,
      '--wf-cat': `var(--aeg-color-editor-accent-${presentation.accent})`,
      '--wf-bloom': `var(--aeg-color-editor-bloom-${presentation.accent}, var(--wf-cat))`,
    }"
  >
    <span v-if="props.state === null && known" class="wf-node__rim" aria-hidden="true" />
    <Handle type="target" :position="Position.Left" class="wf-node__handle" :style="{ top: '16px' }" />
    <div class="wf-node__head">
      <span class="wf-node__icon"><NodeIcon :type="nodeType" /></span>
      <span class="wf-node__ident">
        <span class="wf-node__title" :title="label">{{ label }}</span>
        <span class="wf-node__type">{{ known ? nodeType : `未知类型 · ${nodeType}` }}</span>
      </span>
      <span class="wf-node__badge">
        <span class="wf-node__dot" aria-hidden="true"></span>{{ known ? badge : '只读' }}
      </span>
    </div>
    <div class="wf-node__body"><slot /></div>
    <div class="wf-node__meta">
      <span>SLA {{ slaMs }} ms</span>
      <span>超时 {{ timeoutMs }} ms</span>
      <span v-if="props.state !== null">已试 {{ attempts }} 次</span>
    </div>
    <Handle type="source" :position="Position.Right" class="wf-node__handle" :style="{ top: '16px' }" />
  </div>
</template>

<style scoped>
.wf-node {
  /* 宽度钉死：标题一长就撑盒会把相邻节点压叠；长标题改省略号，
     全文看 title 提示与右侧检查器。240px 与 Dify `NODE_WIDTH` 对齐。 */
  position: relative;
  width: var(--aeg-node-width, 240px);
  padding: 12px 12px 8px;
  /* 缩小画布时补偿描边，保持节点轮廓可辨。 */
  --wf-frame: color-mix(in srgb, var(--wf-cat) var(--wf-frame-weight, 52%), var(--aeg-color-editor-node-border));
  --wf-elevation: var(--aeg-shadow-sm);
  border: calc(1px * var(--canvas-stroke-k, 1)) solid var(--wf-frame);
  border-radius: 16px;
  background-color: var(--aeg-color-editor-node);
  background-image: radial-gradient(ellipse at 10% -24%, color-mix(in srgb, var(--wf-cat) 10%, transparent), transparent 73%);
  color: var(--aeg-color-text);
  font-size: 13px;
  line-height: 1.5;
  box-shadow: var(--wf-elevation), var(--wf-focus, 0 0 transparent);
  transition:
    border-color var(--aeg-motion-base) ease,
    box-shadow var(--aeg-motion-base) ease;
}
html[data-theme='dark'] .wf-node {
  --wf-frame-weight: 91%;
  --wf-elevation:
    inset 0 1px 13px color-mix(in srgb, var(--wf-frame) 25%, transparent),
    0 1px 9px color-mix(in srgb, var(--wf-frame) 44%, transparent),
    0 10px 26px -6px color-mix(in srgb, var(--wf-frame) 27%, transparent);
  background-image: radial-gradient(ellipse at 11% -13%, color-mix(in srgb, var(--wf-cat) 31%, transparent), transparent 69%);
}
html[data-theme='dark'] .wf-node.is-idle:not(.is-unknown) {
  --wf-frame-weight: 76%;
  --wf-rim-upper: 4% 5%;
  --wf-rim-lower: 5% 97%;
  --wf-rim-other: 96% 4%;
  --wf-rim-other-strength: 54%;
  --wf-elevation:
    inset 0 0 10px color-mix(in srgb, var(--wf-bloom) 21%, transparent),
    0 0 3px color-mix(in srgb, var(--wf-cat) 44%, transparent),
    0 3px 14px -4px color-mix(in srgb, var(--wf-bloom) 30%, transparent);
}
html[data-theme='dark'] .wf-node.is-idle[data-accent='blue'] {
  --wf-rim-upper: 90% 1%;
  --wf-rim-other: 5% 3%;
  --wf-rim-other-strength: 89%;
}
html[data-theme='dark'] .wf-node.is-idle[data-accent='amber'] {
  --wf-rim-upper: 9% 2%;
  --wf-rim-other: 93% 94%;
  --wf-rim-other-strength: 91%;
}
.wf-node__rim { display: none; }
html[data-theme='dark'] .wf-node__rim {
  display: block;
  position: absolute;
  z-index: -1;
  inset: -1px;
  border-radius: inherit;
  filter: blur(4px) drop-shadow(0 1px 10px color-mix(in srgb, var(--wf-bloom) 58%, transparent));
  pointer-events: none;
}
/* 同一组亮点分别画窄高光和宽散射，外晕随亮点分布。 */
html[data-theme='dark'] .wf-node.is-idle:not(.is-unknown)::before,
html[data-theme='dark'] .wf-node__rim::before {
  position: absolute;
  inset: var(--wf-rim-inset, -1px);
  padding: var(--wf-rim-width, 2px);
  border-radius: inherit;
  background:
    radial-gradient(ellipse 51% 53% at var(--wf-rim-upper), var(--wf-rim-ink), color-mix(in srgb, var(--wf-rim-ink) 58%, transparent) 34%, transparent 91%),
    radial-gradient(ellipse 55% 67% at var(--wf-rim-lower), var(--wf-rim-ink), color-mix(in srgb, var(--wf-rim-ink) 48%, transparent) 35%, transparent 88%),
    radial-gradient(ellipse 49% 52% at var(--wf-rim-other), color-mix(in srgb, var(--wf-rim-ink) var(--wf-rim-other-strength), transparent), transparent 85%);
  mask: linear-gradient(currentColor 0 0) content-box, linear-gradient(currentColor 0 0);
  mask-composite: exclude;
  pointer-events: none;
  content: '';
}
html[data-theme='dark'] .wf-node.is-idle:not(.is-unknown)::before {
  --wf-rim-ink: color-mix(in srgb, var(--wf-cat) 14%, var(--aeg-color-text));
}
html[data-theme='dark'] .wf-node__rim::before {
  --wf-rim-ink: var(--wf-bloom);
  --wf-rim-inset: -5px;
  --wf-rim-width: 11px;
}
html[data-theme='dark'] .wf-node.is-idle:not(.is-unknown)::after {
  position: absolute;
  z-index: -1;
  inset: calc(100% - 4px) -44px auto;
  height: 96px;
  background: radial-gradient(ellipse at 49% 38%, color-mix(in srgb, var(--wf-bloom) 57%, transparent), color-mix(in srgb, var(--wf-bloom) 24%, transparent) 34%, transparent 62%);
  transform: rotate(-7deg);
  pointer-events: none;
  content: '';
}
.wf-node.is-running { --wf-frame: var(--aeg-color-editor-state-running); }
.wf-node:is(.is-failed, .is-timeout) { --wf-frame: var(--aeg-color-status-danger); }
.wf-node.is-awaiting-human { --wf-frame: var(--aeg-color-editor-state-awaiting); }
.wf-node:is(.is-succeeded, .is-degraded, .is-bypassed) { --wf-frame: var(--aeg-color-status-success); }
.wf-node.is-selected { --wf-focus: 0 0 0 2px var(--aeg-color-brand); }
.wf-node.is-unknown {
  border-style: dashed;
}
.wf-node.is-unknown .wf-node__type {
  color: var(--aeg-color-status-warning);
}
.wf-node__head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.wf-node__icon {
  display: grid;
  flex: 0 0 auto;
  place-items: center;
  width: 32px;
  height: 32px;
  border-radius: 8px;
  border: 1px solid color-mix(in srgb, var(--wf-cat) 32%, transparent);
  background: linear-gradient(145deg, color-mix(in srgb, var(--wf-cat) 24%, transparent), color-mix(in srgb, var(--wf-cat) 9%, transparent));
  color: var(--wf-cat);
}
.wf-node__ident {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  min-width: 0;
}
.wf-node__title {
  overflow: hidden;
  color: var(--aeg-color-text);
  font-size: 15px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.wf-node__type {
  overflow: hidden;
  color: var(--aeg-color-text-muted);
  font-family: var(--aeg-font-mono);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.wf-node__badge {
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 4px;
  color: var(--aeg-color-text-secondary);
  font-size: 11px;
  white-space: nowrap;
}
.wf-node__dot {
  width: 6px;
  height: 6px;
  border-radius: 999px;
  background: var(--wf-accent);
}
.wf-node__body:not(:empty) {
  margin-top: 8px;
}
.wf-node__meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 8px;
  padding-top: 6px;
  border-top: 1px solid var(--aeg-color-border-subtle);
  color: var(--aeg-color-text-muted);
  font-size: 11px;
}
html[data-theme='dark'] .wf-node__meta { border-color: color-mix(in srgb, var(--wf-cat) 21%, var(--aeg-color-editor-node-border)); }
.wf-node__handle {
  width: 10px;
  height: 10px;
  border: 2px solid var(--aeg-color-editor-node);
  background: var(--aeg-color-editor-edge);
}
.wf-node:hover .wf-node__handle,
.wf-node.is-selected .wf-node__handle {
  background: var(--aeg-color-brand);
}
</style>
