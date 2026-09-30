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
}>()

const emit = defineEmits<{ 'update:modelValue': [value: JsonValue] }>()

function render(value: JsonValue | undefined): string {
  if (value === undefined || value === null) return ''
  return JSON.stringify(value, null, 2)
}

const draft = ref(render(props.modelValue))
const invalid = ref('')
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
    invalid.value = ''
    lastEmitted = 'null'
    emit('update:modelValue', null)
    return
  }
  try {
    const parsed: unknown = JSON.parse(text)
    invalid.value = ''
    lastEmitted = JSON.stringify(parsed)
    emit('update:modelValue', parsed as JsonValue)
  } catch {
    invalid.value = 'JSON 解析失败，未提交'
  }
}
</script>

<template>
  <label class="wf-json">
    <span class="wf-json__label">{{ label }}</span>
    <textarea
      class="wf-json__area"
      rows="4"
      :value="draft"
      :placeholder="placeholder"
      :disabled="disabled"
      @input="onInput"
    />
    <span v-if="invalid !== ''" class="wf-json__error">{{ invalid }}</span>
  </label>
</template>

<style scoped>
.wf-json {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.wf-json__label {
  color: #595959;
  font-size: 12px;
}
.wf-json__area {
  padding: 4px 6px;
  border: 1px solid #d9d9d9;
  border-radius: 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  resize: vertical;
}
.wf-json__error {
  color: #cf1322;
  font-size: 12px;
}
</style>
