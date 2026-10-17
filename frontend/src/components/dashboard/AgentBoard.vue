<script setup lang="ts">
/**
 * 智能体在线板（spike：dashboard 标杆页）。
 *
 * 与旧 a-table 的差别：这页要回答的是"谁还活着、谁在忙、忙到什么程度"，
 * 表格把每颗智能体摊成五列等宽字，扫一遍要五次眼跳；行式紧凑排布 +
 * 在途/上限的小容量条，让"活着但一件没干"和"满负荷"两种状态一眼可分。
 *
 * 状态口径不动：在线/失联仍取注册表的 `healthy`（总线契约），这里不判"心跳多久没来"。
 */
import type { AgentInfo } from '@/api/types'

defineProps<{ agents: AgentInfo[] }>()

function loadRatio(agent: AgentInfo): number {
  if (agent.max_concurrency <= 0) return 0
  return Math.min(1, agent.inflight / agent.max_concurrency)
}
</script>

<template>
  <div class="agent-board">
    <div v-for="agent in agents" :key="agent.agent_id" class="agent" :data-testid="`agent-row-${agent.agent_id}`">
      <span class="agent__dot" :class="agent.healthy ? 'is-live' : 'is-dead'" aria-hidden="true"></span>
      <span class="agent__id" :title="agent.agent_id">{{ agent.agent_id }}</span>
      <span class="agent__type">{{ agent.agent_type }}</span>
      <span class="agent__caps" :title="agent.capabilities.join('、')">{{ agent.capabilities.join('、') || '—' }}</span>
      <span class="agent__load" :title="`在途 ${agent.inflight} / 上限 ${agent.max_concurrency}`">
        <span class="agent__loadbar" aria-hidden="true"><i :style="{ width: `${Math.round(loadRatio(agent) * 100)}%` }"></i></span>
        <span class="agent__loadnum">{{ agent.inflight }}/{{ agent.max_concurrency }}</span>
      </span>
      <span class="agent__served" :title="`已服务 ${agent.served} 次，失败 ${agent.failed} 次`">成 {{ agent.served }} · 败 {{ agent.failed }}</span>
    </div>
    <div v-if="!agents.length" class="empty-hero" data-testid="agents-empty">
      <span class="empty-hero__icon" aria-hidden="true">◇</span>
      <div>
        <div class="empty-hero__title">还没有智能体注册上来</div>
        <div class="empty-hero__desc">智能体经总线契约注册；后端起来后它们会逐个出现在这里，跑一次演练即可看到它们接活。</div>
        <router-link class="empty-hero__action" to="/monitor">去监测页发起演练 →</router-link>
      </div>
    </div>
  </div>
</template>

<style scoped>
.agent-board {
  display: flex;
  flex-direction: column;
}
.agent {
  display: flex;
  align-items: center;
  gap: var(--aeg-space-s2);
  padding: var(--aeg-space-s2) var(--aeg-space-s1);
  border-bottom: 1px solid var(--aeg-color-border-subtle);
}
.agent:last-child {
  border-bottom: none;
}
.agent__dot {
  flex: 0 0 auto;
  width: 8px;
  height: 8px;
  border-radius: var(--aeg-radius-pill);
  background: var(--aeg-color-border-strong);
}
.agent__dot.is-live {
  background: var(--aeg-color-status-success);
}
.agent__dot.is-dead {
  background: var(--aeg-color-status-danger);
}
.agent__id {
  flex: 0 0 auto;
  overflow: hidden;
  max-width: 180px;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--aeg-font-mono);
  font-size: var(--aeg-font-sm);
  font-weight: var(--aeg-weight-medium);
  color: var(--aeg-color-text);
}
.agent__type {
  flex: 0 0 auto;
  padding: 0 6px;
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-sm);
  background: var(--aeg-color-sunken);
  font-size: var(--aeg-font-xs);
  color: var(--aeg-color-text-secondary);
}
.agent__caps {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--aeg-font-xs);
  color: var(--aeg-color-text-muted);
}
.agent__load {
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 6px;
}
.agent__loadbar {
  width: 44px;
  height: 4px;
  border-radius: var(--aeg-radius-pill);
  background: var(--aeg-color-sunken);
}
.agent__loadbar i {
  display: block;
  height: 100%;
  border-radius: var(--aeg-radius-pill);
  background: var(--aeg-color-brand);
}
.agent__loadnum {
  font-size: var(--aeg-font-xs);
  font-variant-numeric: tabular-nums;
  color: var(--aeg-color-text-secondary);
}
.agent__served {
  flex: 0 0 auto;
  min-width: 84px;
  text-align: right;
  font-size: var(--aeg-font-xs);
  font-variant-numeric: tabular-nums;
  color: var(--aeg-color-text-muted);
}
</style>
