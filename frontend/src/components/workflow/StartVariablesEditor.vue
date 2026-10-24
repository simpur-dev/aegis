<script setup lang="ts">
/**
 * Start 节点的输入变量表：名字、标签、类型、必填。
 *
 * 为什么不再是"贴一段 JSON"：这几项直接决定运行时表单长什么样、payload 里有哪些键，
 * 而后端对名字有硬约束（字母/下划线开头、≤32 字符、不许是 `__proto__` 这类原型链名字）。
 * 让值班员手填 JSON 的结果是发布吃 422 或——更糟——一个能发布但永远取不到值的变量。
 * 每行都沿用原对象再改那几格，所以 `default` 这类本编辑器不管的键不会在改写时丢掉。
 */
import { computed } from 'vue'

import type { JsonValue } from '@/utils/graph'

const START_TYPES = ['string', 'number', 'boolean', 'json']

const props = defineProps<{
  modelValue: unknown
  disabled: boolean
  /** 变量表里有不合规的行：给整块一道红框，具体哪一行由外层那句话点名。 */
  invalid?: boolean
}>()

const emit = defineEmits<{ 'update:modelValue': [value: Record<string, JsonValue>[]] }>()

const rows = computed<Record<string, JsonValue>[]>(() =>
  Array.isArray(props.modelValue)
    ? props.modelValue.map((item) =>
        item !== null && typeof item === 'object' && !Array.isArray(item) ? { ...(item as Record<string, JsonValue>) } : {},
      )
    : [],
)

function write(next: Record<string, JsonValue>[]): void {
  emit('update:modelValue', next)
}

function edit(index: number, key: 'name' | 'label' | 'type', value: string): void {
  const next = rows.value.map((row) => ({ ...row }))
  const row = next[index]
  if (row === undefined) return
  row[key] = value
  write(next)
}

function toggleRequired(index: number, value: boolean): void {
  const next = rows.value.map((row) => ({ ...row }))
  const row = next[index]
  if (row === undefined) return
  if (value) row.required = true
  else delete row.required
  write(next)
}

function add(): void {
  write([...rows.value, { name: '', type: 'string' }])
}

function remove(index: number): void {
  write(rows.value.filter((_, position) => position !== index))
}

function nameLooksWrong(value: string): boolean {
  return !/^[A-Za-z_][A-Za-z0-9_]{0,31}$/.test(value)
}
</script>

<template>
  <div class="wf-startvars" :class="{ 'is-invalid': invalid }">
    <p class="wf-startvars__count" data-testid="start-var-count">
      已声明 {{ rows.length }} 个变量（上限 32）
    </p>
    <ul class="wf-startvars__rows">
      <li v-for="(row, index) in rows" :key="index" class="wf-startvars__row">
        <input
          class="wf-startvars__name"
          :class="{ 'is-invalid': nameLooksWrong(String(row.name ?? '')) }"
          type="text"
          :value="String(row.name ?? '')"
          placeholder="变量名"
          :disabled="disabled"
          aria-label="变量名"
          :data-testid="`var-name-${index}`"
          @input="edit(index, 'name', ($event.target as HTMLInputElement).value)"
        />
        <input
          class="wf-startvars__label"
          type="text"
          :value="String(row.label ?? '')"
          placeholder="显示标签（选填）"
          :disabled="disabled"
          aria-label="显示标签"
          :data-testid="`var-label-${index}`"
          @input="edit(index, 'label', ($event.target as HTMLInputElement).value)"
        />
        <select
          class="wf-startvars__type"
          :value="row.type === undefined ? 'string' : String(row.type)"
          :disabled="disabled"
          aria-label="变量类型"
          :data-testid="`var-type-${index}`"
          @change="edit(index, 'type', ($event.target as HTMLSelectElement).value)"
        >
          <option v-for="type in START_TYPES" :key="type" :value="type">{{ type }}</option>
        </select>
        <label class="wf-startvars__required">
          <input
            type="checkbox"
            :checked="row.required === true"
            :disabled="disabled"
            aria-label="必填"
            :data-testid="`var-required-${index}`"
            @change="toggleRequired(index, ($event.target as HTMLInputElement).checked)"
          />
          必填
        </label>
        <button
          type="button"
          class="wf-startvars__remove"
          :disabled="disabled"
          aria-label="删除这个变量"
          :data-testid="`var-remove-${index}`"
          @click="remove(index)"
        >
          ×
        </button>
      </li>
    </ul>
    <button type="button" class="wf-startvars__add" :disabled="disabled" data-testid="var-add" @click="add">
      新增变量
    </button>
  </div>
</template>

<style scoped>
.wf-startvars {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
/* 整块红框：具体哪一行不对，由外层那一行点名（名字形状、重名、超上限…）。 */
.wf-startvars.is-invalid {
  border-radius: 4px;
  box-shadow: 0 0 0 1px var(--aeg-color-status-danger);
}
.wf-startvars__count,
.wf-startvars__rows {
  margin: 0;
  padding: 0;
  list-style: none;
}
.wf-startvars__count {
  color: var(--aeg-color-text-muted);
  font-size: 11px;
}
.wf-startvars__row {
  display: grid;
  grid-template-columns: 1fr 1fr 72px auto auto;
  gap: 4px;
  align-items: center;
}
.wf-startvars__name,
.wf-startvars__label,
.wf-startvars__type {
  min-width: 0;
  padding: 2px 6px;
  border: 1px solid var(--aeg-color-border-strong);
  border-radius: 4px;
  background: var(--aeg-color-elevated);
  color: var(--aeg-color-text);
  font-size: 12px;
}
.wf-startvars__name {
  font-family: var(--aeg-font-mono);
}
.wf-startvars__name.is-invalid {
  border-color: var(--aeg-color-status-danger);
}
.wf-startvars__required {
  display: flex;
  gap: 3px;
  align-items: center;
  color: var(--aeg-color-text-secondary);
  font-size: 11px;
}
.wf-startvars__remove {
  width: 20px;
  height: 20px;
  border: 1px solid var(--aeg-color-border);
  border-radius: 4px;
  background: var(--aeg-color-elevated);
  color: var(--aeg-color-text-muted);
  font-size: 14px;
  line-height: 1;
  cursor: pointer;
}
.wf-startvars__remove:hover:not(:disabled) {
  border-color: var(--aeg-color-status-danger);
  color: var(--aeg-color-status-danger);
}
.wf-startvars__add {
  align-self: flex-start;
  padding: 1px 6px;
  border: 1px dashed var(--aeg-color-border-strong);
  border-radius: 999px;
  background: transparent;
  color: var(--aeg-color-text-secondary);
  font-size: 11px;
  cursor: pointer;
}
.wf-startvars__add:hover:not(:disabled) {
  border-color: var(--aeg-color-brand);
  color: var(--aeg-color-brand-hover);
}
</style>
