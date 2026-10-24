<script setup lang="ts">
/**
 * 允许出现变量引用的文本格（通报内容、接口地址、核签提示语…）。
 *
 * 为什么单独一档：`{{#fetch_1.rows#}}` 这类串只有从真实上游里点出来才靠得住，
 * 手敲会把节点 ID 打错一个字母——发布照样通过，跑到那一步才变成空值。
 * 这一格把变量选择器焊在输入框旁边，并把引用插到光标处，而不是永远追加到末尾。
 */
import { computed, nextTick, ref } from 'vue'

import type { UpstreamVariableGroup } from '@/composables/workflow/useUpstreamVariables'
import { variableReferences } from './configValidation'

import VariablePicker from './VariablePicker.vue'

const props = defineProps<{
  label: string
  placeholder: string
  modelValue: unknown
  disabled: boolean
  variables: readonly UpstreamVariableGroup[]
  /** 这一格现在有问题：只负责把边框染红，那句话由外层统一说，不重复摆两遍。 */
  invalid?: boolean
}>()

const emit = defineEmits<{ 'update:modelValue': [value: string | null] }>()

const areaRef = ref<HTMLTextAreaElement | null>(null)
const open = ref(false)

const current = (): string => (typeof props.modelValue === 'string' ? props.modelValue : '')

/** 这一格里已经点了几处引用：让人知道自己写的不是死文本。 */
const referenceCount = computed(() => variableReferences({ v: props.modelValue }).length)

function onInput(event: Event): void {
  const raw = (event.target as HTMLTextAreaElement).value
  emit('update:modelValue', raw === '' ? null : raw)
}

/** 插到光标处：人往往是在句中补一个数，不是在末尾追加。 */
function insert(reference: string): void {
  const text = current()
  const element = areaRef.value
  const start = element?.selectionStart ?? text.length
  const end = element?.selectionEnd ?? start
  emit('update:modelValue', `${text.slice(0, start)}${reference}${text.slice(end)}`)
  open.value = false
  void nextTick(() => {
    const area = areaRef.value
    if (area === null) return
    const caret = start + reference.length
    area.focus()
    area.setSelectionRange(caret, caret)
  })
}

function toggle(): void {
  if (props.disabled) return
  open.value = !open.value
}
</script>

<template>
  <div class="wf-template">
    <textarea
      ref="areaRef"
      class="wf-template__area"
      :class="{ 'is-invalid': invalid }"
      rows="2"
      :value="current()"
      :placeholder="placeholder"
      :disabled="disabled"
      :aria-label="label"
      :aria-invalid="invalid"
      @input="onInput"
    />
    <div class="wf-template__bar">
      <button
        type="button"
        class="wf-template__pick"
        :disabled="disabled"
        :aria-expanded="open"
        data-testid="variable-toggle"
        @click="toggle"
      >
        插入变量
      </button>
      <span v-if="referenceCount > 0" class="wf-template__count"> 已引用 {{ referenceCount }} 处 </span>
    </div>
    <VariablePicker v-if="open" :groups="variables" :disabled="disabled" @pick="insert" />
  </div>
</template>

<style scoped>
.wf-template {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.wf-template__area {
  padding: 2px 6px;
  border: 1px solid var(--aeg-color-border-strong);
  border-radius: 4px;
  background: var(--aeg-color-elevated);
  color: var(--aeg-color-text);
  font-family: var(--aeg-font-mono);
  font-size: 12px;
  line-height: 1.6;
  resize: vertical;
}
.wf-template__area.is-invalid {
  border-color: var(--aeg-color-status-danger);
}
.wf-template__bar {
  display: flex;
  gap: 6px;
  align-items: center;
}
.wf-template__pick {
  padding: 1px 6px;
  border: 1px solid var(--aeg-color-border);
  border-radius: 999px;
  background: var(--aeg-color-elevated);
  color: var(--aeg-color-text-secondary);
  font-size: 11px;
  cursor: pointer;
}
.wf-template__pick:hover:not(:disabled) {
  border-color: var(--aeg-color-brand);
  color: var(--aeg-color-brand-hover);
}
.wf-template__pick:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}
.wf-template__count {
  color: var(--aeg-color-text-muted);
  font-family: var(--aeg-font-mono);
  font-size: 11px;
}
</style>
