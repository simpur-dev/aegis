<script setup lang="ts">
/** 选中节点的定义编辑器：名称/失败策略/SLA/超时/重试 + 类型参数。运行中操作见 RuntimeActions。 */
import { computed } from 'vue'

import { useWorkflowStore } from '@/stores/workflow'
import {
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
import { nodeMeta } from './registry'

const store = useWorkflowStore()

const node = computed<NodeDef | null>(() => store.selectedNode)
const meta = computed(() => (node.value === null ? null : nodeMeta(node.value.type)))
const failureOptions = computed(() => Object.entries(ON_FAILURE_LABELS) as [OnFailure, string][])
/** 只有声明了 upstream 的类型才需要与真实连线对齐（join 缺上游会在运行期才报错）。 */
const canSyncUpstream = computed(() => node.value !== null && 'upstream' in node.value.config)

function patch(next: NodePatch): void {
  store.patchSelected(next)
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
    <p class="wf-inspector__desc">{{ store.descriptionByType[node.type] ?? meta?.description }}</p>

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
    <p class="wf-inspector__hint">超时是单次执行硬上限，必须不大于 SLA（model.py:61-66）。</p>

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
      <ConfigFields :def="node" :disabled="false" @update:config="onConfig" />
    </fieldset>

    <div class="wf-inspector__actions">
      <button v-if="canSyncUpstream" type="button" class="wf-inspector__button" @click="store.syncUpstreamConfig(node.node_id)">
        按连线回填 upstream
      </button>
      <button type="button" class="wf-inspector__button wf-inspector__button--danger" @click="store.removeSelected()">
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
  color: #5a6072;
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
  color: #5a6072;
  font-size: 11px;
}
.wf-inspector__desc {
  margin: 0;
  color: #595959;
  font-size: 12px;
}
.wf-inspector__row {
  display: flex;
  flex-direction: column;
  gap: 2px;
  color: #595959;
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
  color: #5a6072;
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
  color: #595959;
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
