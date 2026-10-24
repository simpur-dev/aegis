<script setup lang="ts">
/**
 * JSON 型配置项（阈值条件、分支规则、请求体等）。
 * 草稿文本独立持有：用户输入非法 JSON 时只在本地提示、不写回模型，
 * 否则一次误输入会让画布校验区持续报错；外部改动（例如"按连线回填"）再同步回草稿。
 */
import { ref, watch } from 'vue'

import type { JsonValue } from '@/utils/graph'

const props = defineProps<{
  label: string
  placeholder: string
  modelValue: JsonValue | undefined
  disabled: boolean
  /** 这格的内容不合规（白名单外的结构、类型不对…）：只染红边框，话由外层说。 */
  invalid?: boolean
}>()

const emit = defineEmits<{ 'update:modelValue': [value: JsonValue] }>()

function render(value: JsonValue | undefined): string {
  if (value === undefined || value === null) return ''
  return JSON.stringify(value, null, 2)
}

const draft = ref(render(props.modelValue))
/** 本地解析状态：与"内容不合规"（`invalid`）分开——前者是没写完，后者是写完了但不对。 */
const parseError = ref('')
/** 记录最后一次由本组件写出的值，用来区分"外部改写"与"自己刚写回的回声"。 */
let lastEmitted: string = JSON.stringify(props.modelValue ?? null)

watch(
  () => props.modelValue,
  (value) => {
    const incoming = JSON.stringify(value ?? null)
    if (incoming !== lastEmitted) draft.value = render(value)
  },
)

function onInput(event: Event): void {
  const text = (event.target as HTMLTextAreaElement).value
  draft.value = text
  if (text.trim() === '') {
    parseError.value = ''
    lastEmitted = 'null'
    emit('update:modelValue', null)
    return
  }
  try {
    const parsed: unknown = JSON.parse(text)
    parseError.value = ''
    lastEmitted = JSON.stringify(parsed)
    emit('update:modelValue', parsed as JsonValue)
  } catch {
    parseError.value = 'JSON 解析失败，未提交'
  }
}
</script>

<template>
  <label class="wf-json">
    <span class="wf-json__label">{{ label }}</span>
    <textarea
      class="wf-json__area"
      :class="{ 'is-invalid': parseError !== '' || invalid === true }"
      rows="4"
      :value="draft"
      :placeholder="placeholder"
      :disabled="disabled"
      :aria-invalid="parseError !== '' || invalid === true"
      @input="onInput"
    />
    <span v-if="parseError !== ''" class="wf-json__error">{{ parseError }}</span>
  </label>
</template>

<style scoped>
.wf-json {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.wf-json__label {
  color: var(--aeg-color-text-secondary);
  font-size: 12px;
}
.wf-json__area {
  padding: 4px 6px;
  border: 1px solid var(--aeg-color-border-strong);
  border-radius: 4px;
  background: var(--aeg-color-elevated);
  color: var(--aeg-color-text);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  resize: vertical;
}
.wf-json__area.is-invalid {
  border-color: var(--aeg-color-status-danger);
}
.wf-json__error {
  color: var(--aeg-color-status-danger);
  font-size: 12px;
}
</style>
