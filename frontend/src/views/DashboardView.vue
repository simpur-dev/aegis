<script setup lang="ts">
import { message } from 'ant-design-vue'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import api from '@/api/client'
import type { AgentInfo, ChainSummary, LatencyReport, WarningRecord } from '@/api/types'
import { HAZARD_LABELS, RISK_COLORS, RISK_LABELS } from '@/api/types'
import LegStatusPanel from '@/components/system/LegStatusPanel.vue'
import { useEventStream } from '@/composables/useEventStream'
import { formatOperatingTime } from '@/utils/clock'

const REFRESH_MS = 15_000

const ready = ref<{ agents_online: number; store: Record<string, number> } | null>(null)
const agents = ref<AgentInfo[]>([])
const warnings = ref<WarningRecord[]>([])
const chains = ref<ChainSummary[]>([])
const latency = ref<LatencyReport | null>(null)
const loading = ref(false)
/** 这页的数字是什么时候取的。没有它，"预警 3 条"到底是"只有 3 条"还是"页面 20 分钟没动"就无从判断。 */
const updatedAt = ref<string | null>(null)
const refreshError = ref<string | null>(null)
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
const kpis = computed(() => [
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
    suffix: latency.value?.collaboration.pass ? '达标' : '样本不足',
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

const regionGrid = computed(() => {
  const byRegion = new Map<string, ChainSummary>()
  for (const chain of chains.value) {
    const region = chain.risk?.region_code ?? chain.task_units[0]?.slice(4, 10) ?? '未知区域'
    const previous = byRegion.get(region)
    if (!previous || (chain.risk && previous.risk && chain.risk.risk_level < previous.risk.risk_level)) {
      byRegion.set(region, chain)
    }
  }
  return [...byRegion.entries()].map(([region, chain]) => ({
    region,
    hazard: chain.risk ? HAZARD_LABELS[chain.risk.hazard_type] : '无风险',
    level: chain.risk?.risk_level ?? 5,
    confidence: chain.risk?.confidence ?? 0,
    degraded: chain.degradations.length > 0,
  }))
})

async function refresh(): Promise<void> {
  loading.value = true
  const seq = ++chainsSeq
  try {
    const [readyInfo, agentInfo, warningList, eventList, latencyReport] = await Promise.all([
      api.ready(),
      api.agents(),
      api.warnings({ limit: 50 }),
      api.events(20),
      api.latency(),
    ])
    ready.value = { agents_online: readyInfo.agents_online, store: readyInfo.store }
    agents.value = agentInfo.items
    warnings.value = warningList.items
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
          const list = await api.events(20)
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

const stageColumns = [
  { title: '事件', dataIndex: 'trace_id', key: 'trace_id', ellipsis: true },
  { title: '结果', key: 'result', width: 120 },
  { title: '各段执行方式', key: 'stages' },
  { title: '任务', dataIndex: 'task_units', key: 'task_units', customRender: ({ text }: { text: string[] }) => `${text.length} 个` },
]

const agentColumns = [
  { title: '智能体', dataIndex: 'agent_id', key: 'agent_id' },
  { title: '类型', dataIndex: 'agent_type', key: 'agent_type' },
  { title: '能力', dataIndex: 'capabilities', key: 'capabilities', customRender: ({ text }: { text: string[] }) => text.join(', ') },
  { title: '状态', key: 'healthy' },
  { title: '在途/上限', key: 'load' },
]
</script>

<template>
  <div>
    <div class="dash-bar" data-testid="dashboard-bar">
      <span class="dash-stamp" data-testid="dashboard-updated">
        {{ updatedAt ? `更新于 ${formatOperatingTime(updatedAt)}（UTC+8）` : '尚未取到数据' }}
      </span>
      <a-button size="small" :loading="loading" data-testid="dashboard-refresh" @click="refresh">刷 新</a-button>
    </div>
    <a-alert
      v-if="refreshError"
      type="warning"
      show-icon
      :message="`最近一次取数失败：${refreshError}（页面显示的是上一次成功取到的数字）`"
      data-testid="dashboard-error"
      class="dash-error"
    />
    <a-row :gutter="12">
      <a-col v-for="kpi in kpis" :key="kpi.label" :span="6">
        <a-card size="small">
          <a-statistic :title="kpi.label" :value="kpi.value" :suffix="kpi.suffix" />
        </a-card>
      </a-col>
    </a-row>

    <a-row :gutter="12" style="margin-top: 12px">
      <a-col :span="10">
        <a-card title="风险区域网格" size="small" :loading="loading">
          <div class="grid">
            <div v-for="cell in regionGrid" :key="cell.region" class="cell" :style="{ borderColor: RISK_COLORS[cell.level] }">
              <div class="region">{{ cell.region }}</div>
              <div class="hazard">{{ cell.hazard }}</div>
              <a-tag :color="RISK_COLORS[cell.level]">{{ RISK_LABELS[cell.level] }}</a-tag>
              <div v-if="cell.degraded" class="degraded">降级：平台规则兜底</div>
            </div>
            <a-empty v-if="!regionGrid.length" description="暂无研判结论，可在监测页发起演练" />
          </div>
        </a-card>
      </a-col>

      <a-col :span="14">
        <a-card title="链路执行记录（最新在上；感知→研判→决策→执行→反馈）" size="small" :loading="loading">
          <a-table :columns="stageColumns" :data-source="recentChains" :pagination="{ pageSize: 6 }" row-key="trace_id" size="small">
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'result'">
                <a-tag :color="record.errors.length ? 'red' : record.ok ? 'green' : 'orange'">
                  {{ record.errors.length ? '失败' : record.ok ? '成功' : '进行中' }}
                </a-tag>
                <a-tag v-if="!record.acted">未触发</a-tag>
              </template>
              <template v-else-if="column.key === 'stages'">
                <a-tag v-for="stage in record.stages" :key="stage.name" :color="stage.mode === 'agent' ? 'blue' : 'default'">
                  {{ stage.name }}:{{ stage.mode }}
                </a-tag>
              </template>
            </template>
          </a-table>
        </a-card>
      </a-col>
    </a-row>

    <a-row :gutter="12" style="margin-top: 12px">
      <a-col :span="14">
        <a-card title="智能体在线状态（经总线契约注册）" size="small">
          <a-table :columns="agentColumns" :data-source="agents" :pagination="false" row-key="agent_id" size="small">
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'healthy'">
                <a-tag :color="record.healthy ? 'green' : 'red'">{{ record.healthy ? '在线' : '失联' }}</a-tag>
              </template>
              <template v-else-if="column.key === 'load'">{{ record.inflight }} / {{ record.max_concurrency }}</template>
            </template>
          </a-table>
        </a-card>
      </a-col>
      <a-col :span="10">
        <a-card title="实时事件流（SSE）" size="small">
          <a-timeline>
            <a-timeline-item v-for="event in events.slice(0, 8)" :key="event.trace_id + event.ts" color="blue">
              <div>{{ event.payload.title ?? event.payload.warning_id ?? event.subject }}</div>
              <small>{{ formatOperatingTime(event.ts) }}（UTC+8）</small>
            </a-timeline-item>
            <a-empty v-if="!events.length" description="暂无事件" />
          </a-timeline>
        </a-card>
      </a-col>
    </a-row>

    <a-row style="margin-top: 12px">
      <a-col :span="24">
        <!-- 垫在明细之后：KPI 与链路要先占住首屏，但这块必须和它们在同一页——
             判"预案为什么变慢"时翻的是这个，不是日志。 -->
        <LegStatusPanel />
      </a-col>
    </a-row>
  </div>
</template>

<style scoped>
.dash-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
}
.dash-stamp {
  color: rgba(0, 0, 0, 0.55);
  font-size: 12px;
}
.dash-error {
  margin-bottom: 12px;
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 10px;
}
.cell {
  border: 2px solid #d9d9d9;
  border-radius: 6px;
  padding: 10px;
  background: #fff;
}
.region {
  font-weight: 600;
}
.hazard {
  font-size: 12px;
  color: #595959;
  margin-bottom: 4px;
}
.degraded {
  margin-top: 6px;
  font-size: 11px;
  color: #fa8c16;
}
</style>
