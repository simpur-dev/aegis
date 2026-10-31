<script setup lang="ts">
/**
 * 运行输入：Start 变量表单为主，旧定义仍可退回贴一段 JSON。
 *
 * 两档语义必须写在标题上：试运行跑的是**当前画布草稿**（含未保存改动）且不产生外部副作用；
 * 正式启动跑的是**已发布版本**，会写实例账本、通知与设备控制照常执行。
 */
import { computed, ref, watch } from 'vue'

import type { JsonValue } from '@/utils/graph'
import type { StartVariable, StartVariableType } from '@/utils/graph/document'

import { useEditorContext } from './editor/context'

const props = defineProps<{ mode: 'preview' | 'start'; busy: boolean }>()
const emit = defineEmits<{ close: []; submit: [payload: Record<string, JsonValue>] }>()

const { document: documentStore } = useEditorContext()

const tab = ref<'form' | 'json'>('form')
const jsonText = ref('{}')
const error = ref<string | null>(null)
const inputs = ref<Record<string, string>>({})

const START_TYPES: readonly StartVariableType[] = ['string', 'number', 'boolean', 'json']

function variablesOf(): StartVariable[] {
  const def = documentStore.definition
  if (def === null) return []
  const out: StartVariable[] = []
  const seen = new Set<string>()
  for (const node of def.nodes) {
    if (node.type !== 'start') continue
    const raw = node.config.variables
    if (!Array.isArray(raw)) continue
    for (const item of raw) {
      if (item === null || typeof item !== 'object' || Array.isArray(item)) continue
      const name = typeof item.name === 'string' ? item.name : ''
      // 名字不合法的行发布时会被拦下；运行输入这里直接不收，免得表单出现一个永远对不上的键。
      if (!/^[A-Za-z_][A-Za-z0-9_]{0,31}$/.test(name) || seen.has(name)) continue
      seen.add(name)
      const type = START_TYPES.includes(item.type as StartVariableType)
        ? (item.type as StartVariableType)
        : 'string'
      out.push({
        name,
        label: typeof item.label === 'string' && item.label !== '' ? item.label : name,
        type,
        required: item.required === true,
        default: item.default === undefined ? null : (item.default as JsonValue),
      })
    }
  }
  return out
}

const variables = computed(variablesOf)

function textOf(value: JsonValue | undefined): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'string') return value
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function resetInputs(): void {
  inputs.value = Object.fromEntries(variables.value.map((item) => [item.name, textOf(item.default)]))
  error.value = null
  tab.value = 'form'
}

watch(variables, resetInputs, { immediate: true })

const title = computed(() =>
  props.mode === 'preview' ? '试运行（不产生外部副作用）' : '正式启动实例（会写实例并产生真实副作用）',
)

const scopeNote = computed(() =>
  props.mode === 'preview'
    ? '跑的是当前画布草稿（含未保存改动）；不写定义、不写草稿、不进实例账本，通知/外呼/设备控制/预警发布只给回执。'
    : `跑的是已发布版本 v${documentStore.publishedVersion ?? '—'}；画布上未发布的改动不参与本次执行。`,
)

type Coerced = { ok: true; value: JsonValue } | { ok: true; skip: true } | { ok: false; reason: string }

function coerce(item: StartVariable, raw: string): Coerced {
  const text = raw.trim()
  if (text === '') {
    if (item.default !== null && item.default !== undefined) return { ok: true, value: item.default }
    if (item.required) return { ok: false, reason: `变量 ${item.name} 必填` }
    return { ok: true, skip: true }
  }
  if (item.type === 'number') {
    const value = Number(text)
    return Number.isFinite(value) ? { ok: true, value } : { ok: false, reason: `变量 ${item.name} 应为数字` }
  }
  if (item.type === 'boolean') {
    if (text === 'true') return { ok: true, value: true }
    if (text === 'false') return { ok: true, value: false }
    return { ok: false, reason: `变量 ${item.name} 应为 true 或 false` }
  }
  if (item.type === 'json') {
    try {
      return { ok: true, value: JSON.parse(text) as JsonValue }
    } catch {
      return { ok: false, reason: `变量 ${item.name} 不是合法 JSON` }
    }
  }
  return { ok: true, value: raw }
}

function payloadFromForm(): Record<string, JsonValue> | null {
  const payload: Record<string, JsonValue> = {}
  for (const item of variables.value) {
    const result = coerce(item, inputs.value[item.name] ?? '')
    if (!result.ok) {
      error.value = result.reason
      return null
    }
    if ('skip' in result) continue
    payload[item.name] = result.value
  }
  return payload
}

function payloadFromJson(): Record<string, JsonValue> | null {
  let parsed: unknown
  try {
    parsed = JSON.parse(jsonText.value)
  } catch {
    error.value = 'JSON 不是合法格式'
    return null
  }
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    error.value = 'JSON 必须是对象，例如 {"alert": "沟道泥位抬升"}'
    return null
  }
  return parsed as Record<string, JsonValue>
}

function submit(): void {
  error.value = null
  const payload = tab.value === 'json' ? payloadFromJson() : payloadFromForm()
  if (payload !== null) emit('submit', payload)
}
</script>

<template>
  <div class="wf-runinput" role="presentation" @click.self="emit('close')">
    <section
      class="wf-runinput__card"
      role="dialog"
      aria-modal="true"
      aria-labelledby="wf-runinput-title"
      data-testid="run-input-dialog"
      @keydown.esc="emit('close')"
    >
      <header class="wf-runinput__head">
        <h3 id="wf-runinput-title" class="wf-runinput__title">{{ title }}</h3>
        <button type="button" class="wf-runinput__close" aria-label="关闭" @click="emit('close')">×</button>
      </header>
      <p class="wf-runinput__scope" :class="mode === 'preview' ? 'is-preview' : 'is-formal'">{{ scopeNote }}</p>

      <div class="wf-runinput__tabs" role="tablist" aria-label="输入方式">
        <button
          type="button"
          role="tab"
          class="wf-runinput__tab"
          :class="{ 'is-active': tab === 'form' }"
          :aria-selected="tab === 'form'"
          data-testid="input-tab-form"
          @click="tab = 'form'"
        >
          Start 变量
        </button>
        <button
          type="button"
          role="tab"
          class="wf-runinput__tab"
          :class="{ 'is-active': tab === 'json' }"
          :aria-selected="tab === 'json'"
          data-testid="input-tab-json"
          @click="tab = 'json'"
        >
          旧定义 JSON
        </button>
      </div>

      <div v-if="tab === 'form'" class="wf-runinput__body">
        <p v-if="variables.length === 0" class="wf-runinput__empty">
          这份定义没有 Start 节点或没有声明输入变量；直接运行即可，或改用「旧定义 JSON」传任意载荷。
        </p>
        <label v-for="item in variables" :key="item.name" class="wf-runinput__field">
          <span class="wf-runinput__label">
            {{ item.label }}
            <code>{{ item.name }}</code>
            <em>{{ item.type }}{{ item.required ? ' · 必填' : '' }}</em>
          </span>
          <input
            v-model="inputs[item.name]"
            class="wf-runinput__input"
            type="text"
            :placeholder="item.type === 'boolean' ? 'true / false' : item.type === 'json' ? 'JSON 值' : '取值'"
            :data-testid="`run-input-${item.name}`"
          />
        </label>
      </div>

      <div v-else class="wf-runinput__body">
        <label class="wf-runinput__label" for="wf-runinput-json">运行载荷（JSON 对象）</label>
        <textarea
          id="wf-runinput-json"
          v-model="jsonText"
          class="wf-runinput__json"
          rows="8"
          spellcheck="false"
          data-testid="run-input-json"
        />
      </div>

      <p v-if="error !== null" class="wf-runinput__error" data-testid="run-input-error">{{ error }}</p>

      <footer class="wf-runinput__foot">
        <button type="button" class="wf-runinput__button" @click="emit('close')">取消</button>
        <button
          type="button"
          class="wf-runinput__button"
          :class="mode === 'preview' ? 'is-primary' : 'is-danger'"
          :disabled="busy"
          data-testid="run-input-submit"
          @click="submit"
        >
          {{ mode === 'preview' ? '开始试运行' : '正式启动' }}
        </button>
      </footer>
    </section>
  </div>
</template>

<style scoped>
.wf-runinput {
  position: fixed;
  inset: 0;
  z-index: 60;
  display: grid;
  place-items: center;
  background: var(--aeg-color-overlay-scrim);
}
.wf-runinput__card {
  display: flex;
  flex-direction: column;
  gap: 10px;
  width: min(520px, calc(100vw - 32px));
  max-height: calc(100vh - 64px);
  padding: 16px;
  overflow-y: auto;
  border: 1px solid var(--aeg-color-border);
  border-radius: 12px;
  background: var(--aeg-color-elevated);
  box-shadow: var(--aeg-shadow-lg);
}
.wf-runinput__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.wf-runinput__title {
  margin: 0;
  font-size: 14px;
  font-weight: 700;
}
.wf-runinput__close {
  width: 28px;
  height: 28px;
  border: none;
  border-radius: 7px;
  background: transparent;
  color: var(--aeg-color-text-muted);
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
}
.wf-runinput__close:hover {
  background: var(--aeg-color-sunken);
  color: var(--aeg-color-text);
}
.wf-runinput__scope {
  margin: 0;
  padding: 6px 8px;
  border-radius: 8px;
  font-size: 11px;
  line-height: 1.6;
}
.wf-runinput__scope.is-preview {
  background: var(--aeg-color-brand-soft);
  color: var(--aeg-color-brand-hover);
}
.wf-runinput__scope.is-formal {
  background: var(--aeg-tag-orange-bg);
  color: var(--aeg-tag-orange-text);
}
.wf-runinput__tabs {
  display: flex;
  gap: 2px;
  border-bottom: 1px solid var(--aeg-color-border-subtle);
}
.wf-runinput__tab {
  height: 30px;
  padding: 0 10px;
  border: none;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: var(--aeg-color-text-muted);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}
.wf-runinput__tab.is-active {
  border-bottom-color: var(--aeg-color-brand);
  color: var(--aeg-color-brand);
}
.wf-runinput__body {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.wf-runinput__empty {
  margin: 0;
  color: var(--aeg-color-text-secondary);
  font-size: 12px;
  line-height: 1.7;
}
.wf-runinput__field {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.wf-runinput__label {
  display: flex;
  gap: 6px;
  align-items: baseline;
  color: var(--aeg-color-text-secondary);
  font-size: 12px;
}
.wf-runinput__label code {
  font-family: var(--aeg-font-mono);
  font-size: 11px;
}
.wf-runinput__label em {
  color: var(--aeg-color-text-muted);
  font-size: 10px;
  font-style: normal;
}
.wf-runinput__input,
.wf-runinput__json {
  padding: 5px 8px;
  border: 1px solid var(--aeg-color-border-strong);
  border-radius: 6px;
  background: var(--aeg-color-elevated);
  color: var(--aeg-color-text);
  font: inherit;
  font-size: 12px;
}
.wf-runinput__json {
  font-family: var(--aeg-font-mono);
  resize: vertical;
}
.wf-runinput__error {
  margin: 0;
  color: var(--aeg-color-status-danger);
  font-size: 12px;
}
.wf-runinput__foot {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
.wf-runinput__button {
  height: 30px;
  padding: 0 12px;
  border: 1px solid var(--aeg-color-border-strong);
  border-radius: 7px;
  background: var(--aeg-color-elevated);
  color: var(--aeg-color-text);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}
.wf-runinput__button.is-primary {
  border-color: var(--aeg-color-brand);
  background: var(--aeg-color-brand);
  color: var(--aeg-color-text-inverse);
}
.wf-runinput__button.is-danger {
  border-color: var(--aeg-color-status-danger);
  background: var(--aeg-color-status-danger);
  color: var(--aeg-color-text-inverse);
}
.wf-runinput__button:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
</style>
