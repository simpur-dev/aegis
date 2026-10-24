<script setup lang="ts">
/** 选中节点的定义编辑器：名称/失败策略/SLA/超时/重试 + 类型参数 + 参数体检。运行中操作见 RuntimeActions。 */
import { computed } from 'vue'

import { useUpstreamVariables } from '@/composables/workflow/useUpstreamVariables'
import {
  findNode,
  inboundSources,
  MAX_ATTEMPTS,
  MAX_BACKOFF_MS,
  MAX_NODE_NAME_CHARS,
  ON_FAILURE_LABELS,
  SLA_MAX_MS,
  SLA_MIN_MS,
  type JsonValue,
  type NodeDef,
  type NodePatch,
  type OnFailure,
} from '@/utils/graph'

import ConfigFields from './ConfigFields.vue'
import RuntimeActions from './RuntimeActions.vue'
import ValidationList from './ValidationList.vue'
import { nodeConfigIssues, referenceIssues } from './configValidation'
import { useEditorContext } from './editor/context'
import { nodeMeta, type NodeRefCandidate } from './registry'

const { catalog, document: documentStore, editor, run } = useEditorContext()

const node = computed<NodeDef | null>(() => {
  const def = documentStore.definition
  if (def === null || editor.selectedNodeId === null) return null
  return findNode(def, editor.selectedNodeId)
})
const meta = computed(() => (node.value === null ? null : nodeMeta(node.value.type)))
const known = computed(() => meta.value !== null)
const failureOptions = computed(() => Object.entries(ON_FAILURE_LABELS) as [OnFailure, string][])
/** 只有声明了 upstream 的类型才需要与真实连线对齐（join 缺上游会在运行期才报错）。 */
const canSyncUpstream = computed(() => known.value && node.value !== null && 'upstream' in node.value.config)

/**
 * `upstream` 那几格的候选=真实入边。这里用入边而不是全部祖先：后端判的也是入边
 * （`engine._validate_wired_upstreams`），隔一层祖先能选、连线却没有，症状一模一样。
 */
const candidates = computed<NodeRefCandidate[]>(() => {
  const def = documentStore.definition
  const current = node.value
  if (def === null || current === null) return []
  return inboundSources(def, current.node_id).map((id) => {
    const source = findNode(def, id)
    return { id, label: source?.name ?? id, type: source?.type ?? '' }
  })
})

/** 上次运行各节点真的产出了哪些键：catalog 没声明的键（如 chain_stage 的整包）也从这里补进来。 */
const lastRunOutputs = computed<Record<string, Record<string, JsonValue>> | null>(() => {
  const instance = run.instance
  return instance === null ? null : Object.fromEntries(instance.nodes.map((row) => [row.node_id, row.output]))
})

const upstreamVariables = useUpstreamVariables({
  definition: () => documentStore.definition,
  targetNodeId: () => editor.selectedNodeId,
  catalog: () => catalog.nodeTypes,
  lastRunOutputs: () => lastRunOutputs.value,
})

/**
 * 参数体检 = 取值判据 + 变量引用判据。
 *
 * 引用为什么要单独判：`{{#fetch_1.rows#}}` 写错一个字母，发布与运行都不会当场报错，
 * 只会在实例日志里变成空值。选择器点出来的不会错，所以要拦的是手敲的那几只。
 */
const issues = computed(() => {
  const current = node.value
  if (current === null) return []
  const inbound = candidates.value.map((row) => row.id)
  return [
    ...nodeConfigIssues(current, { inbound }),
    ...referenceIssues(current, { ancestors: upstreamVariables.value.map((group) => group.nodeId) }),
  ]
})

function patch(next: NodePatch): void {
  if (node.value !== null) documentStore.patchNode(node.value.node_id, next)
}

function removeNode(): void {
  const current = node.value
  if (current === null) return
  documentStore.removeNodes([current.node_id])
  editor.select(documentStore.definition?.nodes[0]?.node_id ?? null)
}

function numberValue(event: Event): number | null {
  const input = event.target as HTMLInputElement
  const parsed = Number(input.value)
  return input.value === '' || Number.isNaN(parsed) ? null : parsed
}

function onName(event: Event): void {
  patch({ name: (event.target as HTMLInputElement).value.slice(0, MAX_NODE_NAME_CHARS) })
}

function onFailure(event: Event): void {
  const raw = (event.target as HTMLSelectElement).value
  if (raw in ON_FAILURE_LABELS) patch({ on_failure: raw as OnFailure })
}

function onBudget(event: Event, key: 'sla_ms' | 'timeout_ms'): void {
  const parsed = numberValue(event)
  if (parsed === null) return
  // 取值域与后端一致（workflow_api.py 的 NodeInput），越界直接夹住，省一轮 422。
  // 超时那一格的上限还要压到本节点的 SLA：界面上写着"上限＝SLA"，就得真是那个数。
  const cap = key === 'timeout_ms' ? Math.min(node.value?.sla_ms ?? SLA_MAX_MS, SLA_MAX_MS) : SLA_MAX_MS
  const bounded = Math.min(Math.max(parsed, SLA_MIN_MS), cap)
  patch(key === 'sla_ms' ? { sla_ms: bounded } : { timeout_ms: bounded })
}

function onRetry(event: Event, key: 'max_attempts' | 'backoff_ms'): void {
  const current = node.value?.retry
  const parsed = numberValue(event)
  if (current === undefined || parsed === null) return
  // 与 onBudget 同一手法：越界先夹住，别让人白敲一次再吃 validateGraph 的红字。
  // 上界取自 utils/graph 的那批常量（有跨端门禁对着 model.py 校验）。
  const clamp = (value: number, max: number) => Math.min(Math.max(value, 0), max)
  const next =
    key === 'max_attempts'
      ? { ...current, max_attempts: clamp(parsed, MAX_ATTEMPTS) }
      : { ...current, backoff_ms: clamp(parsed, MAX_BACKOFF_MS) }
  patch({ retry: next })
}

function onConfig(next: Record<string, JsonValue>): void {
  patch({ config: next })
}
</script>

<template>
  <section v-if="node === null" class="wf-inspector">
    <p class="wf-inspector__empty">在画布上选中一个节点后可在此编辑参数。</p>
  </section>
  <section v-else class="wf-inspector">
    <header class="wf-inspector__head">
      <h3 class="wf-inspector__title">{{ meta?.label ?? node.type }}</h3>
      <code class="wf-inspector__id">{{ node.node_id }}</code>
    </header>
    <p class="wf-inspector__desc">{{ catalog.descriptionByType[node.type] ?? meta?.description }}</p>

    <template v-if="known">
      <label class="wf-inspector__row">
        <span>节点显示名</span>
        <input class="wf-inspector__input" type="text" :maxlength="MAX_NODE_NAME_CHARS" :value="node.name" @input="onName" />
      </label>

      <div class="wf-inspector__grid">
        <label class="wf-inspector__row">
          <span>SLA（毫秒）</span>
          <input
            class="wf-inspector__input"
            type="number"
            :min="SLA_MIN_MS"
            :max="SLA_MAX_MS"
            :value="node.sla_ms"
            @change="onBudget($event, 'sla_ms')"
          />
        </label>
        <label class="wf-inspector__row">
          <span>超时（毫秒，上限＝SLA）</span>
          <input
            class="wf-inspector__input"
            type="number"
            :min="SLA_MIN_MS"
            :max="node.sla_ms"
            :value="node.timeout_ms"
            @change="onBudget($event, 'timeout_ms')"
          />
        </label>
      </div>
      <p class="wf-inspector__hint">超时是单次执行硬上限，必须不大于 SLA。</p>

      <label class="wf-inspector__row">
        <span>失败策略</span>
        <select class="wf-inspector__input" :value="node.on_failure" @change="onFailure">
          <option v-for="[value, label] in failureOptions" :key="value" :value="value">{{ label }}</option>
        </select>
      </label>

      <div class="wf-inspector__grid">
        <label class="wf-inspector__row">
          <span>重试次数（不含首次）</span>
          <input
            class="wf-inspector__input"
            type="number"
            min="0"
            :max="MAX_ATTEMPTS"
            :value="node.retry.max_attempts"
            @change="onRetry($event, 'max_attempts')"
          />
        </label>
        <label class="wf-inspector__row">
          <span>重试退避（毫秒）</span>
          <input
            class="wf-inspector__input"
            type="number"
            min="0"
            :max="MAX_BACKOFF_MS"
            :value="node.retry.backoff_ms"
            @change="onRetry($event, 'backoff_ms')"
          />
        </label>
      </div>
      <p class="wf-inspector__hint">
        重试 0 表示不重试；这一条会随定义一起保存（写接口认 retry），改动后仍需点"保存定义"才落到服务端。
      </p>

      <fieldset class="wf-inspector__config">
        <legend>节点参数</legend>
        <ValidationList :issues="issues" />
        <ConfigFields
          :def="node"
          :disabled="false"
          :issues="issues"
          :candidates="candidates"
          :variables="upstreamVariables"
          @update:config="onConfig"
        />
      </fieldset>
    </template>

    <p v-else class="wf-inspector__unknown" role="status">
      此节点类型 <code>{{ node.type }}</code> 不在当前前端注册表中，已按只读降级显示。升级前端后可编辑参数；这里仍可删除它。
    </p>

    <div class="wf-inspector__actions">
      <button v-if="canSyncUpstream" type="button" class="wf-inspector__button" @click="documentStore.syncUpstreamConfig(node.node_id)">
        按连线回填 upstream
      </button>
      <button type="button" class="wf-inspector__button wf-inspector__button--danger" @click="removeNode">
        删除节点
      </button>
    </div>

    <RuntimeActions :node="node" />
  </section>
</template>

<style scoped>
.wf-inspector {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px;
  overflow-y: auto;
}
.wf-inspector__empty {
  margin: 12px;
  color: var(--aeg-color-text-secondary);
  font-size: 12px;
}
.wf-inspector__head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
}
.wf-inspector__title {
  margin: 0;
  font-size: 14px;
}
.wf-inspector__id {
  color: var(--aeg-color-text-secondary);
  font-size: 11px;
}
.wf-inspector__desc {
  margin: 0;
  color: var(--aeg-color-text-secondary);
  font-size: 12px;
}
.wf-inspector__unknown {
  margin: 0;
  padding: 8px;
  border: 1px dashed var(--aeg-color-status-warning);
  border-radius: 6px;
  color: var(--aeg-color-text-secondary);
  font-size: 12px;
}
.wf-inspector__row {
  display: flex;
  flex-direction: column;
  gap: 2px;
  color: var(--aeg-color-text-secondary);
  font-size: 12px;
}
.wf-inspector__grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}
.wf-inspector__input {
  padding: 2px 6px;
  border: 1px solid #d9d9d9;
  border-radius: 4px;
  font-size: 12px;
}
.wf-inspector__hint {
  margin: 0;
  color: var(--aeg-color-text-secondary);
  font-size: 11px;
}
.wf-inspector__config {
  margin: 4px 0 0;
  padding: 8px;
  border: 1px solid #f0f0f0;
  border-radius: 4px;
}
.wf-inspector__config legend {
  padding: 0 4px;
  color: var(--aeg-color-text-secondary);
  font-size: 12px;
}
.wf-inspector__actions {
  display: flex;
  gap: 8px;
}
.wf-inspector__button {
  padding: 2px 8px;
  border: 1px solid #d9d9d9;
  border-radius: 4px;
  background: #fff;
  font-size: 12px;
  cursor: pointer;
}
.wf-inspector__button--danger {
  border-color: #ffa39e;
  color: #cf1322;
}
</style>
