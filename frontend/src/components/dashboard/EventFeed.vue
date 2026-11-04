<script setup lang="ts">
/**
 * 实时事件流：一条事件一张小卡，和链路执行记录同一套卡片语言。
 *
 * 卡片两行：第一行 = 级别圆点 + 时间 + 类型标签；第二行 = 一句正文。
 * 默认只露 4 张（卡内滚动 + 底部渐隐），其余等"查看全部"再展开——
 * 和链路卡一样，不让行数把首屏顶走。
 * 时间仍走 `formatOperatingTime`（UTC+8，跨日进位），不给裸 ISO。
 */
import { computed, ref } from 'vue'

import type { StreamEvent } from '@/composables/useEventStream'
import { formatOperatingTime } from '@/utils/clock'

const props = defineProps<{ events: StreamEvent[] }>()

const VISIBLE_ROWS = 4
const expanded = ref(false)
const canExpand = computed(() => props.events.length > VISIBLE_ROWS)

/** 没有业务标题的事件（SSE 只带 warning_id 的那种）用类型名兜底，别把 id 当正文 */
const SUBJECT_LABELS: Record<string, string> = {
  'feedback.status': '反馈状态',
  'execute.ack': '执行确认',
}

function headlineOf(event: StreamEvent): string {
  const title = event.payload.title
  if (typeof title === 'string' && title.trim() !== '') return title
  const fallback = SUBJECT_LABELS[event.subject]
  if (fallback) return fallback
  return event.subject
}

/** 只用正文做级别的粗判：红色/崩塌类压红，预警类压橙，其余保持中性 */
function toneOf(event: StreamEvent): 'bad' | 'warn' | 'muted' {
  const text = `${headlineOf(event)} ${event.subject}`
  if (/红色|崩塌|critical|danger/i.test(text)) return 'bad'
  if (/橙色|黄色|预警|warn/i.test(text)) return 'warn'
  return 'muted'
}
</script>

<template>
  <div class="evt-feed">
    <div class="evt-feed__rows" :class="{ 'is-expanded': expanded }">
      <div
        v-for="event in events"
        :key="event.trace_id + event.ts"
        class="evt"
        :class="`is-${toneOf(event)}`"
        data-testid="evt-row"
        :title="`${event.subject} · ${event.trace_id}`"
      >
        <div class="evt__top">
          <i class="evt__dot" aria-hidden="true"></i>
          <span class="evt__time">{{ formatOperatingTime(event.ts) }}</span>
          <span class="evt__subject">{{ event.subject }}</span>
        </div>
        <span class="evt__text" :title="event.subject">{{ headlineOf(event) }}</span>
      </div>
    </div>

    <button
      v-if="canExpand"
      type="button"
      class="evt-feed__more"
      data-testid="events-more"
      @click="expanded = !expanded"
    >
      {{ expanded ? '收起' : `查看全部 ${events.length} 条` }}<span aria-hidden="true"> →</span>
    </button>

    <div v-if="!events.length" class="empty-hero empty-hero--compact" data-testid="events-empty">
      <span class="empty-hero__icon" aria-hidden="true">◇</span>
      <div>
        <div class="empty-hero__title">事件流还没有消息</div>
        <div class="empty-hero__desc">SSE 连上后链路每一步都会落在这里；一直空白就先跑一次演练看链路是否启动。</div>
        <router-link class="empty-hero__action empty-hero__cta" to="/workflow">去流程编排跑一次链路 →</router-link>
      </div>
    </div>
  </div>
</template>

<style scoped>
.evt-feed {
  display: flex;
  flex-direction: column;
  gap: var(--aeg-space-s2);
}

/* 默认 4 张卡（4×~55px + 3×8px 间距）；底部渐隐提示还有更多 */
.evt-feed__rows {
  display: flex;
  flex-direction: column;
  gap: var(--aeg-space-s2);
  max-height: 252px;
  overflow-y: auto;
  -webkit-mask-image: linear-gradient(to bottom, black calc(100% - 22px), transparent);
  mask-image: linear-gradient(to bottom, black calc(100% - 22px), transparent);
}
.evt-feed__rows.is-expanded {
  max-height: 380px;
  -webkit-mask-image: none;
  mask-image: none;
}
.evt-feed__rows::-webkit-scrollbar {
  width: 6px;
}
.evt-feed__rows::-webkit-scrollbar-thumb {
  border-radius: var(--aeg-radius-pill);
  background: var(--aeg-color-border);
}

/* 一条事件一张卡：与链路卡同一套圆角/描边/底色 */
.evt {
  display: flex;
  flex-direction: column;
  gap: var(--aeg-space-s1);
  padding: var(--aeg-space-s2) 10px;
  border: 1px solid var(--aeg-color-glass-border);
  border-radius: var(--aeg-radius-md);
  background: var(--aeg-color-sunken);
  transition:
    border-color var(--aeg-motion-base) ease,
    box-shadow var(--aeg-motion-base) ease;
}
.evt:hover {
  border-color: var(--aeg-color-brand);
  box-shadow: var(--aeg-shadow-sm);
}

.evt__top {
  display: flex;
  align-items: center;
  gap: var(--aeg-space-s2);
  min-width: 0;
}

.evt__dot {
  flex: 0 0 auto;
  width: 6px;
  height: 6px;
  border-radius: var(--aeg-radius-pill);
  background: var(--aeg-color-border-strong);
}
.evt.is-warn .evt__dot {
  background: var(--aeg-color-status-warning);
}
.evt.is-bad .evt__dot {
  background: var(--aeg-color-status-danger);
}

.evt__time {
  flex: 0 0 auto;
  font-family: var(--aeg-font-mono);
  font-size: var(--aeg-font-xs);
  font-variant-numeric: tabular-nums;
  color: var(--aeg-color-text-muted);
}

/* 类型标签退到第一行行尾 */
.evt__subject {
  flex: 0 1 auto;
  min-width: 0;
  max-width: 150px;
  margin-left: auto;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--aeg-font-mono);
  font-size: var(--aeg-font-xs);
  color: var(--aeg-color-text-muted);
}

.evt__text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--aeg-font-sm);
  color: var(--aeg-color-text);
}

.evt-feed__more {
  align-self: flex-end;
  padding: 0;
  border: none;
  background: transparent;
  color: var(--aeg-color-brand);
  font-size: var(--aeg-font-xs);
  cursor: pointer;
}
.evt-feed__more:hover {
  text-decoration: underline;
}
</style>
