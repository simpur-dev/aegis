<script setup lang="ts">
/**
 * 智能体在线面板（态势总览右栏）。
 *
 * 设计稿要求“一行一颗”：状态点 / 编号 / 类型 / 当前任务 / 成功·失败 / 详情箭头
 * 全部在同一行、垂直居中；不再做成两行小卡（那会把 12 颗智能体撑成一屏放不下）。
 * 点一行看详情（右栏装不下 capabilities/hazard_types/版本）。
 * 状态口径不动：在线/失联取注册表的 `healthy`。
 */
import type { AgentInfo } from '@/api/types'

defineProps<{ agents: AgentInfo[] }>()

const emit = defineEmits<{ select: [agent: AgentInfo] }>()
</script>

<template>
  <div class="agent-board">
    <div
      v-for="agent in agents"
      :key="agent.agent_id"
      class="agent"
      :data-testid="`agent-row-${agent.agent_id}`"
      role="button"
      tabindex="0"
      :aria-label="`查看智能体 ${agent.agent_id} 详情`"
      @click="emit('select', agent)"
      @keydown.enter.prevent="emit('select', agent)"
      @keydown.space.prevent="emit('select', agent)"
    >
      <span class="agent__dot" :class="agent.healthy ? 'is-live' : 'is-dead'" aria-hidden="true"></span>
      <span class="agent__id" :title="agent.agent_id">{{ agent.agent_id }}</span>
      <span class="agent__type" :title="agent.agent_type">{{ agent.agent_type }}</span>
      <span
        class="agent__load"
        :title="`当前任务 ${agent.inflight} / 上限 ${agent.max_concurrency}`"
      >{{ agent.inflight }}/{{ agent.max_concurrency }}</span>
      <span class="agent__served" :title="`已服务 ${agent.served} 次，失败 ${agent.failed} 次`">
        <span>成 {{ agent.served }}</span>
        <span :class="{ 'agent__failed': agent.failed > 0 }">败 {{ agent.failed }}</span>
      </span>
      <span class="agent__chevron" aria-hidden="true">›</span>
    </div>
    <div v-if="!agents.length" class="empty-hero" data-testid="agents-empty">
      <span class="empty-hero__icon" aria-hidden="true">◇</span>
      <div>
        <div class="empty-hero__title">还没有智能体注册上来</div>
        <div class="empty-hero__desc">后端起来后它们会逐个上线；跑一次演练即可看到它们接活。</div>
        <router-link class="empty-hero__action" to="/monitor">跑一次演练看谁接活 →</router-link>
      </div>
    </div>
  </div>
</template>

<style scoped>
.agent-board {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
/* 一行一颗：固定 38px，扫一列就能对齐读 */
.agent {
  display: flex;
  align-items: center;
  gap: var(--aeg-space-s2);
  height: 46px;
  padding: 0 var(--aeg-space-s1);
  border-bottom: 1px solid var(--aeg-color-border-subtle);
  border-radius: var(--aeg-radius-sm);
  cursor: pointer;
  transition: background-color var(--aeg-motion-base) ease;
}
.agent:last-child {
  border-bottom: none;
}
.agent:hover {
  background: var(--aeg-color-sunken);
}
.agent:focus-visible {
  outline: 2px solid var(--aeg-color-brand);
  outline-offset: -2px;
}
.agent__dot {
  flex: 0 0 auto;
  width: 7px;
  height: 7px;
  border-radius: var(--aeg-radius-pill);
  background: var(--aeg-color-status-danger);
}
.agent__dot.is-live {
  background: var(--aeg-color-status-success);
}
.agent__id {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--aeg-font-sm);
  font-weight: var(--aeg-weight-semibold);
  color: var(--aeg-color-text);
}
.agent__type {
  flex: 0 0 auto;
  max-width: 96px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding: 0 6px;
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-sm);
  font-size: var(--aeg-font-xs);
  color: var(--aeg-color-text-muted);
}
.agent__load {
  flex: 0 0 auto;
  min-width: 40px;
  text-align: right;
  font-size: var(--aeg-font-xs);
  font-variant-numeric: tabular-nums;
  color: var(--aeg-color-text-secondary);
}
.agent__served {
  display: inline-flex;
  flex: 0 0 auto;
  gap: 6px;
  min-width: 74px;
  justify-content: flex-end;
  font-size: var(--aeg-font-xs);
  font-variant-numeric: tabular-nums;
  color: var(--aeg-color-text-secondary);
}
.agent__failed {
  color: var(--aeg-color-status-danger);
}
.agent__chevron {
  flex: 0 0 auto;
  color: var(--aeg-color-text-muted);
  font-size: var(--aeg-font-base);
  line-height: 1;
}
</style>
