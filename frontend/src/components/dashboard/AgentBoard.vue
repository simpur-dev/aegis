<script setup lang="ts">
/**
 * 智能体在线面板（态势总览右栏）。
 *
 * 设计稿要求“一行一颗”：状态点 / 编号 / 类型 / 当前任务 / 成功·失败 / 详情箭头
 * 全部在同一行、垂直居中；不再做成两行小卡（那会把 12 颗智能体撑成一屏放不下）。
 * 点一行看详情（右栏装不下 capabilities/hazard_types/版本）。
 * 状态口径不动：在线/失联取注册表的 `healthy`。
 */
import { CheckCircleFilled, CloseCircleFilled, RightOutlined, RobotOutlined } from '@ant-design/icons-vue'

import type { AgentInfo } from '@/api/types'

defineProps<{ agents: AgentInfo[] }>()

const emit = defineEmits<{ select: [agent: AgentInfo] }>()
</script>

<template>
  <div class="agent-board">
    <button
      v-for="agent in agents"
      :key="agent.agent_id"
      type="button"
      class="agent"
      :data-testid="`agent-row-${agent.agent_id}`"
      :aria-label="`查看智能体 ${agent.agent_id} 详情，${agent.healthy ? '在线' : '离线'}`"
      @click="emit('select', agent)"
    >
      <span class="agent__dot" :class="agent.healthy ? 'is-live' : 'is-dead'" aria-hidden="true"></span>
      <span class="agent__id" :title="agent.agent_id">{{ agent.agent_id }}</span>
      <span class="agent__type" :title="agent.agent_type">{{ agent.agent_type }}</span>
      <span
        class="agent__load"
        :title="`当前任务 ${agent.inflight} / 上限 ${agent.max_concurrency}`"
      >{{ agent.inflight }}/{{ agent.max_concurrency }}</span>
      <span class="agent__served" :title="`已服务 ${agent.served} 次，失败 ${agent.failed} 次`">
        <span><CheckCircleFilled class="agent__success" aria-hidden="true" /> {{ agent.served }}</span>
        <span><CloseCircleFilled class="agent__failed" aria-hidden="true" /> {{ agent.failed }}</span>
      </span>
      <RightOutlined class="agent__chevron" aria-hidden="true" />
    </button>
    <div v-if="!agents.length" class="empty-hero" data-testid="agents-empty">
      <span class="empty-hero__icon" aria-hidden="true"><RobotOutlined /></span>
      <div>
        <div class="empty-hero__title">还没有智能体注册上来</div>
        <div class="empty-hero__desc">后端起来后它们会逐个上线；跑一次演练即可看到它们接活。</div>
        <router-link class="empty-hero__action" to="/monitor#drill">跑一次演练看谁接活 →</router-link>
      </div>
    </div>
  </div>
</template>

<style scoped>
.agent-board {
  container-type: inline-size;
  display: flex;
  flex-direction: column;
  gap: var(--aeg-space-s1);
}
/* 一行一颗：CSS Grid 固定列，扫一列就能对齐读 */
.agent {
  display: grid;
  grid-template-columns: 9px minmax(0, 1fr) 64px 40px 77px 12px;
  align-items: center;
  column-gap: var(--aeg-space-s3);
  min-height: 39px;
  padding: 0 var(--aeg-space-s2);
  border: 1px solid var(--aeg-color-border-subtle);
  border-radius: var(--aeg-radius-md);
  background: var(--aeg-color-overview-tile);
  font: inherit;
  text-align: start;
  cursor: pointer;
  transition: background-color var(--aeg-motion-base) ease;
}
.agent:hover {
  background: var(--aeg-color-sunken);
}
html[data-theme='dark'] .agent {
  border-color: transparent;
  background: var(--dash-detail-face) padding-box, var(--dash-detail-rim) border-box;
}
html[data-theme='dark'] .agent:hover {
  border-color: var(--aeg-color-overview-accent);
}
.agent:focus-visible {
  outline: 2px solid var(--aeg-color-brand);
  outline-offset: -2px;
}
.agent__dot {
  justify-self: start;
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
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  font-weight: var(--aeg-weight-semibold);
  color: var(--aeg-color-text);
}
.agent__type {
  width: 64px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-align: center;
  padding: 1px 0;
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-pill);
  font-size: 11px;
  color: var(--aeg-color-text-secondary);
}
.agent__load {
  text-align: right;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  color: var(--aeg-color-text-secondary);
}
.agent__served {
  display: inline-flex;
  gap: 6px;
  justify-content: flex-end;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  color: var(--aeg-color-text-secondary);
}
.agent__failed {
  color: var(--aeg-color-status-danger);
}
.agent__success {
  color: var(--aeg-color-status-success);
}
.agent__chevron {
  justify-self: end;
  flex: 0 0 auto;
  color: var(--aeg-color-overview-accent);
  font-size: var(--aeg-font-xs);
  line-height: 1;
}
@container (max-width: 370px) {
  .agent {
    grid-template-columns: 7px minmax(0, 1fr) 46px 30px 56px 8px;
    column-gap: 6px;
    padding-inline: 4px;
  }
  .agent__type {
    width: auto;
  }
  .agent__served {
    gap: 3px;
  }
}
</style>
