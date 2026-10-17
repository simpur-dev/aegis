<script setup lang="ts">
import { message } from 'ant-design-vue'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import api from '@/api/client'
import type { AgentInfo, ChainSummary, LatencyReport, WarningRecord } from '@/api/types'
import { HAZARD_LABELS } from '@/api/types'
import { workflowApi } from '@/api/workflow'
import PageHero from '@/components/PageHero.vue'
import KpiStrip, { type KpiItem } from '@/components/dashboard/KpiStrip.vue'
import AgentBoard from '@/components/dashboard/AgentBoard.vue'
import ChainList from '@/components/dashboard/ChainList.vue'
import EventFeed from '@/components/dashboard/EventFeed.vue'
import RiskWall from '@/components/dashboard/RiskWall.vue'
import LegStatusPanel from '@/components/system/LegStatusPanel.vue'
import { useEventStream } from '@/composables/useEventStream'
import { formatOperatingTime } from '@/utils/clock'

const REFRESH_MS = 15_000
/** 链路表与事件补数都取这个窗口，卡片标题里的"最近 N 条"必须与它同一个数。 */
const CHAIN_WINDOW = 20

const ready = ref<{ agents_online: number; store: Record<string, number> } | null>(null)
const agents = ref<AgentInfo[]>([])
const warnings = ref<WarningRecord[]>([])
const chains = ref<ChainSummary[]>([])
const latency = ref<LatencyReport | null>(null)
const loading = ref(false)
/** 这页的数字是什么时候取的。没有它，"预警 3 条"到底是"只有 3 条"还是"页面 20 分钟没动"就无从判断。 */
const updatedAt = ref<string | null>(null)
const refreshError = ref<string | null>(null)
/**
 * 等人签的工单数。`null` = 还没取到：这条是值班员上班第一眼要看的东西，
 * 取不到就什么都不摆，不拿 0 冒充"没有单在等"。
 */
const pendingSign = ref<number | null>(null)
let timer: ReturnType<typeof setInterval> | null = null
let eventTimer: ReturnType<typeof setTimeout> | null = null
/**
 * "最新那趟取链路的人"的编号。这张表有两个写手：15 秒轮询与事件流触发的补数。
 * 补数本来就是为"时间线已经有了、这张表还要等下一次轮询"而加的，
 * 若轮询那份更早发出的快照迟到落地又把表整个换回去，刚补上的那条就再次消失。
 */
let chainsSeq = 0
const { events } = useEventStream()

/**
 * KPI 只写"量到的数"。取数失败或还没取到时写 `—`，不写 0：
 * 首屏 500 之后这里曾是"在线智能体 0 个 / 已发布预警 0 条 / 任务单元 0 个"，
 * 页脚明明说了取数失败，可三个大数字看着像是"现场确实一个都没有"。
 */
const kpis = computed<KpiItem[]>(() => [
  { label: '在线智能体', value: ready.value === null ? '—' : ready.value.agents_online, suffix: '个' },
  // 取后端口径的总数，不用列表长度：`warnings` 是按 `limit: 50` 拉回来的，
  // 拿它的长度当"已发布预警"，过 50 条之后这个数字就永远停在 50，而且看着完全正常。
  {
    label: '已发布预警',
    value: ready.value?.store?.warning_count ?? (warnings.value.length > 0 ? warnings.value.length : '—'),
    suffix: '条',
  },
  { label: '任务单元', value: ready.value?.store?.task_count ?? '—', suffix: '个' },
  {
    label: '协同成功率',
    value: latency.value?.collaboration.success_rate == null ? '—' : `${(latency.value.collaboration.success_rate * 100).toFixed(1)}%`,
    // 口径注脚只写给得出目标的数：目标与达标判定都来自时延账本，不是这里估的。
    note:
      latency.value?.collaboration.success_rate == null
        ? undefined
        : `目标 ${(latency.value.collaboration.target * 100).toFixed(0)}% · ${latency.value.collaboration.pass ? '达标' : '样本不足'}`,
    tone: latency.value?.collaboration.pass ? 'ok' : 'idle',
  },
])

/**
 * 表格里按"最新在上"呈现。
 *
 * 后端 `/api/v1/events` 给的是最近 N 条**按时间正序**（`BoundedCollection.latest`：
 * 取尾部窗口，旧→新），照原样摆上去，第一页就永远是几条最旧的：真机连点两次上报，
 * 时间线立刻出"西藏泥石流预警（橙色）"，而这张"链路执行记录"第一页一个字没变——
 * 刚发生的那条排在第 2 页，等于"最近做了什么"要翻到最后一页才看得见。
 */
const recentChains = computed<ChainSummary[]>(() => [...chains.value].reverse())

/**
 * 这张表取的是最近 N 条链路，标题里就得写 N：真机把这台进程演练到 108 条之后，
 * 卡片只说"最新在上"，看不出 88 条在表外（`/readyz` 的 chain_count 才是总数）。
 */
const chainsTitle = computed<string>(() => {
  const base = `链路执行记录（最近 ${CHAIN_WINDOW} 条，最新在上；感知→研判→决策→执行→反馈）`
  const total = ready.value?.store?.chain_count ?? null
  return total !== null && total > CHAIN_WINDOW ? `${base}｜服务端共 ${total} 条` : base
})

/** 网格吃的是同一个 20 条窗口，标题就得和旁边的表说同一句实话。 */
const gridTitle = computed<string>(() => {
  const base = `风险区域网格（按最近 ${CHAIN_WINDOW} 条链路归集）`
  const total = ready.value?.store?.chain_count ?? null
  return total !== null && total > CHAIN_WINDOW ? `${base}｜服务端共 ${total} 条` : base
})

const regionGrid = computed(() => {
  const byRegion = new Map<string, ChainSummary>()
  for (const chain of chains.value) {
    /**
     * 没研判出风险的链路只能说"未标注区域"。原先这里退到 `task_units[0].slice(4, 10)`，
     * 于是 `stu_5401210abcd` 会在"风险区域网格"里摆出一个 `540121`——长得和真区划代码
     * 一模一样，读的人无从知道它是从任务单元号上截出来的（用例钉住这一点）。
     */
    const region = chain.risk?.region_code ?? '未标注区域'
    const previous = byRegion.get(region)
    if (!previous || (chain.risk && previous.risk && chain.risk.risk_level < previous.risk.risk_level)) {
      byRegion.set(region, chain)
    }
  }
  return [...byRegion.entries()]
    .map(([region, chain]) => ({
      region,
      hazard: chain.risk ? HAZARD_LABELS[chain.risk.hazard_type] : '无风险',
      level: chain.risk?.risk_level ?? 5,
      confidence: chain.risk?.confidence ?? 0,
      degraded: chain.degradations.length > 0,
    }))
    .sort((a, b) => a.level - b.level || b.confidence - a.confidence || a.region.localeCompare(b.region))
})

async function refresh(): Promise<void> {
  loading.value = true
  const seq = ++chainsSeq
  try {
    const [readyInfo, agentInfo, warningList, eventList, latencyReport, instanceList] = await Promise.all([
      api.ready(),
      api.agents(),
      api.warnings({ limit: 50 }),
      api.events(CHAIN_WINDOW),
      api.latency(),
      // 总览要报"有几张工单在等人签"：真机量过，这台进程 2 张工单等着签，
      // 而落地页上"待签/等人签/核签/工单"字样 0 次——工单在等，页面不吭声。
      workflowApi.instances(),
    ])
    ready.value = { agents_online: readyInfo.agents_online, store: readyInfo.store }
    agents.value = agentInfo.items
    warnings.value = warningList.items
    pendingSign.value = instanceList.items.filter((row) => row.status === 'waiting').length
    // 这趟在路上时补数可能已经把新链路补进来了：更旧的快照迟到就丢掉。
    if (seq === chainsSeq) chains.value = eventList.items
    latency.value = latencyReport
    updatedAt.value = new Date().toISOString()
    refreshError.value = null
  } catch (error) {
    // 失败要留在页面上：只弹一条 3 秒就消失的 message，值班员回头只看得到数字，
    // 分不清"没有新预警"和"取数一直在失败"。原因也不能吞成"未知错误"——
    // 非 ApiError 的异常（网络层抛的裸 Error 等）同样带着现场要看的字。
    const reason = error instanceof Error ? error.message : String(error)
    refreshError.value = reason.trim() || '未知错误'
    message.error(`加载失败：${refreshError.value}`)
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  void refresh()
  // 页签切到后台就不发请求（与腿面板同一口径）：大屏副页签没人看，轮询却一直在打接口。
  timer = setInterval(() => {
    if (document.visibilityState !== 'visible') return
    void refresh()
  }, REFRESH_MS)
})

onBeforeUnmount(() => {
  if (timer !== null) clearInterval(timer)
  timer = null
  if (eventTimer !== null) clearTimeout(eventTimer)
  eventTimer = null
})

/**
 * 事件流里出现新链路时补一次取数（1.2 秒内多条事件只补一次）。
 *
 * 这张表本来 15 秒轮询一次，而时间线是即时的：真机上刚发的预警已经出现在时间线里，
 * "链路执行记录"却还要等下一次轮询才有它——同一块屏幕上"已经发生"与"记录里没有"并存。
 * 一次演练会连发十几条事件，逐条打接口就是请求风暴，所以按下同一把计时器。
 */
watch(
  () => events.value[0]?.trace_id ?? '',
  (trace) => {
    if (trace === '' || eventTimer !== null) return
    if (chains.value.some((chain) => chain.trace_id === trace)) return
    eventTimer = setTimeout(() => {
      eventTimer = null
      if (document.visibilityState !== 'visible') return
      void (async () => {
        const seq = ++chainsSeq
        try {
          const list = await api.events(CHAIN_WINDOW)
          // 补数拿到的必然是更新的一份，所以它也要占号；但若这趟在路上时
          // 轮询又发了更新的一趟（seq 已不是自己），那次才该写。
          if (seq === chainsSeq) {
            chains.value = list.items
            updatedAt.value = new Date().toISOString()
          }
          refreshError.value = null
        } catch (caught) {
          // 后台补数失败也要看得见：静默失败正是"页面数字停在旧值却像现场没动静"
          refreshError.value = caught instanceof Error ? caught.message : String(caught)
        }
      })()
    }, 1_200)
  },
)
</script>

<template>
  <div>
    <PageHero title="态势总览" badge="实时" caption="值班员的第一屏：要人处理的待办、现场读数、风险瓦片、链路记录与实时事件流。">
      <template #actions>
        <span data-testid="dashboard-updated">
          {{ updatedAt ? `更新于 ${formatOperatingTime(updatedAt)}（UTC+8）` : '尚未取到数据' }}
        </span>
        <a-button size="small" :loading="loading" data-testid="dashboard-refresh" @click="refresh">刷 新</a-button>
      </template>
    </PageHero>
    <!-- 值班员的任务入口：有单在等就顶到第一屏，和流程页的待办条同一句话、同一个数 -->
    <div v-if="pendingSign !== null && pendingSign > 0" class="dash-todos" data-testid="dashboard-todos">
      <span>有 {{ pendingSign }} 张工单在等人签。</span>
      <router-link class="dash-todos__link" to="/workflow">去处理</router-link>
    </div>
    <a-alert
      v-if="refreshError"
      type="warning"
      show-icon
      :message="`最近一次取数失败：${refreshError}（页面显示的是上一次成功取到的数字）`"
      data-testid="dashboard-error"
      class="dash-error"
    />
    <KpiStrip :items="kpis" />
    <a-row :gutter="12">
      <a-col :xs="24" :lg="10">
        <a-card :title="gridTitle" size="small" :loading="loading">
          <RiskWall :cells="regionGrid" :window-size="CHAIN_WINDOW" />
        </a-card>
      </a-col>

      <a-col :xs="24" :lg="14">
        <a-card :title="chainsTitle" size="small" :loading="loading">
          <ChainList :chains="recentChains" />
        </a-card>
      </a-col>
    </a-row>

    <a-row :gutter="12" class="dash-row">
      <a-col :xs="24" :lg="14">
        <a-card title="智能体在线状态（经总线契约注册）" size="small">
          <AgentBoard :agents="agents" />
        </a-card>
      </a-col>
      <a-col :xs="24" :lg="10">
        <a-card title="实时事件流（SSE）" size="small">
          <EventFeed :events="events.slice(0, 8)" />
        </a-card>
      </a-col>
    </a-row>

    <a-row class="dash-row">
      <a-col :span="24">
        <!-- 垫在明细之后：KPI 与链路要先占住首屏，但这块必须和它们在同一页——
             判"预案为什么变慢"时翻的是这个，不是日志。 -->
        <LegStatusPanel />
      </a-col>
    </a-row>
  </div>
</template>

<style scoped>
/* KPI 托盘（.stat-cell）与空态（.empty-hero）的样式在 styles/theme.css：两页共用一份。 */
.dash-row {
  margin-top: var(--aeg-space-s3);
}
.dash-error {
  margin-bottom: var(--aeg-space-s3);
}
/* 待办条是"要人去做的事"：动作位给它主色左带 + brand-soft 底，与数据卡分开一档。
   旧的紫色雾底（#f9f0ff/#531dab）是浅色主题限定的字面色，深色下不成立，已退役。 */
.dash-todos {
  display: flex;
  gap: var(--aeg-space-s3);
  align-items: center;
  margin-bottom: var(--aeg-space-s3);
  padding: var(--aeg-space-s2) var(--aeg-space-s3);
  border: 1px solid var(--aeg-color-brand-line);
  border-left: 3px solid var(--aeg-color-brand);
  border-radius: var(--aeg-radius-md);
  background: var(--aeg-color-brand-soft);
  color: var(--aeg-color-text);
  font-size: var(--aeg-font-sm);
}
.dash-todos__link {
  font-weight: var(--aeg-weight-semibold);
  /* 链接压在 brand-soft 合成底上：浅档 #1D4ED8 / 深档 #60A5FA，两档都过 4.5:1 */
  color: var(--aeg-color-brand-hover);
  text-decoration: underline;
}
</style>
