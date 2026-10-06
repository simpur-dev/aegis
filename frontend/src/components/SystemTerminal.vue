<script setup lang="ts">
/**
 * 系统输出浮层：引擎与事件流的实时出口，不用离开这一页、也不用开控制台。
 *
 * 形状移植 NexusMind `Step1GraphBuild.vue:1054-1234`（`.system-terminal`）：
 * 深底 `rgba(15,23,42,.85)` + `blur(12px)` + 顶边一道主色 + 三颗 9px 窗控点 +
 * 标签 `10px / letter-spacing .18em` + 11px 等宽日志行 + 时间戳固定宽。
 * 色相按我们的口径走蓝系（它自己那支是 indigo/紫青混的）。
 *
 * 两条硬约束：
 * ①默认收起。展开态高 188px，压在监测页右下角会盖住表格最后几行——值班员要看得自己点开；
 * ②数据源是 `useEventStream()` 那份**共享**流（全应用一条），这里不再自己 new EventSource。
 */
import { computed, ref } from 'vue'

import { useEventStream } from '@/composables/useEventStream'
import { operatingClock } from '@/utils/clock'

const { events, connected, stalled } = useEventStream()

const open = ref(false)
const maxLines = 40

const lines = computed(() =>
  events.value.slice(0, maxLines).map((event) => ({
    key: `${event.ts}-${event.trace_id}-${event.subject}`,
    clock: operatingClock(event.ts),
    subject: event.subject,
    trace: event.trace_id === '' ? '—' : event.trace_id,
  })),
)
const statusLine = computed<string>(() => {
  if (!connected.value && stalled.value) return '事件流没心跳，正在重连…'
  if (!connected.value) return '事件流未连接，正在重连…'
  return ''
})
</script>

<template>
  <section class="sys-term" :class="{ 'is-open': open }" data-testid="sys-term">
    <button
      type="button"
      class="sys-term__chip"
      :aria-expanded="open"
      data-testid="sys-term-toggle"
      @click="open = !open"
    >
      <span class="sys-term__dots" aria-hidden="true"><i></i><i></i><i></i></span>
      <span class="sys-term__label">系统输出</span>
      <span class="sys-term__count">{{ events.length }}</span>
      <span class="sys-term__caret" aria-hidden="true">{{ open ? '▾' : '▴' }}</span>
    </button>
    <div v-if="open" class="sys-term__body" role="log" aria-label="系统输出">
      <p v-if="statusLine !== ''" class="sys-term__warn">{{ statusLine }}</p>
      <p v-if="lines.length === 0 && statusLine === ''" class="sys-term__empty">
        还没有事件。链路跑起来之后，引擎与通道的每一帧都会落在这里。
      </p>
      <ul v-if="lines.length > 0" class="sys-term__list">
        <li v-for="line in lines" :key="line.key" class="sys-term__row">
          <span class="sys-term__time">{{ line.clock }}</span>
          <span class="sys-term__subject">{{ line.subject }}</span>
          <span class="sys-term__trace">{{ line.trace }}</span>
        </li>
      </ul>
    </div>
  </section>
</template>

<style scoped>
/* 走文档流，不 fixed。第一版按 NexusMind 那样浮在右下角，真机门禁当场量出它压住分页控件
   （"10 条/页" × "系统输出" 418px²，六档全中）——NexusMind 那条浮层是 absolute 在一个定高画布里，
   我们这一页是整页滚动的正文，没有那块可以"借"的底。 */
.sys-term {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  margin-top: 12px;
}
.sys-term__chip {
  display: flex;
  align-items: center;
  gap: 8px;
  width: auto;
  padding: 7px 12px;
  border: 1px solid rgba(148, 163, 184, 0.18);
  border-radius: 999px;
  background: rgba(15, 23, 42, 0.85);
  -webkit-backdrop-filter: blur(12px);
  backdrop-filter: blur(12px);
  color: #cbd5e1;
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  letter-spacing: 0.08em;
  cursor: pointer;
  transition: background var(--motion-base) ease, border-color var(--motion-base) ease;
  margin-left: auto;
}
.sys-term__chip:hover {
  border-color: rgba(59, 130, 246, 0.5);
  background: rgba(15, 23, 42, 0.94);
}
.sys-term__dots {
  display: inline-flex;
  gap: 4px;
}
.sys-term__dots i {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: #1e293b;
}
.sys-term__dots i + i {
  background: #334155;
}
.sys-term__dots i + i + i {
  background: #475569;
}
.sys-term__label {
  font-weight: 700;
  letter-spacing: 0.18em;
}
.sys-term__count {
  padding: 1px 7px;
  border-radius: 999px;
  background: rgba(59, 130, 246, 0.22);
  color: #bfdbfe;
  font-variant-numeric: tabular-nums;
}
.sys-term__caret {
  color: #94a3b8;
}
.sys-term__body {
  overflow: hidden;
  width: min(460px, 100%);
  margin-top: 8px;
  height: clamp(96px, 18vh, 188px);
  padding: 10px 12px;
  border: 1px solid rgba(148, 163, 184, 0.18);
  border-top: 1px solid rgba(59, 130, 246, 0.5);
  border-radius: 16px;
  background: rgba(15, 23, 42, 0.85);
  -webkit-backdrop-filter: blur(12px);
  backdrop-filter: blur(12px);
  box-shadow: 0 20px 50px rgba(15, 23, 42, 0.3);
  font-family: 'JetBrains Mono', monospace;
}
.sys-term__list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 0;
  padding: 0 4px 0 0;
  height: 100%;
  overflow-y: auto;
  list-style: none;
}
.sys-term__row {
  display: grid;
  grid-template-columns: 78px minmax(0, 1fr) auto;
  gap: 8px;
  align-items: baseline;
  font-size: 11px;
  line-height: 1.5;
  color: #94a3b8;
}
.sys-term__time {
  color: #94a3b8;
  font-variant-numeric: tabular-nums;
}
.sys-term__subject {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: #cbd5e1;
}
.sys-term__trace {
  color: #94a3b8;
}
.sys-term__empty,
.sys-term__warn {
  margin: 0;
  font-size: 11px;
  line-height: 1.6;
  color: #94a3b8;
}
.sys-term__warn {
  margin-bottom: 6px;
  color: #fbbf24;
}
</style>
