<script setup lang="ts">
/**
 * 智能助手页：只负责页头（能力面状态 + 重读），会话本身在 `AssistantPanel`。
 *
 * 页面与全站抽屉是同一个会话的两种容器——把会话逻辑留在页面里，抽屉就只能做成空壳
 * （那正是 2026-11 之前的状态）。
 */
import { onMounted } from 'vue'

import PageHero from '@/components/PageHero.vue'
import AssistantPanel from '@/components/assistant/AssistantPanel.vue'
import { useAssistantCapabilities } from '@/composables/useAssistantCapabilities'

const { capabilities, error: capabilitiesError, loading: capabilitiesLoading, llmConfigured, load } = useAssistantCapabilities()

onMounted(() => void load())
</script>

<template>
  <div class="assistant">
    <PageHero title="语义交互" badge="对话" caption="对话 → 白名单动作 → 人工确认。">
      <template #actions>
        <a-tag v-if="capabilitiesError" color="red" data-testid="caps-state">读不到能力面</a-tag>
        <a-tag v-else-if="capabilities && !llmConfigured" color="orange" data-testid="caps-state">语义服务未配置</a-tag>
        <a-tag v-else-if="capabilities" color="green" data-testid="caps-state">能力面已读取</a-tag>
        <a-button size="small" :loading="capabilitiesLoading" data-testid="caps-refresh" @click="load">重读能力面</a-button>
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
  display: flex;
  flex-direction: column;
  gap: var(--aeg-space-s4);
  flex: 1;
  /* min-height:auto = 内容最小高度：值班台比一屏高时整页滚动，而不是把会话压回去 */
  min-height: auto;
}
</style>
