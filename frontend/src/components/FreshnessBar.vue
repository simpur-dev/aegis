<script setup lang="ts">
/**
 * 一条"这数从哪来、多旧"的横条：左端是窗口口径，右端是取数时刻与距今多久，中间一道线。
 *
 * 做法来自 uptime-kuma 的心跳条两侧时间戳（`HeartbeatBar.vue:23-28,232-238,839-849`：
 * 左边窗口起点、右边"距上次心跳"），NexusMind 与 go-view 都没有这一层——它们只说"每 30 秒轮询"，
 * 界面不说数据什么时候到的。15 秒一轮的台子挂着不动时，人和数字一起过期，得看得出来。
 */
import { computed, onUnmounted, ref } from 'vue'

import { operatingClock } from '@/utils/clock'

const props = withDefaults(
  defineProps<{
    /** 后端回来的 UTC ISO；一次都没取到时传 null——不拿当前时间冒充"更新于"。 */
    fetchedAt: string | null
    /** 左端那句口径（"最近 100 条 ｜ 台账 6 条"这类）。 */
    label?: string
    intervalMs?: number
  }>(),
  { label: '本页数据', intervalMs: 15_000 },
)

const now = ref(Date.now())
const timer = setInterval(() => {
  now.value = Date.now()
}, 1_000)
onUnmounted(() => clearInterval(timer))

const ageSec = computed<number | null>(() => {
  if (props.fetchedAt === null) return null
  const at = Date.parse(props.fetchedAt)
  if (Number.isNaN(at)) return null
  return Math.max(0, Math.round((now.value - at) / 1000))
})
const stale = computed<boolean>(() => ageSec.value !== null && ageSec.value * 1000 > props.intervalMs * 2)
const rightLabel = computed<string>(() => {
  if (ageSec.value === null) return '尚未取到数据'
  const at = `取数 ${operatingClock(props.fetchedAt)}（UTC+8）`
  const age = ageSec.value < 60 ? `${ageSec.value} 秒前` : `${Math.round(ageSec.value / 60)} 分前`
  return stale.value ? `${at} ｜ ${age}，比预期周期慢` : `${at} ｜ ${age}`
})
</script>

<template>
  <p class="fresh" :class="{ 'is-stale': stale }" data-testid="freshness">
    <span class="fresh__side fresh__left">{{ label }}</span>
    <span class="fresh__rule" aria-hidden="true"></span>
    <span class="fresh__side fresh__right">{{ rightLabel }}</span>
  </p>
</template>

<style scoped>
.fresh {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 0 0 8px;
  font-size: 12px;
  line-height: 1.4;
  color: #5a6072;
  font-variant-numeric: tabular-nums;
}
/* 两端都可缩不可撑：中间那道线是 flex:1，窄屏下先让文字省略号，不许换行成两排 */
.fresh__side {
  flex: 0 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.fresh__rule {
  flex: 1 1 auto;
  height: 1px;
  min-width: 12px;
  background: linear-gradient(90deg, rgba(37, 99, 235, 0.22), rgba(37, 99, 235, 0.06));
}
.fresh.is-stale {
  color: #8a4005;
}
.fresh.is-stale .fresh__rule {
  background: linear-gradient(90deg, rgba(245, 158, 11, 0.35), rgba(245, 158, 11, 0.1));
}
</style>
