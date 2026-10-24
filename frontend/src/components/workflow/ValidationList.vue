<script setup lang="ts">
/**
 * 参数体检清单：把"这一颗节点现在存不出去"的原因摊在字段上方。
 *
 * 为什么整节点还要再来一份、每格下面不是已经写了：一处问题常常牵动好几格
 * （比如删了一条连线，upstream 与变量引用同时失效）。逐格的红字要人挨个找，
 * 这一份是"一共有几处、分别是什么"的账，改完一处就能看见少了一条。
 */
import type { NodeIssue } from './configValidation'

defineProps<{ issues: readonly NodeIssue[] }>()
</script>

<template>
  <section
    class="wf-issues"
    :class="{ 'is-clean': issues.length === 0 }"
    :data-testid="issues.length > 0 ? 'node-issues' : 'node-issues-clean'"
  >
    <p class="wf-issues__summary" :role="issues.length > 0 ? 'alert' : 'status'">
      <template v-if="issues.length > 0">参数体检：{{ issues.length }} 处会被发布拦下</template>
      <template v-else>参数体检：这一颗节点的参数齐全</template>
    </p>
    <ul v-if="issues.length > 0" class="wf-issues__list">
      <li v-for="(issue, index) in issues" :key="`${issue.key}-${index}`" class="wf-issues__item">
        {{ issue.message }}
      </li>
    </ul>
  </section>
</template>

<style scoped>
.wf-issues {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 6px 8px;
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-sm);
  background: var(--aeg-color-sunken);
}
.wf-issues.is-clean {
  border-color: var(--aeg-color-border-subtle);
}
.wf-issues__summary {
  margin: 0;
  color: var(--aeg-color-text-secondary);
  font-size: 12px;
  font-weight: 600;
}
.wf-issues.is-clean .wf-issues__summary {
  color: var(--aeg-color-status-success);
  font-weight: 400;
}
.wf-issues__list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin: 0;
  padding-left: 16px;
}
.wf-issues__item {
  color: var(--aeg-color-status-danger);
  font-size: 12px;
  line-height: 1.5;
}
</style>
