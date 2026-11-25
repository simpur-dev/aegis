<script setup lang="ts">
/**
 * 智能助手页：只负责页头（能力面状态 + 重读），会话本身在 `AssistantPanel`。
 *
 * 页面与全站抽屉是同一个会话的两种容器——把会话逻辑留在页面里，抽屉就只能做成空壳
 * （那正是 2026-11 之前的状态）。
 */
import { onMounted } from 'vue'
import { ReloadOutlined } from '@ant-design/icons-vue'

import PageHero from '@/components/PageHero.vue'
import AssistantPanel from '@/components/assistant/AssistantPanel.vue'
import { useAssistantCapabilities } from '@/composables/useAssistantCapabilities'

const { capabilities, error: capabilitiesError, loading: capabilitiesLoading, llmConfigured, load } = useAssistantCapabilities()

onMounted(() => void load())
</script>

<template>
  <div class="assistant">
    <PageHero title="智能助手" caption="查询预警与处置记录。需要确认的操作会先显示提案和影响范围。">
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

    <AssistantPanel mode="page" />
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
  display: flex;
  flex-direction: column;
  gap: var(--aeg-space-s3);
  flex: 1;
  /* min-height:auto = 内容最小高度：值班台比一屏高时整页滚动，而不是把会话压回去 */
  min-height: auto;
}
.assistant::before {
  content: '';
  position: absolute;
  z-index: -1;
  inset: 0 0 auto 24%;
  height: min(75vh, 580px);
  background: url('/backgrounds/tibet-relief.webp') 58% 35% / cover no-repeat;
  opacity: 0.16;
  mask-image: radial-gradient(ellipse at 72% 36%, black 10%, transparent 73%);
  pointer-events: none;
}
.assistant :deep(.page-head) {
  margin-bottom: 0;
}
.assistant :deep(.page-head__row) {
  flex-wrap: wrap;
}
.assistant :deep(.page-head__title) {
  font-size: var(--aeg-font-xl);
  font-weight: var(--aeg-weight-bold);
}
@media (max-width: 720px) {
  .assistant :deep(.page-head__actions) {
    gap: var(--aeg-space-s1);
    flex: 0 0 auto;
    margin-left: auto;
  }
  .assistant :deep(.page-head__title) {
    font-size: var(--aeg-font-lg);
  }
  .assistant :deep(.page-head__caption) {
    margin-top: var(--aeg-space-s1);
  }
}
</style>
