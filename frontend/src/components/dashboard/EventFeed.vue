<script setup lang="ts">
/**
 * 实时事件流（spike：dashboard 标杆页）。
 *
 * 弃用 a-timeline 的大圆点与竖线：值班员扫事件要的是"什么时候、发生了什么"两列对齐，
 * 时间轴那种一停一顿的排法把 8 条事件摊成半屏高。改成等宽时间戳 + 单行正文的日志排法，
 * 同屏容量翻倍，且时间列对齐后可按秒扫。
 *
 * 时间仍走 `formatOperatingTime`（UTC+8，跨日进位），不给裸 ISO；条数上限由调用方给。
 */
import type { StreamEvent } from '@/composables/useEventStream'
import { formatOperatingTime } from '@/utils/clock'

defineProps<{ events: StreamEvent[] }>()

function headlineOf(event: StreamEvent): string {
  const title = event.payload.title ?? event.payload.warning_id
  return typeof title === 'string' && title !== '' ? title : event.subject
}
</script>

<template>
  <div class="evt-feed">
    <div v-for="event in events" :key="event.trace_id + event.ts" class="evt" data-testid="evt-row">
      <span class="evt__time">{{ formatOperatingTime(event.ts) }}</span>
      <span class="evt__text" :title="event.subject">{{ headlineOf(event) }}</span>
      <span class="evt__subject">{{ event.subject }}</span>
    </div>
    <div v-if="!events.length" class="empty-hero" data-testid="events-empty">
      <span class="empty-hero__icon" aria-hidden="true">◇</span>
      <div>
        <div class="empty-hero__title">事件流还没有消息</div>
        <div class="empty-hero__desc">SSE 连上后链路每一步都会落在这里；一直空白就先跑一次演练看链路是否启动。</div>
        <router-link class="empty-hero__action" to="/workflow">去流程编排跑一次链路 →</router-link>
      </div>
    </div>
  </div>
</template>

<style scoped>
.evt-feed {
  display: flex;
  flex-direction: column;
}
.evt {
  display: flex;
  align-items: baseline;
  gap: var(--aeg-space-s2);
  padding: var(--aeg-space-s1) 0;
  border-bottom: 1px solid var(--aeg-color-border-subtle);
}
.evt:last-child {
  border-bottom: none;
}
.evt__time {
  flex: 0 0 auto;
  font-family: var(--aeg-font-mono);
  font-size: var(--aeg-font-xs);
  font-variant-numeric: tabular-nums;
  color: var(--aeg-color-text-muted);
}
.evt__text {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--aeg-font-sm);
  color: var(--aeg-color-text);
}
.evt__subject {
  flex: 0 0 auto;
  font-family: var(--aeg-font-mono);
  font-size: var(--aeg-font-xs);
  color: var(--aeg-color-text-muted);
}
</style>
