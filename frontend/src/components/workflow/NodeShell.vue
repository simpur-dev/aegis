<script setup lang="ts">
/**
 * 全部画布节点共用的外框：分类图标、标题、状态点、SLA/超时读数与左右 Handle。
 * 类别差异通过默认插槽注入，避免 18 类节点各写一份近似重复的模板。
 *
 * 尺寸与层级对齐 Dify 工作流节点（240px 宽、16px 圆角、handle 顶部 16px），
 * 颜色只走 `--aeg-*` 令牌：暗色主题下由令牌翻面，组件里不再写一套 hex。
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
    :style="{
      '--wf-accent': props.state === null ? 'var(--aeg-color-text-muted)' : style.color,
      '--wf-cat': `var(--aeg-color-editor-accent-${presentation.accent})`,
    }"
  >
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
  width: var(--aeg-node-width, 240px);
  padding: 12px 12px 8px;
  /* 描边按 `--canvas-stroke-k`（1/zoom，封顶 3）补偿：zoom 作用在整张变换层上，
     写死 1px 的卡片描边在"看全局"那一档（fit 到 0.48）只剩 0.48 设备像素，
     一整片节点会糊成一块块没有边的白板；类别条同一套补偿，否则 3px 的色条同样看不见。 */
  border: calc(1px * var(--canvas-stroke-k, 1)) solid var(--aeg-color-editor-node-border);
  border-left: calc(3px * var(--canvas-stroke-k, 1)) solid var(--wf-accent, var(--aeg-color-text-muted));
  border-radius: 16px;
  background: var(--aeg-color-editor-node);
  color: var(--aeg-color-text);
  font-size: 12px;
  line-height: 1.5;
  box-shadow: var(--aeg-shadow-sm);
  transition:
    border-color var(--aeg-motion-base) ease,
    box-shadow var(--aeg-motion-base) ease;
}
.wf-node.is-selected {
  border-color: var(--aeg-color-brand);
  box-shadow: 0 0 0 2px var(--aeg-color-brand-line);
}
.wf-node.is-running {
  border-color: var(--aeg-color-editor-state-running);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--aeg-color-editor-state-running) 28%, transparent);
}
.wf-node.is-failed,
.wf-node.is-timeout {
  border-color: var(--aeg-color-status-danger);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--aeg-color-status-danger) 24%, transparent);
}
.wf-node.is-awaiting-human {
  border-color: var(--aeg-color-editor-state-awaiting);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--aeg-color-editor-state-awaiting) 26%, transparent);
}
.wf-node.is-succeeded,
.wf-node.is-degraded,
.wf-node.is-bypassed {
  border-color: var(--aeg-color-status-success);
}
.wf-node.is-idle .wf-node__title {
  color: var(--aeg-color-text-secondary);
}
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
  width: 28px;
  height: 28px;
  border-radius: 8px;
  background: color-mix(in srgb, var(--wf-cat) 14%, transparent);
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
  font-size: 13px;
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
