<script setup lang="ts">
/**
 * 智能助手页：只负责页头（能力面状态 + 重读），会话本身在 `AssistantPanel`。
 *
 * 页面与全站抽屉是同一个会话的两种容器——把会话逻辑留在页面里，抽屉就只能做成空壳
 * （那正是 2026-11 之前的状态）。
 */
import { onMounted, ref } from 'vue'
import { ReloadOutlined } from '@ant-design/icons-vue'

import PageHero from '@/components/PageHero.vue'
import AssistantPanel from '@/components/assistant/AssistantPanel.vue'
import { useAssistantCapabilities } from '@/composables/useAssistantCapabilities'

const { capabilities, error: capabilitiesError, loading: capabilitiesLoading, llmConfigured, load } = useAssistantCapabilities()
const panel = ref<InstanceType<typeof AssistantPanel> | null>(null)
function showPendingAlerts(): Promise<void> | undefined {
  return panel.value?.showPendingAlerts()
}
defineExpose({ showPendingAlerts })

onMounted(() => void load())
</script>

<template>
  <div class="assistant">
    <PageHero
      class="assistant-heading workspace-surface"
      title="智能助手"
      caption="查询预警与处置记录。需要确认的操作会先显示提案和影响范围。"
      hide-ridge
    >
      <template #heading-icon>
        <span class="assistant-heading__icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" width="23" height="23" fill="currentColor" focusable="false">
            <path d="M12 1.5C13.1 7.9 16.1 10.9 22.5 12c-6.4 1.1-9.4 4.1-10.5 10.5C10.9 16.1 7.9 13.1 1.5 12 7.9 10.9 10.9 7.9 12 1.5Z" />
          </svg>
        </span>
      </template>
      <template #actions>
        <a-tag v-if="capabilitiesError" color="red" data-testid="caps-state">读不到能力面</a-tag>
        <a-tag v-else-if="capabilities && !llmConfigured" color="blue" data-testid="caps-state" title="语义模型未配置，可用内置规则查询">规则问答</a-tag>
        <a-tag v-else-if="capabilities" color="green" data-testid="caps-state">会话服务就绪</a-tag>
        <a-button size="small" :loading="capabilitiesLoading" data-testid="caps-refresh" @click="load">
          <template #icon><ReloadOutlined aria-hidden="true" /></template>
          刷新能力
        </a-button>
      </template>
    </PageHero>

    <AssistantPanel ref="panel" mode="page" />
  </div>
</template>

<style scoped>
/*
 * 整页定高：主内容在值班台路由下是竖排 flex（App.vue 的 is-desk），这里 flex:1 吃掉
 * 令牌条与页脚之外的全部高度，会话板块才能锁满余下空间、整页不滚动。
 * 页头占自然高度，余下的全给 AssistantPanel（它自己 flex:1）。
 */
.assistant {
  position: relative;
  isolation: isolate;
  container-type: inline-size;
  display: flex;
  flex-direction: column;
  gap: var(--aeg-space-s3);
  flex: 1;
  /* min-height:auto = 内容最小高度：值班台比一屏高时整页滚动，而不是把会话压回去 */
  min-height: auto;
}
.assistant-heading {
  display: grid;
  align-content: center;
  flex: none;
  min-height: 94px;
  padding: var(--aeg-space-s3) var(--aeg-space-s4);
  margin-bottom: 0;
  --data-highlight-position: 29%;
}
.assistant-heading__icon {
  display: grid;
  place-items: center;
  inline-size: 40px;
  block-size: 40px;
  border: 1px solid var(--aeg-color-brand-line);
  border-radius: var(--aeg-radius-md);
  background: var(--aeg-color-brand-soft);
  color: var(--aeg-color-overview-accent);
}
.assistant-heading :deep(.page-head__row) {
  gap: var(--aeg-space-s2);
}
.assistant-heading :deep(.page-head__caption) {
  margin-top: var(--aeg-space-s1);
}
html[data-theme='dark'] .assistant-heading :deep(.ant-btn-default) {
  border-color: color-mix(in srgb, var(--aeg-color-overview-accent) 52%, var(--aeg-color-border));
  background: linear-gradient(180deg, var(--aeg-color-command-panel), var(--aeg-color-command-deep));
}
html[data-theme='dark'] .assistant-heading :deep(.ant-btn-default:hover) {
  border-color: var(--aeg-color-overview-accent);
}
@container (min-width: 660px) {
  .assistant-heading {
    padding-inline-start: 76px;
  }
  .assistant-heading :deep(.page-head__title) {
    font-size: clamp(var(--aeg-font-xl), 2cqi, 30px);
  }
  .assistant-heading__icon,
  .assistant-heading :deep(.page-head__actions) {
    position: absolute;
    inset-block-start: 50%;
    transform: translateY(-50%);
  }
  .assistant-heading__icon { inset-inline-start: 20px; }
  .assistant-heading :deep(.page-head__actions) { inset-inline-end: 20px; }
  .assistant-heading :deep(.page-head__caption) {
    max-width: calc(100% - 190px);
  }
}
@container (min-width: 900px) {
  .assistant-heading::after {
    position: absolute;
    z-index: -1;
    inset-block: 0;
    inset-inline: max(480px, 32%) max(172px, 16%);
    background: url('/backgrounds/assistant-heading-radar.webp') center / 100% min(87%, 7cqi, 112px) no-repeat;
    opacity: .74;
    filter: saturate(1.16) brightness(.72);
    pointer-events: none;
    content: '';
  }
  html[data-theme='dark'] .assistant-heading::after {
    opacity: .94;
    filter: none;
  }
}
@media (max-width: 720px) {
  .assistant-heading :deep(.page-head__actions) {
    gap: var(--aeg-space-s1);
    flex: 0 0 auto;
    margin-left: auto;
  }
  .assistant-heading :deep(.page-head__title) {
    font-size: var(--aeg-font-lg);
  }
  .assistant-heading__icon {
    inline-size: 32px;
    block-size: 32px;
  }
}
</style>
