<script setup lang="ts">
import { message } from 'ant-design-vue'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import api from '@/api/client'
import type { AgentInfo, ChainSummary, LatencyReport, WarningRecord } from '@/api/types'
import { HAZARD_LABELS } from '@/api/types'
import { workflowApi } from '@/api/workflow'
import KpiStrip, { type KpiItem } from '@/components/dashboard/KpiStrip.vue'
import PageHero from '@/components/PageHero.vue'
import AgentBoard from '@/components/dashboard/AgentBoard.vue'
import ChainList from '@/components/dashboard/ChainList.vue'
import DashboardTerrainScene from '@/components/dashboard/DashboardTerrainScene.vue'
import { anchorForRegion } from '@/components/dashboard/terrainAnchors'
import type { SceneCell } from '@/components/dashboard/terrainAnchors'
import EventFeed from '@/components/dashboard/EventFeed.vue'
import RiskWall from '@/components/dashboard/RiskWall.vue'
import LegStatusPanel from '@/components/system/LegStatusPanel.vue'
import { useAlerts } from '@/composables/useAlerts'
import { useEventStream } from '@/composables/useEventStream'
import { formatOperatingTime } from '@/utils/clock'

const REFRESH_MS = 15_000
/** 链路表与事件补数都取这个窗口，卡片标题里的"最近 N 条"必须与它同一个数。 */
const CHAIN_WINDOW = 20

/** 告警条在顶部不再独占一行：总览页只留标题旁的一枚紧凑提示（App 全局横幅在本页隐藏） */
const { urgentCards } = useAlerts()

const ready = ref<{ agents_online: number; store: Record<string, number> } | null>(null)
/** 上一次成功取到的台账快照：KPI 趋势用它和本次算真实差值，绝不编"较昨日 +12%"。
 *  首屏（null）没有上一次，就不摆趋势——没有差值可说。 */
const prevReady = ref<{ agents_online: number; store: Record<string, number> } | null>(null)
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
/** 两次轮询的真实差值趋势：没有上一次（首屏）或本次没取到就不给，卡片落口径注脚或留白。 */
function deltaTrend(current: number | null | undefined, previous: number | null | undefined, unit: string): KpiItem['trend'] {
  if (current == null || previous == null) return undefined
  const delta = current - previous
  // 持平不展示：没有信息量的“较上次持平”只会让四张卡变吵
  if (delta === 0) return undefined
  return { text: `较上次 ${delta > 0 ? '+' : ''}${delta} ${unit}`, dir: delta > 0 ? 'up' : 'down' }
}

const kpis = computed<KpiItem[]>(() => [
  {
    label: '在线智能体',
    value: ready.value === null ? '—' : ready.value.agents_online,
    suffix: '个',
    trend: deltaTrend(ready.value?.agents_online, prevReady.value?.agents_online, '个'),
  },
  // 取后端口径的总数，不用列表长度：`warnings` 是按 `limit: 50` 拉回来的，
  // 拿它的长度当"已发布预警"，过 50 条之后这个数字就永远停在 50，而且看着完全正常。
  {
    label: '已发布预警',
    value: ready.value?.store?.warning_count ?? (warnings.value.length > 0 ? warnings.value.length : '—'),
    suffix: '条',
    trend: deltaTrend(ready.value?.store?.warning_count, prevReady.value?.store?.warning_count, '条'),
  },
  {
    label: '任务单元',
    value: ready.value?.store?.task_count ?? '—',
    suffix: '个',
    trend: deltaTrend(ready.value?.store?.task_count, prevReady.value?.store?.task_count, '个'),
  },
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
  const base = `事件处置记录（最近 ${CHAIN_WINDOW} 条，新的在上）`
  const total = ready.value?.store?.chain_count ?? null
  return total !== null && total > CHAIN_WINDOW ? `${base}｜共 ${total} 条` : base
})

/** 网格吃的是同一个 20 条窗口，标题就得和旁边的表说同一句实话。 */
const gridTitle = computed<string>(() => {
  const base = `风险区域一览（按最近 ${CHAIN_WINDOW} 条处置归集）`
  const total = ready.value?.store?.chain_count ?? null
  return total !== null && total > CHAIN_WINDOW ? `${base}｜共 ${total} 条` : base
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
      id: region,
      region,
      hazard: chain.risk ? HAZARD_LABELS[chain.risk.hazard_type] : '无风险',
      level: chain.risk?.risk_level ?? 5,
      confidence: chain.risk?.confidence ?? 0,
      degraded: chain.degradations.length > 0,
      isMock: chain.risk?.data_provenance?.is_mock ?? false,
      disclaimer: chain.risk?.data_provenance?.disclaimer ?? '',
      // 场景位置来自人工校准表；没校准就是 undefined，列表会标"未配置场景位置"
      anchor: anchorForRegion(region),
    }))
    .sort((a, b) => a.level - b.level || b.confidence - a.confidence || a.region.localeCompare(b.region))
})

/** 风险区域一览与静态地形共用这一份数据；选中 id 同时驱动列表高亮与图上标记高亮。 */
const selectedRiskId = ref<string | null>(null)

const sceneCells = computed<SceneCell[]>(() =>
  regionGrid.value.map((cell) => ({
    id: cell.region,
    region: cell.region,
    hazard: cell.hazard,
    level: cell.level,
    anchor: cell.anchor,
  })),
)

function selectRisk(id: string): void {
  selectedRiskId.value = id
  if (typeof document === 'undefined') return
  // 列表可能滚在卡内，点图上的标记要把它带到可视区；找不到就只高亮，不报错
  const row = document.querySelector<HTMLElement>(`[data-risk-id="${CSS.escape(id)}"]`)
  row?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
}

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
    // 趋势的"上一次"就是这趟出发前的快照：首屏为 null（没有差值可说），之后每趟滚动一份。
    prevReady.value = ready.value
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

/**
 * 智能体面板折叠 + 拉杆调高。
 * 列表原先照单全收，十几颗智能体把第一行撑到一屏多高，
 * 第二行（风险网格/链路/事件流）整个被顶出首屏；默认收进视口的约 55%，
 * 剩下的交给拉杆——值班员想多看几颗就往下拖，双击回到默认。
 */
const AGENT_PANEL_KEY = 'aegis.dashboard.agent-panel'
const AGENT_PANEL_MIN = 220
/** 折叠后只留标题行：16 上内边距 + 69 卡头 + 16 下内边距。 */
const AGENT_PANEL_COLLAPSED = 101
const AGENT_PANEL_MAX_RATIO = 0.8
const AGENT_PANEL_DEFAULT_RATIO = 0.55

function readAgentPanel(): { collapsed: boolean; height: number | null } {
  try {
    const raw = localStorage.getItem(AGENT_PANEL_KEY)
    if (raw !== null) {
      const saved = JSON.parse(raw) as { collapsed?: unknown; height?: unknown }
      return {
        collapsed: saved.collapsed === true,
        height: typeof saved.height === 'number' && Number.isFinite(saved.height) ? saved.height : null,
      }
    }
  } catch {
    // localStorage 不可用（隐私模式等）就落回默认高度，不让面板把页面一起拖垮
  }
  return { collapsed: false, height: null }
}

const agentPanelInit = readAgentPanel()
const panelRef = ref<HTMLElement | null>(null)
const panelCollapsed = ref(agentPanelInit.collapsed)
/** null = 跟随默认 55vh；拖过拉杆才写明确像素。 */
const panelHeight = ref<number | null>(agentPanelInit.height)
const viewportH = ref(window.innerHeight)

const panelMax = computed(() => Math.max(AGENT_PANEL_MIN, Math.round(viewportH.value * AGENT_PANEL_MAX_RATIO)))
/** 第二行（风险网格/链路/事件流）自然高度约 266，加顶栏与两处 gap 约 370：
    默认高度在 55vh 与"给第二行让位"之间取小，矮屏也保证第二行整行可见。 */
const SECOND_ROW_RESERVE = 370
const panelDefault = computed(() =>
  Math.min(
    860,
    Math.max(
      AGENT_PANEL_MIN,
      Math.min(Math.round(viewportH.value * AGENT_PANEL_DEFAULT_RATIO), viewportH.value - SECOND_ROW_RESERVE),
    ),
  ),
)
const panelNow = computed(() => (panelCollapsed.value ? AGENT_PANEL_COLLAPSED : (panelHeight.value ?? panelDefault.value)))
/** 默认不写死高度：交给网格与左侧主卡等高；只有用户拖过拉杆才记一个显式高度。 */
const panelStyle = computed(() => {
  if (panelCollapsed.value) return { height: `${AGENT_PANEL_COLLAPSED}px` }
  return panelHeight.value === null ? {} : { height: `${panelHeight.value}px` }
})

/** 折叠时标题行也要能回答"平台还活着吗"：在线数/总数，取不到写 — 不写 0。 */
const agentSummary = computed(() => {
  if (agents.value.length === 0) return '—'
  return `在线 ${agents.value.filter((a) => a.healthy).length} / ${agents.value.length}`
})

/** 点一行智能体看详情：右栏太窄装不下全部字段，抽屉里给全。 */
const selectedAgent = ref<AgentInfo | null>(null)
const agentDrawerOpen = ref(false)
function openAgent(agent: AgentInfo): void {
  selectedAgent.value = agent
  agentDrawerOpen.value = true
}

watch([panelCollapsed, panelHeight], () => {
  try {
    localStorage.setItem(AGENT_PANEL_KEY, JSON.stringify({ collapsed: panelCollapsed.value, height: panelHeight.value }))
  } catch {
    // 隐私模式写不进就算了，下次不记忆而已
  }
})

function clampPanel(h: number): number {
  return Math.min(panelMax.value, Math.max(AGENT_PANEL_MIN, Math.round(h)))
}

let dragStartY = 0
let dragStartH = 0
let dragLastY = 0
let dragRaf = 0
let dragging = false
/** pointermove 一帧能来好几发：逐发写高度就是一帧多次网格回流（实测改前拖动长帧 83ms）。
    合到 rAF 每帧直写一次 DOM，pointerup 才提交状态，Vue 与 localStorage 都只动一次。
    is-resizing 也走 classList 直改：它只关过渡/换手柄色，不值得在按下那一帧触发整页渲染。 */
function writeDragHeight(): void {
  dragRaf = 0
  const el = panelRef.value
  if (el === null) return
  const h = clampPanel(dragStartH + (dragLastY - dragStartY))
  el.style.height = `${h}px`
  el.querySelector('[data-testid="agent-panel-handle"]')?.setAttribute('aria-valuenow', String(h))
}
function onPanelHandleDown(e: PointerEvent): void {
  if (panelCollapsed.value) return
  ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
  dragStartY = e.clientY
  dragLastY = e.clientY
  dragStartH = panelNow.value
  dragging = true
  panelRef.value?.classList.add('is-resizing')
}
function onPanelHandleMove(e: PointerEvent): void {
  if (!dragging) return
  dragLastY = e.clientY
  if (dragRaf === 0) dragRaf = requestAnimationFrame(writeDragHeight)
}
function onPanelHandleUp(e: PointerEvent): void {
  if (!dragging) return
  dragging = false
  panelRef.value?.classList.remove('is-resizing')
  if (dragRaf !== 0) {
    cancelAnimationFrame(dragRaf)
    dragRaf = 0
  }
  // 没拖过（单纯点了一下）不写明确高度，保住"跟随默认 55vh"的语义
  if (dragLastY !== dragStartY) panelHeight.value = clampPanel(dragStartH + (dragLastY - dragStartY))
  const el = e.currentTarget as HTMLElement
  if (el.hasPointerCapture(e.pointerId)) el.releasePointerCapture(e.pointerId)
}
function onPanelHandleKey(e: KeyboardEvent): void {
  let next: number
  if (e.key === 'ArrowDown') next = panelNow.value + 24
  else if (e.key === 'ArrowUp') next = panelNow.value - 24
  else if (e.key === 'Home') next = AGENT_PANEL_MIN
  else if (e.key === 'End') next = panelMax.value
  else return
  e.preventDefault()
  panelCollapsed.value = false
  panelHeight.value = clampPanel(next)
}
function resetPanelHeight(): void {
  panelHeight.value = null
}
function onWindowResize(): void {
  viewportH.value = window.innerHeight
  if (panelHeight.value !== null) panelHeight.value = clampPanel(panelHeight.value)
}

onMounted(() => {
  void refresh()
  window.addEventListener('resize', onWindowResize)
  // 页签切到后台就不发请求（与腿面板同一口径）：大屏副页签没人看，轮询却一直在打接口。
  timer = setInterval(() => {
    if (document.visibilityState !== 'visible') return
    void refresh()
  }, REFRESH_MS)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', onWindowResize)
  if (dragRaf !== 0) cancelAnimationFrame(dragRaf)
  dragRaf = 0
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
    <!--
      指挥中心骨架（2026-11-20 裁决：三维地形是主视觉中心）。
      - 左列主区：KPI 横向数据卡一行 + 三维地形（复用现有 Cesium/GIS，不拿静态图顶替）；
      - 右列：智能体在线状态，纵跨 KPI + 地形两行，保留折叠与底缘拉杆调高；
      - 底部三张等高卡：风险区域一览 / 事件处置记录 / 实时动态，标题与内边距统一；
      - "可选子系统"跨整宽垫底。
      全部面板共用 .glass-panel（半透玻璃叠在波浪底上），波浪只在面板间隙露出，不各写一份装饰。
    -->
    <div class="dash-layout">
      <!-- 左侧：一张主卡装「标题 + 四指标 + 地形」，与右侧智能体面板同高。 -->
      <section class="glass-panel dash-main">
        <div class="dash-main__head">
          <PageHero
            title="态势总览"
            badge="实时"
            caption="值班第一屏：等人处理的待办、现场读数、风险区域与最新处置。"
          >
            <template #actions>
              <a-button type="primary" data-testid="dashboard-drill" @click="$router.push('/monitor')">发起演练</a-button>
              <span data-testid="dashboard-updated">
                {{ updatedAt ? `更新于 ${formatOperatingTime(updatedAt)}（UTC+8）` : '尚未取到数据' }}
              </span>
              <a-button size="small" :loading="loading" data-testid="dashboard-refresh" @click="refresh">刷 新</a-button>
            </template>
          </PageHero>
          <!-- 告警不再独占整行：收成一枚贴在标题旁的提示 -->
          <span v-if="urgentCards.length" class="dash-alert-chip" data-testid="dashboard-alert-chip">
            <span class="dash-alert-chip__dot" aria-hidden="true"></span>
            {{ urgentCards.length }} 条红色/橙色告警未确认
          </span>
        </div>

        <KpiStrip class="dash-kpi" :items="kpis" />

        <!-- 静态三维地形 + 动态风险标记（不再请求 Cesium/离线瓦片）：
             底图是艺术化高地切片，标记位置来自 terrainAnchors 的人工校准。 -->
        <section class="dash-map" data-testid="dashboard-map">
          <DashboardTerrainScene :cells="sceneCells" :selected-id="selectedRiskId" @select="selectRisk" />
        </section>
      </section>

      <!-- 第一行右：智能体在线状态。可折叠 + 底缘拉杆调高（默认按视口取高），
           点一行看详情。状态记忆在 localStorage。 -->
      <section
        ref="panelRef"
        class="glass-panel dash-agent"
        :class="{ 'is-collapsed': panelCollapsed }"
        :style="panelStyle"
        data-testid="agent-panel"
      >
        <a-card title="智能体在线状态" size="small">
          <template #extra>
            <span class="dash-agent__summary" data-testid="agent-panel-summary">{{ agentSummary }}</span>
            <button
              type="button"
              class="dash-agent__toggle"
              data-testid="agent-panel-toggle"
              :aria-expanded="!panelCollapsed"
              aria-controls="agent-panel-body"
              @click="panelCollapsed = !panelCollapsed"
            >
              <span class="dash-agent__chev" :class="{ 'is-collapsed': panelCollapsed }" aria-hidden="true">▾</span>
              {{ panelCollapsed ? '展开' : '收起' }}
            </button>
          </template>
          <div id="agent-panel-body" class="dash-agent__body" :aria-hidden="panelCollapsed" :tabindex="panelCollapsed ? -1 : 0">
            <AgentBoard :agents="agents" @select="openAgent" />
          </div>
        </a-card>
        <div
          v-show="!panelCollapsed"
          class="dash-agent__handle"
          data-testid="agent-panel-handle"
          role="slider"
          tabindex="0"
          aria-label="智能体面板高度：上下方向键调整，双击恢复默认"
          aria-orientation="vertical"
          :aria-valuemin="AGENT_PANEL_MIN"
          :aria-valuemax="panelMax"
          :aria-valuenow="panelNow"
          @pointerdown="onPanelHandleDown"
          @pointermove="onPanelHandleMove"
          @pointerup="onPanelHandleUp"
          @pointercancel="onPanelHandleUp"
          @dblclick="resetPanelHeight"
          @keydown="onPanelHandleKey"
        >
          <i class="dash-agent__grip" aria-hidden="true"></i>
        </div>
      </section>

      <!-- 底部三张等高卡：风险区域一览 / 事件处置记录 / 实时动态。
           三张同宽同高、标题与内边距统一（网格 align-items: stretch + 卡片 flex 撑满）。 -->
      <div class="dash-bottom">
        <section class="glass-panel dash-bottom__card">
          <a-card :title="gridTitle" size="small" :loading="loading">
            <RiskWall
            :cells="regionGrid"
            :window-size="CHAIN_WINDOW"
            :selected-id="selectedRiskId"
            @select="selectRisk"
          />
          </a-card>
        </section>
        <section class="glass-panel dash-bottom__card">
          <a-card :title="chainsTitle" size="small" :loading="loading">
            <ChainList :chains="recentChains" />
          </a-card>
        </section>
        <section class="glass-panel dash-bottom__card">
          <a-card title="实时动态" size="small">
            <EventFeed :events="events.slice(0, 20)" />
          </a-card>
        </section>
      </div>
    </div>

    <!-- “可选子系统”留在首屏之后：设计稿只要求三张业务卡在首屏内，这块仍可向下滚到 -->
    <section class="glass-panel dash-legs">
      <LegStatusPanel />
    </section>

    <!-- 智能体详情：右栏一行装不下 capabilities/hazard_types/版本等全部字段，点开看全。 -->
    <a-drawer
      v-model:open="agentDrawerOpen"
      title="智能体详情"
      placement="right"
      :width="380"
      data-testid="agent-detail-drawer"
    >
      <dl v-if="selectedAgent" class="agent-detail">
        <dt>编号</dt>
        <dd data-testid="agent-detail-id">{{ selectedAgent.agent_id }}</dd>
        <dt>类型</dt>
        <dd>{{ selectedAgent.agent_type }}</dd>
        <dt>版本</dt>
        <dd>{{ selectedAgent.version || '—' }}</dd>
        <dt>状态</dt>
        <dd>
          <span class="agent-detail__status" :class="{ 'is-live': selectedAgent.healthy }">
            {{ selectedAgent.healthy ? '在线' : '离线' }}
          </span>
        </dd>
        <dt>在途 / 上限</dt>
        <dd>{{ selectedAgent.inflight }} / {{ selectedAgent.max_concurrency }}</dd>
        <dt>成 · 败</dt>
        <dd>成 {{ selectedAgent.served }} · 败 {{ selectedAgent.failed }}</dd>
        <dt>空闲</dt>
        <dd>{{ Math.round(selectedAgent.idle_seconds) }} 秒</dd>
        <dt>能力</dt>
        <dd>{{ selectedAgent.capabilities.length > 0 ? selectedAgent.capabilities.join('、') : '未声明' }}</dd>
        <dt>灾种</dt>
        <dd>{{ selectedAgent.hazard_types.length > 0 ? selectedAgent.hazard_types.join('、') : '未声明' }}</dd>
      </dl>
    </a-drawer>
  </div>
</template>

<style scoped>
/* KPI 托盘（.stat-cell）与空态（.empty-hero）的样式在 styles/theme.css：两页共用一份。 */

/* 页头并进第一块玻璃卡：仍然用统一的 PageHero（uiTheme 门禁要求常规页面必须接它），
   只是把宿主从页面顶部挪到 KpiStrip 的 header 插槽里，所以只吃掉它自带的下外边距。 */
.dash-hero {
  width: 100%;
}
.dash-hero :deep(.page-head) {
  margin-bottom: 0;
}
/* 值班台的更新时间会随真实数据变长：窄屏（900 档）再把标题挤成两行就不像页头了。
   让动作区整体换行，标题保持单行；≥1600 的目标档位不会触发换行。 */
.dash-hero :deep(.page-head__row) {
  flex-wrap: wrap;
  row-gap: var(--aeg-space-s2);
}
/* KPI 卡里的页头标题与其它卡片标题同档：20px / 700（PageHero 默认 600） */
.dash-hero :deep(.page-head__title) {
  white-space: nowrap;
  font-size: var(--aeg-font-lg);
  font-weight: var(--aeg-weight-bold);
}

/* 指挥中心骨架（2026-11-20 裁决：三维地形是主视觉中心）：
   左列主区 KPI 一行 + 三维地形（纵跨两行）；
   右列智能体在线状态，与 KPI + 地形同一行高（保留折叠/拉杆）；
   底部三张等高卡跨两列（风险区域 / 事件处置 / 实时动态）；
   "可选子系统"跨整宽垫底。
   用 fr 而不是百分比：带 gap 的百分比会把一行撑破。 */
.dash-layout {
  display: grid;
  grid-template-columns: minmax(0, 67fr) minmax(0, 33fr);
  grid-template-areas:
    'main agent'
    'bottom bottom';
  gap: var(--aeg-space-s4);
  align-items: stretch;
  min-width: 0;
  /* 主区行高固定 390–470（1600 下 ~405）：不跟智能体数量走，地图在其中伸缩 */
  --dash-bottom-h: 210px;
  height: auto;
  grid-template-rows: clamp(390px, 45vh, 470px) auto;
}
.dash-layout:has(.chain, .evt, .cell) {
  --dash-bottom-h: clamp(240px, 26vh, 300px);
}
/* 左侧一张主卡：标题 + 四指标 + 地形，纵跨上半区；右侧智能体面板同高 */
.dash-main {
  grid-area: main;
  display: flex;
  flex-direction: column;
  gap: var(--aeg-space-s2);
  min-width: 0;
  min-height: 0;
  padding: var(--aeg-space-s3) var(--aeg-space-s3) 0;
  overflow: hidden;
}
/* 覆盖 .glass-panel 的 16px 内边距：地图要直接铺到面板最底端，不留底边和底边框 */
.dash-main.glass-panel {
  padding: var(--aeg-space-s3) var(--aeg-space-s3) 0;
  border-bottom: none;
}
.dash-main__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: var(--aeg-space-s3);
}
.dash-main :deep(.page-head) {
  margin-bottom: 0;
}
/* 指标托盘并进主卡：去掉自带玻璃底，四张小卡直接落在主卡上 */
.dash-main .dash-kpi {
  margin: 0;
  padding: 0;
  border: none;
  border-radius: 0;
  background: transparent;
  box-shadow: none;
}
/* 数值在上、指标名在下：留 6px 呼吸，标签降一档字号，别和数值的 descender 挤在一起 */
.dash-main .dash-kpi :deep(.kpi__main) {
  gap: 4px;
}
.dash-main .dash-kpi :deep(.kpi__label) {
  font-size: 12px;
}
/* 四张指标卡：一行四列等宽；高度 76、图标 38、数字 26（25–28 区间） */
.dash-main .dash-kpi :deep(.kpi-strip__cards) {
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}
.dash-main .dash-kpi :deep(.kpi) {
  height: 76px;
  padding: 10px 12px;
  gap: 12px;
  border-radius: var(--aeg-radius-md);
}
.dash-main .dash-kpi :deep(.kpi__icon) {
  width: 38px;
  height: 38px;
}
/* 图标语义色：在线青 / 预警橙 / 任务蓝 / 成功率绿；卡片底色保持统一深蓝 */
.dash-main .dash-kpi :deep(.kpi:nth-child(2) .kpi__icon) {
  color: var(--aeg-color-status-warning);
}
.dash-main .dash-kpi :deep(.kpi:nth-child(3) .kpi__icon) {
  color: var(--aeg-color-brand-alt);
}
.dash-main .dash-kpi :deep(.kpi:nth-child(4) .kpi__icon) {
  color: var(--aeg-color-status-success);
}
.dash-main .dash-kpi :deep(.kpi__value) {
  font-size: 26px;
}
.dash-main .dash-kpi :deep(.kpi__suffix) {
  font-size: 12px;
}
.dash-main .dash-kpi :deep(.kpi__trend) {
  font-size: 11px;
}
.dash-main .dash-kpi :deep(.kpi__note) {
  font-size: 11px;
  line-height: var(--aeg-lh-tight);
}
/* 紧凑标题栏：标题 21、说明 12px、按钮 36，整体约 60 */
.dash-main :deep(.page-head) {
  margin-bottom: 0;
}
.dash-main :deep(.page-head__title) {
  font-size: 21px;
}
.dash-main :deep(.page-head__caption) {
  margin-top: 2px;
  font-size: var(--aeg-font-xs);
  max-width: none;
}
.dash-main :deep(.page-head__actions .ant-btn) {
  min-height: 36px;
}
.dash-main :deep(.page-head__actions) {
  font-size: var(--aeg-font-sm);
}
/* 地形直接铺在主面板下半部：去掉子卡片边框/圆角/背景，左右贴边 */
.dash-main .dash-map {
  flex: 1 1 auto;
  min-height: 210px;
  max-height: 290px;
  min-width: 0;
  margin: 0 calc(-1 * var(--aeg-space-s3));
  overflow: hidden;
  border: none;
  border-radius: 0;
  background: transparent;
}
.dash-main .dash-map > * {
  height: 100%;
}
/* 告警提示：贴在标题旁的一枚小胶囊，不再独占一整行 */
.dash-alert-chip {
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 6px;
  height: 26px;
  margin-top: 2px;
  padding: 0 10px;
  border: 1px solid color-mix(in srgb, var(--aeg-color-status-danger) 62%, transparent);
  border-radius: var(--aeg-radius-md);
  background: color-mix(in srgb, var(--aeg-color-status-danger) 18%, var(--aeg-color-page));
  color: color-mix(in srgb, var(--aeg-color-status-danger) 62%, var(--aeg-color-text));
  font-size: var(--aeg-font-xs);
}
.dash-alert-chip__dot {
  width: 6px;
  height: 6px;
  border-radius: var(--aeg-radius-pill);
  background: var(--aeg-color-status-danger);
}
.dash-layout > .dash-agent {
  grid-area: agent;
  min-width: 0;
  overflow: hidden;
  /* 默认与左侧主卡等高；用户拖过底缘拉杆后才写死高度 */
  align-self: stretch;
  display: flex;
  flex-direction: column;
  transition: height var(--aeg-motion-slow) ease;
}
/* 拖拽期间必须跟手：过渡会让手柄和面板差半拍 */
.dash-agent.is-resizing {
  transition: none;
}
.dash-agent :deep(.ant-card) {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.dash-agent :deep(.ant-card-body) {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
/* 列表在卡内滚动：底部渐隐 + 细滚动条，与链路/事件流同一套习语 */
.dash-agent__body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding-top: var(--aeg-space-s2);
  -webkit-mask-image: linear-gradient(to bottom, black calc(100% - 22px), transparent);
  mask-image: linear-gradient(to bottom, black calc(100% - 22px), transparent);
}
.dash-agent__summary {
  color: var(--aeg-color-status-success);
  font-weight: var(--aeg-weight-semibold);
  font-variant-numeric: tabular-nums;
}
/* 统一细滚动条：4–6px，低亮度，hover 稍微提亮 */
.dash-agent__body::-webkit-scrollbar,
.dash-bottom__card :deep(.ant-card-body)::-webkit-scrollbar,
.dash-bottom__card :deep(.ant-card-body *)::-webkit-scrollbar {
  width: 5px;
  height: 5px;
}
.dash-agent__body::-webkit-scrollbar-thumb,
.dash-bottom__card :deep(.ant-card-body)::-webkit-scrollbar-thumb,
.dash-bottom__card :deep(.ant-card-body *)::-webkit-scrollbar-thumb {
  border-radius: var(--aeg-radius-pill);
  background: color-mix(in srgb, var(--aeg-color-border-strong) 55%, transparent);
}
.dash-agent__body:hover::-webkit-scrollbar-thumb,
.dash-bottom__card :deep(.ant-card-body):hover::-webkit-scrollbar-thumb {
  background: var(--aeg-color-border-strong);
}
.dash-agent__body::-webkit-scrollbar {
  width: 6px;
}
.dash-agent__body::-webkit-scrollbar-thumb {
  border-radius: var(--aeg-radius-pill);
  background: var(--aeg-color-border);
}
/* 折叠是外层高度动画，正文用 visibility 藏：保留滚动位置，展开接着看 */
.dash-agent.is-collapsed .dash-agent__body {
  visibility: hidden;
}
.dash-agent__summary {
  margin-right: var(--aeg-space-s2);
  font-size: var(--aeg-font-sm);
  font-variant-numeric: tabular-nums;
  color: var(--aeg-color-text-secondary);
}
.dash-agent__toggle {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 0;
  border: none;
  background: transparent;
  color: var(--aeg-color-brand);
  font-size: var(--aeg-font-sm);
  min-height: 24px;
  cursor: pointer;
}
.dash-agent__toggle:hover {
  text-decoration: underline;
}
.dash-agent__chev {
  display: inline-block;
  transition: transform var(--aeg-motion-base) ease;
}
.dash-agent__chev.is-collapsed {
  transform: rotate(-90deg);
}
/* 拉杆：底缘一条水平手柄，拖拽调高；悬停/拖拽时品牌色 + 光晕提示"可以拖" */
.dash-agent__handle {
  flex: none;
  display: flex;
  align-items: center;
  justify-content: center;
  height: 14px;
  margin-top: var(--aeg-space-s1);
  border-radius: var(--aeg-radius-pill);
  cursor: row-resize;
  touch-action: none;
}
.dash-agent__handle:focus-visible {
  outline: 2px solid var(--aeg-color-brand);
  outline-offset: 2px;
}
.dash-agent__grip {
  width: 56px;
  height: 4px;
  border-radius: var(--aeg-radius-pill);
  background: var(--aeg-color-border-strong);
  transition:
    background var(--aeg-motion-base) ease,
    box-shadow var(--aeg-motion-base) ease;
}
.dash-agent__handle:hover .dash-agent__grip,
.dash-agent.is-resizing .dash-agent__grip {
  background: var(--aeg-color-brand);
  box-shadow: 0 0 0 3px var(--aeg-color-brand-soft);
}
.dash-bottom {
  grid-area: bottom;
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: var(--aeg-space-s4);
  align-items: stretch;
  min-width: 0;
  /* 高度由布局变量统一给：空态 210，出现真实行后 240–300；行高与主区互补，首屏始终装得下 */
  height: var(--dash-bottom-h);
  overflow: hidden;
}
/* 三张卡同高：网格拉伸 + 卡片 flex 列，正文区各自滚动，标题顶边与分隔线齐平。 */
.dash-bottom__card {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
}
.dash-bottom__card :deep(.ant-card) {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.dash-bottom__card :deep(.ant-card-body) {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}
.dash-layout > .dash-legs {
  grid-area: legs;
}
/* 首屏之外的可选子系统：与三卡的间距对齐“主图 ↔ 三卡”的视觉间隙
   （主卡内边距 s3 + 网格 gap s4 + 卡片边框 ≈ 32–33px），不再贴着三卡 */
.dash-legs {
  position: relative;
  z-index: 1;
  margin-top: calc(var(--aeg-space-s5) + var(--aeg-space-s3));
}
/* 37% 列比原来的 340px 宽：agent id 放宽，能力标签仍让位（保留"一行读得完"）。
   一行装不下时让 id 省略而不是整块换行：换行会把每颗智能体撑成两行，12 颗只露 6 行。 */
.dash-agent :deep(.agent__id) {
  flex: 1 1 auto;
  min-width: 0;
  max-width: none;
}
.dash-agent :deep(.agent__caps) {
  display: none;
}
/* 类型徽标按设计稿保留，不再隐藏 */
.dash-agent :deep(.agent__loadbar) {
  width: 32px;
}
.dash-agent :deep(.agent__served) {
  min-width: 0;
  padding: 1px 6px;
}
/* 行内五段信息压到 4px 间距：mock 台账里 feedback.mock01/02 只差尾号，id 必须尽量读全。 */
.dash-agent :deep(.agent) {
  gap: var(--aeg-space-s1);
}
@media (max-width: 1279px) {
  .dash-layout {
    grid-template-columns: minmax(0, 1fr);
    grid-template-areas:
      'kpi'
      'map'
      'agent'
      'bottom'
      'legs';
  }
  .dash-layout > .dash-map {
    height: clamp(320px, 46vh, 480px);
  }
}
@media (max-width: 1100px) {
  .dash-bottom {
    grid-template-columns: minmax(0, 1fr);
  }
}

/* 面板卡：半透玻璃叠在波浪底上（2026-11-20 指挥中心裁决：暗蓝面板浮于波浪背景），
   细描边保证边缘可见。里层 antd 卡片的 chrome 全部让位，只留标题的字距与小号字。
   所有面板共用这一个类。 */
.glass-panel {
  padding: var(--aeg-space-s4);
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-lg);
  background: var(--aeg-color-glass-bg);
  box-shadow: var(--aeg-shadow-md);
  /* 独立合成层：滚动时整块搬已栅格化的纹理，不重 raster 整页内容层。 */
  transform: translateZ(0);
}
/* 业务卡提高不透明度：深蓝波浪只做背景，不穿透正文（对比度优先） */
.dash-bottom__card.glass-panel {
  border-color: color-mix(in srgb, var(--aeg-color-border) 80%, transparent);
  background: color-mix(in srgb, var(--aeg-color-page) 94%, transparent);
}
.dash-bottom__card.glass-panel :deep(.ant-card-head) {
  border-bottom: 1px solid var(--aeg-color-border-subtle);
}
/* 底部卡内空态收小：不再用整张大卡占满，留一句说明 + 次级出口 */
.dash-bottom__card :deep(.empty-hero) {
  gap: var(--aeg-space-s2);
  padding: var(--aeg-space-s3);
}
.dash-bottom__card :deep(.empty-hero__icon) {
  width: 32px;
  height: 32px;
  font-size: 14px;
}
.dash-bottom__card :deep(.empty-hero__title) {
  font-size: 14px;
}
.dash-bottom__card :deep(.empty-hero__desc) {
  font-size: 12px;
}
.dash-bottom__card :deep(.empty-hero__action) {
  min-height: 28px;
  padding: 0 10px;
  font-size: 12px;
}
.glass-panel :deep(.ant-card) {
  background: transparent;
  border: none;
  box-shadow: none;
}
.glass-panel :deep(.ant-card-head) {
  /* 标题顶到卡头第一条线（不再被 antd 垂直居中）：与 KPI 卡页头标题同一水平 */
  /* 卡头固定成"两行标题"的高度：单行标题的卡（事件流/智能体）也留出第二行，
     这样同一行卡片的标题顶边与分隔线都齐平；4 + 56 + 8 + 1 = 69 */
  min-height: 69px;
  padding: 4px 0 var(--aeg-space-s2);
  border-bottom: 1px solid var(--aeg-color-border);
  background: transparent;
  align-items: flex-start;
  /* antd 的卡头是 flex-column + 垂直居中；改成靠上，标题才和 KPI 卡页头同一水平 */
  justify-content: flex-start;
}
.glass-panel :deep(.ant-card-head-wrapper) {
  align-items: flex-start;
}
.glass-panel :deep(.ant-card-head-title) {
  padding: 0;
  color: var(--aeg-color-text);
  /* 底部三卡标题统一 19px，与正文层级拉开 */
  font-size: 19px;
  font-weight: var(--aeg-weight-bold);
  letter-spacing: 0.02em;
  /* 标题加重之后会更长：允许折行，别用 antd 默认的省略号把"感知→研判→决策"截掉 */
  white-space: normal;
  overflow: visible;
  text-overflow: clip;
  line-height: var(--aeg-lh-snug);
}
.glass-panel :deep(.ant-card-body) {
  padding: 0;
  background: transparent;
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

/* 智能体详情抽屉：字段名在左、值在右，值可折行（能力/灾种可能很长）。 */
.agent-detail {
  display: grid;
  grid-template-columns: 88px minmax(0, 1fr);
  gap: var(--aeg-space-s2) var(--aeg-space-s3);
  margin: 0;
  font-size: var(--aeg-font-sm);
}
.agent-detail dt {
  color: var(--aeg-color-text-secondary);
}
.agent-detail dd {
  margin: 0;
  overflow-wrap: anywhere;
  color: var(--aeg-color-text);
}
.agent-detail__status {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  color: var(--aeg-color-text-muted);
}
.agent-detail__status::before {
  content: '';
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--aeg-color-text-muted);
}
.agent-detail__status.is-live {
  color: var(--aeg-color-status-success);
}
.agent-detail__status.is-live::before {
  background: var(--aeg-color-status-success);
  box-shadow: 0 0 0 3px var(--aeg-color-brand-soft);
}
</style>
