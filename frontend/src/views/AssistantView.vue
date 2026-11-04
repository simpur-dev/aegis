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
.assistant {
  display: flex;
  flex-direction: column;
  gap: var(--aeg-space-s4);
}
</style>
