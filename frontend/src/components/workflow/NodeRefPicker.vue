<script setup lang="ts">
/**
 * 点名上游节点的格子（threshold/branch 的单值、join/risk_assess 的多值）。
 *
 * 候选只有真实入边：`upstream` 指了没连线的节点，后端在定义期就拒（`_validate_wired_upstreams`），
 * 现场症状却是"流程跑到汇聚那步才卡住"。把候选收成入边之后，画布与参数第一次说同一句话；
 * 存量定义里那些已经失效的引用仍原样显示出来并标红，不静默吞掉——吞掉就等于替人改了参数。
 */
import { computed } from 'vue'

import type { NodeRefCandidate } from './registry'

const props = defineProps<{
  multiple: boolean
  modelValue: unknown
  candidates: readonly NodeRefCandidate[]
  disabled: boolean
  /** 引用了没连线的节点：染红这一格，那句话由外层统一说。 */
  invalid?: boolean
}>()

const emit = defineEmits<{ 'update:modelValue': [value: string | string[] | null] }>()

const rows = computed(() => {
  const selected = new Set(currentIds())
  const known = new Set(props.candidates.map((candidate) => candidate.id))
  const listed = props.candidates.map((candidate) => ({ ...candidate, stale: false, checked: selected.has(candidate.id) }))
  for (const id of currentIds()) {
    if (known.has(id)) continue
    listed.push({ id, label: id, type: '', stale: true, checked: true })
  }
  return listed
})

function currentIds(): string[] {
  if (Array.isArray(props.modelValue)) {
    return props.modelValue.filter((item): item is string => typeof item === 'string' && item !== '')
  }
  return typeof props.modelValue === 'string' && props.modelValue !== '' ? [props.modelValue] : []
}

const singleValue = computed(() => currentIds()[0] ?? '')

function onSingle(event: Event): void {
  const raw = (event.target as HTMLSelectElement).value
  emit('update:modelValue', raw === '' ? null : raw)
}

function toggle(id: string, checked: boolean): void {
  const next = currentIds()
  if (checked) {
    if (!next.includes(id)) emit('update:modelValue', [...next, id])
    return
  }
  emit('update:modelValue', next.filter((item) => item !== id))
}
</script>

<template>
  <div class="wf-noderef">
    <select
      v-if="!multiple"
      class="wf-noderef__select"
      :class="{ 'is-stale': invalid }"
      :value="singleValue"
      :disabled="disabled"
      :aria-invalid="invalid"
      @change="onSingle"
    >
      <option value="">（不指定，用实例 payload）</option>
      <option v-for="row in rows" :key="row.id" :value="row.id">
        {{ row.stale ? `${row.id}（没有连线）` : `${row.label} · ${row.id}` }}
      </option>
    </select>
    <ul v-else class="wf-noderef__list">
      <li v-for="row in rows" :key="row.id" :class="{ 'is-stale': row.stale }">
        <label class="wf-noderef__check">
          <input
            type="checkbox"
            :checked="row.checked"
            :disabled="disabled"
            :data-testid="`ref-${row.id}`"
            @change="toggle(row.id, ($event.target as HTMLInputElement).checked)"
          />
          <span>{{ row.label }}</span>
          <code>{{ row.id }}</code>
          <em v-if="row.stale">没有连线</em>
        </label>
      </li>
    </ul>
    <p v-if="candidates.length === 0" class="wf-noderef__empty" data-testid="node-ref-empty">
      还没有连到本节点的进边：先在画布上连线，再回来点这一格。
    </p>
  </div>
</template>

<style scoped>
.wf-noderef {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.wf-noderef__select {
  padding: 2px 6px;
  border: 1px solid var(--aeg-color-border-strong);
  border-radius: 4px;
  background: var(--aeg-color-elevated);
  color: var(--aeg-color-text);
  font-size: 12px;
}
.wf-noderef__select.is-stale {
  border-color: var(--aeg-color-status-danger);
}
.wf-noderef__list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.wf-noderef__check {
  display: flex;
  gap: 6px;
  align-items: baseline;
  color: var(--aeg-color-text-secondary);
  font-size: 12px;
  cursor: pointer;
}
.wf-noderef__check code {
  font-family: var(--aeg-font-mono);
  font-size: 11px;
}
.wf-noderef__check em {
  color: var(--aeg-color-status-danger);
  font-size: 11px;
  font-style: normal;
}
.wf-noderef__empty {
  margin: 0;
  color: var(--aeg-color-text-muted);
  font-size: 11px;
  line-height: 1.6;
}
</style>
