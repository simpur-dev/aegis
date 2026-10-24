<script setup lang="ts">
/**
 * 节点参数编辑器：完全由 registry 的字段描述驱动，新增节点类型只改注册表。
 *
 * 每一格用哪种控件写在 `NODE_META.fields[].kind` 上：19 类节点共用这一处渲染，
 * 才不会长成"每类一套行为、改一处要验十九处"。取值域、必填、连线一致性这些判据
 * 也不在这里重写，统一来自 `configValidation`（与后端同源）。
 *
 * 这里刻意用原生表单控件而非 ant-design-vue：与 Vue Flow 节点卡片同一视觉语言，
 * 且密集排布的参数行用原生 input 在 jsdom 下可稳定断言（见 __tests__/components.spec.ts）。
 */
import { computed } from 'vue'

import type { UpstreamVariableGroup } from '@/composables/workflow/useUpstreamVariables'
import { NODE_SPECS, type JsonValue, type NodeDef } from '@/utils/graph'

import JsonField from './JsonField.vue'
import NodeRefPicker from './NodeRefPicker.vue'
import RuleEditor from './RuleEditor.vue'
import StartVariablesEditor from './StartVariablesEditor.vue'
import TemplateField from './TemplateField.vue'
import VariableListEditor from './VariableListEditor.vue'
import { issuesForField, nodeConfigIssues, type NodeIssue } from './configValidation'
import { nodeMeta, type ConfigField, type NodeRefCandidate } from './registry'

const props = defineProps<{
  def: NodeDef
  disabled: boolean
  /** 上游变量候选；不给就没有可引用的东西（画布上这颗节点还没有祖先）。 */
  variables?: readonly UpstreamVariableGroup[]
  /** 真实入边节点；`upstream` 那几格的候选与连线判定都读它。 */
  candidates?: readonly NodeRefCandidate[]
  /** 外层（检查器的参数体检）已经算过时传进来，避免同一颗节点判两遍。 */
  issues?: readonly NodeIssue[]
}>()

const emit = defineEmits<{ 'update:config': [config: Record<string, JsonValue>] }>()

const fields = computed<readonly ConfigField[]>(() => nodeMeta(props.def.type)?.fields ?? [])
const candidateIds = computed(() => (props.candidates ?? []).map((candidate) => candidate.id))
const resolvedIssues = computed(
  () => props.issues ?? nodeConfigIssues(props.def, { inbound: candidateIds.value }),
)
const variableGroups = computed(() => props.variables ?? [])
/**
 * 落不到任何一格上的问题：白名单外的键、形状不对的引用。
 *
 * 这类键根本不会出现在字段清单里（正因为如此才要报），所以只能在末尾另起一段说——
 * 憋回整图错误清单里，人只会看到"保存失败"四个字，不知道自己哪一行多写了什么。
 */
const strayIssues = computed(() => {
  const keys = new Set(fields.value.map((field) => field.key))
  return resolvedIssues.value.filter((issue) => !keys.has(issue.key))
})

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

function errorOf(key: string): string {
  return issuesForField(resolvedIssues.value, key)
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
  write(field.key, raw === '' ? null : raw)
}

function onSelect(field: ConfigField, event: Event): void {
  const raw = (event.target as HTMLSelectElement).value
  write(field.key, raw === '' ? null : raw)
}

function onNumber(field: ConfigField, event: Event): void {
  const raw = (event.target as HTMLInputElement).value
  const parsed = Number(raw)
  write(field.key, raw === '' || Number.isNaN(parsed) ? null : parsed)
}

function onJson(field: ConfigField, value: JsonValue): void {
  write(field.key, value)
}
</script>

<template>
  <div class="wf-fields">
    <div v-for="field in fields" :key="field.key" class="wf-field" :data-testid="`field-${field.key}`">
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
        :invalid="errorOf(field.key) !== ''"
        @update:model-value="onJson(field, $event)"
      />
      <RuleEditor
        v-else-if="field.kind === 'rules' || field.kind === 'conditions'"
        :kind="field.kind === 'rules' ? 'rules' : 'conditions'"
        :model-value="def.config[field.key]"
        :disabled="disabled"
        :invalid="errorOf(field.key) !== ''"
        @update:model-value="write(field.key, $event)"
      />
      <StartVariablesEditor
        v-else-if="field.kind === 'start_variables'"
        :model-value="def.config[field.key]"
        :disabled="disabled"
        :invalid="errorOf(field.key) !== ''"
        @update:model-value="write(field.key, $event)"
      />
      <NodeRefPicker
        v-else-if="field.kind === 'node_ref' || field.kind === 'node_ref_list'"
        :multiple="field.kind === 'node_ref_list'"
        :model-value="def.config[field.key]"
        :candidates="candidates ?? []"
        :disabled="disabled"
        :invalid="errorOf(field.key) !== ''"
        @update:model-value="write(field.key, $event)"
      />
      <VariableListEditor
        v-else-if="field.kind === 'string_list'"
        :placeholder="field.placeholder"
        :model-value="def.config[field.key]"
        :disabled="disabled"
        :invalid="errorOf(field.key) !== ''"
        @update:model-value="write(field.key, $event)"
      />
      <TemplateField
        v-else-if="field.kind === 'template'"
        :label="field.label"
        :placeholder="field.placeholder"
        :model-value="def.config[field.key]"
        :disabled="disabled"
        :variables="variableGroups"
        :invalid="errorOf(field.key) !== ''"
        @update:model-value="write(field.key, $event)"
      />
      <template v-else>
        <select
          v-if="field.kind === 'select'"
          class="wf-field__input"
          :value="textOf(field.key)"
          :disabled="disabled"
          :aria-label="field.label"
          :class="{ 'is-invalid': errorOf(field.key) !== '' }"
          @change="onSelect(field, $event)"
        >
          <option value="">（默认）</option>
          <option v-for="option in field.options ?? []" :key="option.value" :value="option.value">
            {{ option.label }}
          </option>
        </select>
        <input
          v-else-if="field.kind === 'number'"
          class="wf-field__input"
          :class="{ 'is-invalid': errorOf(field.key) !== '' }"
          type="number"
          :min="field.min"
          :max="field.max"
          :value="numberOf(field.key)"
          :placeholder="field.placeholder"
          :disabled="disabled"
          :aria-label="field.label"
          @input="onNumber(field, $event)"
        />
        <input
          v-else
          class="wf-field__input"
          :class="{ 'is-invalid': errorOf(field.key) !== '' }"
          type="text"
          :value="textOf(field.key)"
          :placeholder="field.placeholder"
          :disabled="disabled"
          :aria-label="field.label"
          @input="onText(field, $event)"
        />
      </template>

      <span v-if="errorOf(field.key) !== ''" class="wf-field__error" role="alert" :data-testid="`error-${field.key}`">
        {{ errorOf(field.key) }}
      </span>
      <span v-else-if="field.hint" class="wf-field__hint">{{ field.hint }}</span>
    </div>
    <ul v-if="strayIssues.length > 0" class="wf-fields__strays" role="alert" data-testid="stray-issues">
      <li v-for="(issue, index) in strayIssues" :key="`${issue.key}-${index}`" class="wf-field__error">
        {{ issue.message }}
      </li>
    </ul>
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
  color: var(--aeg-color-text-secondary);
  font-size: 12px;
}
.wf-field__required {
  color: var(--aeg-color-status-danger);
  font-size: 11px;
  font-style: normal;
}
.wf-field__input {
  padding: 2px 6px;
  border: 1px solid var(--aeg-color-border-strong);
  border-radius: 4px;
  background: var(--aeg-color-elevated);
  color: var(--aeg-color-text);
  font-size: 12px;
}
.wf-field__input.is-invalid {
  border-color: var(--aeg-color-status-danger);
}
.wf-field__error {
  color: var(--aeg-color-status-danger);
  font-size: 12px;
  line-height: 1.5;
}
.wf-field__hint {
  color: var(--aeg-color-text-muted);
  font-size: 11px;
  line-height: 1.5;
}
.wf-fields__empty {
  margin: 0;
  color: var(--aeg-color-text-secondary);
  font-size: 12px;
}
.wf-fields__strays {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin: 0;
  padding-left: 16px;
}
</style>
