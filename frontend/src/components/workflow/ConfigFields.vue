<script setup lang="ts">
/**
 * 节点参数编辑器：完全由 registry 的字段描述驱动，新增节点类型只改注册表。
 *
 * 这里刻意用原生表单控件而非 ant-design-vue：与 Vue Flow 节点卡片同一视觉语言，
 * 且密集排布的参数行用原生 input 在 jsdom 下可稳定断言（见 __tests__/components.spec.ts）。
 */
import { computed } from 'vue'

import { NODE_SPECS, type JsonValue, type NodeDef } from '@/utils/graph'

import JsonField from './JsonField.vue'
import { nodeMeta, type ConfigField } from './registry'

const props = defineProps<{ def: NodeDef; disabled: boolean }>()

const emit = defineEmits<{ 'update:config': [config: Record<string, JsonValue>] }>()

const fields = computed<readonly ConfigField[]>(() => nodeMeta(props.def.type)?.fields ?? [])

function required(key: string): boolean {
  return NODE_SPECS[props.def.type]?.required_config.includes(key) ?? false
}

/** null 语义为"删除该键"，与后端 validate_config 的缺失判定对齐（nodes.py:106）。 */
function write(key: string, value: JsonValue | null): void {
  const next: Record<string, JsonValue> = { ...props.def.config }
  if (value === null) delete next[key]
  else next[key] = value
  emit('update:config', next)
}

function textOf(key: string): string {
  const value = props.def.config[key]
  if (value === undefined || value === null) return ''
  return Array.isArray(value) ? value.map((item) => String(item)).join(', ') : String(value)
}

function numberOf(key: string): number | undefined {
  const value = props.def.config[key]
  return typeof value === 'number' ? value : undefined
}

function onText(field: ConfigField, event: Event): void {
  const raw = (event.target as HTMLInputElement).value
  if (field.kind !== 'string_list') {
    write(field.key, raw === '' ? null : raw)
    return
  }
  const items = raw
    .split(',')
    .map((item) => item.trim())
    .filter((item) => item !== '')
  write(field.key, items)
}

function onJson(field: ConfigField, value: JsonValue): void {
  write(field.key, value)
}

function onNumber(field: ConfigField, event: Event): void {
  const raw = (event.target as HTMLInputElement).value
  const parsed = Number(raw)
  write(field.key, raw === '' || Number.isNaN(parsed) ? null : parsed)
}
</script>

<template>
  <div class="wf-fields">
    <label v-for="field in fields" :key="field.key" class="wf-field">
      <span class="wf-field__label">
        {{ field.label }}
        <em v-if="required(field.key)" class="wf-field__required">必填</em>
      </span>
      <JsonField
        v-if="field.kind === 'json'"
        :label="field.label"
        :placeholder="field.placeholder"
        :model-value="def.config[field.key]"
        :disabled="disabled"
        @update:model-value="onJson(field, $event)"
      />
      <input
        v-else-if="field.kind === 'number'"
        class="wf-field__input"
        type="number"
        :value="numberOf(field.key)"
        :placeholder="field.placeholder"
        :disabled="disabled"
        @input="onNumber(field, $event)"
      />
      <input
        v-else
        class="wf-field__input"
        type="text"
        :value="textOf(field.key)"
        :placeholder="field.placeholder"
        :disabled="disabled"
        @input="onText(field, $event)"
      />
    </label>
    <p v-if="fields.length === 0" class="wf-fields__empty">该节点类型无参数，仅需配置 SLA 与失败策略。</p>
  </div>
</template>

<style scoped>
.wf-fields {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.wf-field {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.wf-field__label {
  display: flex;
  gap: 4px;
  color: #595959;
  font-size: 12px;
}
.wf-field__required {
  color: #cf1322;
  font-size: 11px;
  font-style: normal;
}
.wf-field__input {
  padding: 2px 6px;
  border: 1px solid #d9d9d9;
  border-radius: 4px;
  font-size: 12px;
}
.wf-fields__empty {
  margin: 0;
  color: #5a6072;
  font-size: 12px;
}
</style>
