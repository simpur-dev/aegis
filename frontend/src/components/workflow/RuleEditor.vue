<script setup lang="ts">
/**
 * 规则/条件表：branch 的 `rules` 与 threshold 的 `conditions`。
 *
 * 这两格原来是裸 JSON 文本域。值班员改一个阈值要先数对方括号和逗号，
 * 少打一个引号的后果是 JsonField 只在本地提示"未提交"，画布上那条旧条件继续生效——
 * 于是"我明明把 30 改成了 50"与"预警为什么没响"同时成立。
 * 逐行成表之后，每格都是普通输入框，算子与聚合下拉里只有后端真认识的取值（`nodes._compare`）。
 *
 * 写回时沿用原行对象再改这几格：规则里可能有本编辑器不管的键，不能被"整理格式"顺手删掉。
 */
import { computed } from 'vue'

import type { JsonValue } from '@/utils/graph'

import { COMPARE_OPS, THRESHOLD_AGGS } from './configValidation'

const props = defineProps<{
  kind: 'rules' | 'conditions'
  modelValue: unknown
  disabled: boolean
  /** 有规则写残了：给表格外圈一道红，具体哪一条哪一格由外层那句话点名。 */
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

const emptyHint = computed(() =>
  props.kind === 'rules' ? '一条规则都没有：这颗节点只会走兜底分支。' : '一条条件都没有：任何读数都不会触发。',
)

function write(next: Record<string, JsonValue>[]): void {
  emit('update:modelValue', next)
}

function edit(index: number, key: string, value: JsonValue): void {
  const next = rows.value.map((row) => ({ ...row }))
  const row = next[index]
  if (row === undefined) return
  row[key] = value
  write(next)
}

function add(): void {
  const blank: Record<string, JsonValue> =
    props.kind === 'rules' ? { when: '', op: '==', value: '', then: '' } : { metric: '', op: '>=', threshold: 0, agg: 'max' }
  write([...rows.value, blank])
}

function remove(index: number): void {
  write(rows.value.filter((_, position) => position !== index))
}

/** 数字/布尔/字符串都得能表达：`value: 2` 与 `value: "2"` 在比较时是两回事。 */
function renderScalar(value: JsonValue | undefined): string {
  if (value === undefined) return ''
  if (typeof value === 'string') return value
  return JSON.stringify(value)
}

function parseScalar(raw: string): JsonValue {
  const trimmed = raw.trim()
  if (trimmed === '') return ''
  try {
    return JSON.parse(trimmed) as JsonValue
  } catch {
    return raw
  }
}

function cellText(row: Record<string, JsonValue>, key: string): string {
  return renderScalar(row[key])
}
</script>

<template>
  <div class="wf-rules" :class="{ 'is-invalid': invalid }" :data-testid="`rule-editor-${kind}`">
    <p v-if="rows.length === 0" class="wf-rules__empty">{{ emptyHint }}</p>
    <table v-else class="wf-rules__table">
      <thead>
        <tr>
          <th class="wf-rules__th">#</th>
          <template v-if="kind === 'rules'">
            <th class="wf-rules__th">取值路径</th>
            <th class="wf-rules__th">算子</th>
            <th class="wf-rules__th">比较值</th>
            <th class="wf-rules__th">走哪个分支</th>
          </template>
          <template v-else>
            <th class="wf-rules__th">指标</th>
            <th class="wf-rules__th">算子</th>
            <th class="wf-rules__th">阈值</th>
            <th class="wf-rules__th">聚合</th>
          </template>
          <th class="wf-rules__th" />
        </tr>
      </thead>
      <tbody>
        <tr v-for="(row, index) in rows" :key="index">
          <td class="wf-rules__index">{{ index + 1 }}</td>
          <template v-if="kind === 'rules'">
            <td>
              <input
                class="wf-rules__cell"
                type="text"
                :value="cellText(row, 'when')"
                placeholder="risk_level"
                :disabled="disabled"
                :aria-label="`第 ${index + 1} 条规则的取值路径`"
                :data-testid="`rule-when-${index}`"
                @input="edit(index, 'when', ($event.target as HTMLInputElement).value)"
              />
            </td>
            <td>
              <select
                class="wf-rules__cell"
                :value="typeof row.op === 'string' ? row.op : '=='"
                :disabled="disabled"
                aria-label="算子"
                :data-testid="`rule-op-${index}`"
                @change="edit(index, 'op', ($event.target as HTMLSelectElement).value)"
              >
                <option v-for="op in COMPARE_OPS" :key="op" :value="op">{{ op }}</option>
              </select>
            </td>
            <td>
              <input
                class="wf-rules__cell"
                type="text"
                :value="cellText(row, 'value')"
                placeholder="2 / true / sms"
                :disabled="disabled"
                :aria-label="`第 ${index + 1} 条规则的比较值`"
                :data-testid="`rule-value-${index}`"
                @input="edit(index, 'value', parseScalar(($event.target as HTMLInputElement).value))"
              />
            </td>
            <td>
              <input
                class="wf-rules__cell"
                type="text"
                :value="cellText(row, 'then')"
                placeholder="分支名"
                :disabled="disabled"
                :aria-label="`第 ${index + 1} 条规则的分支名`"
                :data-testid="`rule-then-${index}`"
                @input="edit(index, 'then', ($event.target as HTMLInputElement).value)"
              />
            </td>
          </template>
          <template v-else>
            <td>
              <input
                class="wf-rules__cell"
                type="text"
                :value="cellText(row, 'metric')"
                placeholder="rain_10min"
                :disabled="disabled"
                :aria-label="`第 ${index + 1} 条条件的指标`"
                :data-testid="`condition-metric-${index}`"
                @input="edit(index, 'metric', ($event.target as HTMLInputElement).value)"
              />
            </td>
            <td>
              <select
                class="wf-rules__cell"
                :value="typeof row.op === 'string' ? row.op : '>='"
                :disabled="disabled"
                aria-label="算子"
                :data-testid="`condition-op-${index}`"
                @change="edit(index, 'op', ($event.target as HTMLSelectElement).value)"
              >
                <option v-for="op in COMPARE_OPS" :key="op" :value="op">{{ op }}</option>
              </select>
            </td>
            <td>
              <input
                class="wf-rules__cell"
                type="text"
                :value="cellText(row, 'threshold')"
                placeholder="30"
                :disabled="disabled"
                aria-label="阈值"
                :data-testid="`condition-threshold-${index}`"
                @input="edit(index, 'threshold', parseScalar(($event.target as HTMLInputElement).value))"
              />
            </td>
            <td>
              <select
                class="wf-rules__cell"
                :value="typeof row.agg === 'string' ? row.agg : 'max'"
                :disabled="disabled"
                aria-label="聚合"
                :data-testid="`condition-agg-${index}`"
                @change="edit(index, 'agg', ($event.target as HTMLSelectElement).value)"
              >
                <option v-for="agg in THRESHOLD_AGGS" :key="agg" :value="agg">{{ agg }}</option>
              </select>
            </td>
          </template>
          <td>
            <button
              type="button"
              class="wf-rules__remove"
              :disabled="disabled"
              aria-label="删除这一条"
              :data-testid="`rule-remove-${index}`"
              @click="remove(index)"
            >
              ×
            </button>
          </td>
        </tr>
      </tbody>
    </table>
    <button type="button" class="wf-rules__add" :disabled="disabled" :data-testid="`rule-add-${kind}`" @click="add">
      {{ kind === 'rules' ? '新增一条规则' : '新增一条条件' }}
    </button>
  </div>
</template>

<style scoped>
.wf-rules {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
/* 哪一条哪一格不对，由外层那一行点名；这里只把整张表框出来。 */
.wf-rules.is-invalid {
  border-radius: 4px;
  box-shadow: 0 0 0 1px var(--aeg-color-status-danger);
}
.wf-rules__table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.wf-rules__th {
  padding: 2px 4px;
  color: var(--aeg-color-text-muted);
  font-weight: 500;
  text-align: left;
}
.wf-rules__index {
  color: var(--aeg-color-text-muted);
  font-family: var(--aeg-font-mono);
  font-size: 11px;
  text-align: right;
}
.wf-rules__cell {
  width: 100%;
  min-width: 0;
  padding: 2px 4px;
  border: 1px solid var(--aeg-color-border-strong);
  border-radius: 4px;
  background: var(--aeg-color-elevated);
  color: var(--aeg-color-text);
  font-family: var(--aeg-font-mono);
  font-size: 11px;
}
.wf-rules__remove {
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
.wf-rules__remove:hover:not(:disabled) {
  border-color: var(--aeg-color-status-danger);
  color: var(--aeg-color-status-danger);
}
.wf-rules__add {
  align-self: flex-start;
  padding: 1px 6px;
  border: 1px dashed var(--aeg-color-border-strong);
  border-radius: 999px;
  background: transparent;
  color: var(--aeg-color-text-secondary);
  font-size: 11px;
  cursor: pointer;
}
.wf-rules__add:hover:not(:disabled) {
  border-color: var(--aeg-color-brand);
  color: var(--aeg-color-brand-hover);
}
.wf-rules__empty {
  margin: 0;
  color: var(--aeg-color-text-muted);
  font-size: 11px;
  line-height: 1.6;
}
</style>
