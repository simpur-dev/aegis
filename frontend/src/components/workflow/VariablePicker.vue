<script setup lang="ts">
/**
 * 上游变量选择器：只列"这颗节点真的能引用到"的东西。
 *
 * 数据来自 `useUpstreamVariables`（真实祖先 + catalog 产出键 + 上次运行多出来的键），
 * 所以这里不需要再判一次合法性——列不出来就是不能引用。点了没连线的节点是画布上最难查的
 * 一类事故：发布当时不报错，跑到那一步才变成空值。
 */
import { computed, ref } from 'vue'

import type { UpstreamVariableGroup } from '@/composables/workflow/useUpstreamVariables'
import { variableTransformer } from '@/utils/graph/variable'

const props = defineProps<{ groups: readonly UpstreamVariableGroup[]; disabled: boolean }>()

const emit = defineEmits<{ pick: [reference: string] }>()

const keyword = ref('')

const filtered = computed(() => {
  const needle = keyword.value.trim().toLowerCase()
  if (needle === '') return props.groups.map((group) => ({ ...group, keys: [...group.keys] }))
  return props.groups
    .map((group) => ({
      nodeId: group.nodeId,
      nodeType: group.nodeType,
      keys: group.keys.filter((key) => `${group.nodeId}.${key}`.toLowerCase().includes(needle)),
    }))
    .filter((group) => group.keys.length > 0 || group.nodeId.toLowerCase().includes(needle))
})

function referenceOf(group: { nodeId: string }, key: string): string {
  return variableTransformer([group.nodeId, key])
}
</script>

<template>
  <div class="wf-varpicker" data-testid="variable-picker">
    <input
      v-model="keyword"
      class="wf-varpicker__search"
      type="text"
      placeholder="搜上游节点或输出键"
      aria-label="搜索上游变量"
      data-testid="variable-search"
    />
    <p v-if="groups.length === 0" class="wf-varpicker__empty">
      这颗节点还没有能引用的上游——先在画布上连一条指向它的进边。
    </p>
    <p v-else-if="filtered.length === 0" class="wf-varpicker__empty">没有匹配的上游变量。</p>
    <template v-else>
      <section v-for="group in filtered" :key="group.nodeId" class="wf-varpicker__group">
        <h5 class="wf-varpicker__group-title">
          <code>{{ group.nodeId }}</code>
          <span>{{ group.nodeType }}</span>
        </h5>
        <ul v-if="group.keys.length > 0" class="wf-varpicker__keys">
          <li v-for="key in group.keys" :key="key">
            <button
              type="button"
              class="wf-varpicker__key"
              :disabled="disabled"
              :title="`插入 ${referenceOf(group, key)}`"
              :data-testid="`var-${group.nodeId}-${key}`"
              @click="emit('pick', referenceOf(group, key))"
            >
              {{ key }}
            </button>
          </li>
        </ul>
        <p v-else class="wf-varpicker__empty">这一路没有声明产出键。</p>
      </section>
    </template>
  </div>
</template>

<style scoped>
.wf-varpicker {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 6px;
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-sm);
  background: var(--aeg-color-sunken);
}
.wf-varpicker__search {
  padding: 2px 6px;
  border: 1px solid var(--aeg-color-border-strong);
  border-radius: 4px;
  background: var(--aeg-color-elevated);
  color: var(--aeg-color-text);
  font-size: 12px;
}
.wf-varpicker__group {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.wf-varpicker__group-title {
  display: flex;
  gap: 6px;
  align-items: baseline;
  margin: 0;
  color: var(--aeg-color-text-secondary);
  font-size: 11px;
  font-weight: 500;
}
.wf-varpicker__group-title code {
  font-family: var(--aeg-font-mono);
  color: var(--aeg-color-text);
}
.wf-varpicker__keys {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.wf-varpicker__key {
  padding: 1px 6px;
  border: 1px solid var(--aeg-color-border);
  border-radius: 999px;
  background: var(--aeg-color-elevated);
  color: var(--aeg-color-text);
  font-family: var(--aeg-font-mono);
  font-size: 11px;
  cursor: pointer;
}
.wf-varpicker__key:hover:not(:disabled) {
  border-color: var(--aeg-color-brand);
  color: var(--aeg-color-brand-hover);
}
.wf-varpicker__key:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}
.wf-varpicker__empty {
  margin: 0;
  color: var(--aeg-color-text-muted);
  font-size: 11px;
  line-height: 1.6;
}
</style>
