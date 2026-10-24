<script setup lang="ts">
/**
 * 字符串列表格（发布通道、候选决策值…）。
 *
 * 原来是"逗号分隔的一整行"：填 `sms, broadcast` 与 `sms,broadcast` 等价，但空格、中文逗号
 * 会被切成看不见的空项，而运行期只认列表里的原值——值班员在画布上看不到那一项其实是空的。
 * 一行一项之后，加、删、改都落在看得见的那一格上。
 */
import { computed, ref } from 'vue'

const props = defineProps<{
  placeholder: string
  modelValue: unknown
  disabled: boolean
  /** 整列有问题（含空项）：染红外框，那句话由外层统一说。 */
  invalid?: boolean
}>()

const emit = defineEmits<{ 'update:modelValue': [value: string[] | null] }>()

const items = computed<string[]>(() =>
  Array.isArray(props.modelValue) ? props.modelValue.map((item) => (typeof item === 'string' ? item : String(item))) : [],
)

/** 新行的草稿：点"新增"先把行摆出来，人写完才进模型，免得留一串空项。 */
const draft = ref('')
const adding = ref(false)

function write(next: string[]): void {
  emit('update:modelValue', next.length > 0 ? next : null)
}

function onEdit(index: number, event: Event): void {
  const raw = (event.target as HTMLInputElement).value
  const next = [...items.value]
  next[index] = raw
  write(next)
}

function remove(index: number): void {
  write(items.value.filter((_, position) => position !== index))
}

function startAdd(): void {
  adding.value = true
  draft.value = ''
}

function commitAdd(): void {
  const value = draft.value.trim()
  if (value !== '') write([...items.value, value])
  adding.value = false
  draft.value = ''
}
</script>

<template>
  <div class="wf-list" :class="{ 'is-invalid': invalid }">
    <ul v-if="items.length > 0 || adding" class="wf-list__rows">
      <li v-for="(item, index) in items" :key="index" class="wf-list__row" :class="{ 'is-blank': item.trim() === '' }">
        <input
          class="wf-list__input"
          type="text"
          :value="item"
          :placeholder="placeholder"
          :disabled="disabled"
          :data-testid="`list-item-${index}`"
          @input="onEdit(index, $event)"
        />
        <button
          type="button"
          class="wf-list__remove"
          :disabled="disabled"
          aria-label="删除这一项"
          :data-testid="`list-remove-${index}`"
          @click="remove(index)"
        >
          ×
        </button>
      </li>
      <li v-if="adding" class="wf-list__row">
        <input
          v-model="draft"
          class="wf-list__input"
          type="text"
          placeholder="新项，回车确认"
          data-testid="list-draft"
          @keyup.enter="commitAdd"
          @blur="commitAdd"
        />
      </li>
    </ul>
    <p v-else class="wf-list__empty">一项都没填。</p>
    <button type="button" class="wf-list__add" :disabled="disabled || adding" data-testid="list-add" @click="startAdd">
      新增一项
    </button>
  </div>
</template>

<style scoped>
.wf-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
/* 整列有问题时给一个外圈：具体是哪一项空着，由那一行自己的红框说。 */
.wf-list.is-invalid {
  border-radius: 4px;
  box-shadow: 0 0 0 1px var(--aeg-color-status-danger);
}
.wf-list__rows {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.wf-list__row {
  display: flex;
  gap: 4px;
  align-items: center;
}
.wf-list__input {
  flex: 1;
  min-width: 0;
  padding: 2px 6px;
  border: 1px solid var(--aeg-color-border-strong);
  border-radius: 4px;
  background: var(--aeg-color-elevated);
  color: var(--aeg-color-text);
  font-family: var(--aeg-font-mono);
  font-size: 12px;
}
.wf-list__row.is-blank .wf-list__input {
  border-color: var(--aeg-color-status-danger);
}
.wf-list__remove {
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
.wf-list__remove:hover:not(:disabled) {
  border-color: var(--aeg-color-status-danger);
  color: var(--aeg-color-status-danger);
}
.wf-list__add {
  align-self: flex-start;
  padding: 1px 6px;
  border: 1px dashed var(--aeg-color-border-strong);
  border-radius: 999px;
  background: transparent;
  color: var(--aeg-color-text-secondary);
  font-size: 11px;
  cursor: pointer;
}
.wf-list__add:hover:not(:disabled) {
  border-color: var(--aeg-color-brand);
  color: var(--aeg-color-brand-hover);
}
.wf-list__add:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}
.wf-list__empty {
  margin: 0;
  color: var(--aeg-color-text-muted);
  font-size: 11px;
}
</style>
