<script setup lang="ts">
import {
  AlertOutlined, BarChartOutlined, BellOutlined, DatabaseOutlined, FilterOutlined, InboxOutlined,
  PlayCircleOutlined, ReloadOutlined, RightOutlined, SearchOutlined, SendOutlined, UnorderedListOutlined,
} from '@ant-design/icons-vue'
import { useMediaQuery } from '@vueuse/core'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import api from '@/api/client'
import { fetchIntegrations } from '@/api/integrations'
import type { HazardType, RiskLevel, TaskUnit, WarningRecord } from '@/api/types'
import { HAZARD_LABELS, RISK_COLORS, RISK_LABELS } from '@/api/types'
import PageHero from '@/components/PageHero.vue'
import FreshnessBar from '@/components/FreshnessBar.vue'
import { useAlerts } from '@/composables/useAlerts'
import { attachFocusTrap, type FocusTrapHandle } from '@/composables/useFocusTrap'
import { formatOperatingTime } from '@/utils/clock'

const REFRESH_MS = 15_000
/**
 * 详情里回查链路时取多少条最近链路。写死一处，"找不到"的提示才与实取窗口一致。
 */
const EVENT_WINDOW = 200
/** 列表取多少条预警。也写死一处，"这只是最近 N 条"那句话才与实取窗口一致。 */
const WARNING_LIST_LIMIT = 100
const PAGE_SIZE = 10
const compact = useMediaQuery('(max-width: 720px)')
const riskLevels = [1, 2, 3, 4, 5] as const

const warnings = ref<WarningRecord[]>([])
const loading = ref(false)
const loadError = ref('')
const search = ref('')
const levelFilter = ref<RiskLevel | ''>('')
const hazardFilter = ref<HazardType | ''>('')
const regionFilter = ref('')
const channelFilter = ref('')
const tablePage = ref(1)
const open = ref(false)
const current = ref<WarningRecord | null>(null)
const currentTasks = ref<TaskUnit[]>([])
const tasksLoading = ref(false)
const tasksError = ref<string | null>(null)
/** 触达通道的形态（mock / http / null=没读到），来自状态面那条 delivery 腿。 */
const deliveryMode = ref<string | null>(null)
/** 服务端一共多少条预警（`/readyz` 的台账数）；读不到就是 null，此时只报窗口不报总数。 */
const storeTotal = ref<number | null>(null)
const taskTotal = ref<number | null>(null)
const chainTotal = ref<number | null>(null)
/** 最近一次**成功**取数的时刻（UTC ISO）；null = 还没取到过。 */
const fetchedAt = ref<string | null>(null)
const { urgentCards, lastSyncAt } = useAlerts()
let timer: ReturnType<typeof setInterval> | null = null
/**
 * 第几次点开详情。抽屉里同时挂着"这条预警的正文"和"它的任务单元"，
 * 而任务单元要回查最近 200 条链路才能算出来——真机量到过：先点的那条回查慢了 2.5 秒，
 * 人在这期间点了另一条，于是抽屉挂着 B 的标题、表里是 B 的 4 个任务单元，
 * 顶上却写着 A 那句"最近 200 条链路里没有这条预警的执行记录"。
 */
let inspectSeq = 0

const reachTitle = computed(() => (deliveryMode.value === 'mock' ? '触达（演练口径）' : '触达'))
const hasSnapshot = computed(() => fetchedAt.value !== null)

/** 新鲜度条左端：台账总数（读不到就说读不到）与自刷周期。 */
const freshLabel = computed<string>(() =>
  storeTotal.value === null ? '台账总数未读到 ｜ 每 15 秒自刷' : `台账 ${storeTotal.value} 条 ｜ 每 15 秒自刷`,
)

const windowLabel = computed(() => hasSnapshot.value ? `最近 ${warnings.value.length} 条` : '最近预警')
const windowSummary = computed(() => {
  const levels = { red: 0, orange: 0, other: 0 }
  const hazards = new Map<HazardType, number>()
  const regions = new Set<string>()
  const channels = new Set<string>()
  let reached = 0
  for (const warning of warnings.value) {
    if (warning.risk_level === 1) levels.red += 1
    else if (warning.risk_level === 2) levels.orange += 1
    else levels.other += 1
    hazards.set(warning.hazard_type, (hazards.get(warning.hazard_type) ?? 0) + 1)
    warning.region_codes.forEach((region) => regions.add(region))
    warning.channels.forEach((channel) => channels.add(channel))
    if (warning.deliveries.some(isDelivered)) reached += 1
  }
  return {
    levels,
    reached,
    hazards: [...hazards].sort(([a, aCount], [b, bCount]) => bCount - aCount || HAZARD_LABELS[a].localeCompare(HAZARD_LABELS[b], 'zh-CN')),
    regions: [...regions].sort(),
    channels: [...channels].sort(),
  }
})
const severityBreakdown = computed(() => [
  { label: '红色预警', count: windowSummary.value.levels.red, tone: 'danger' },
  { label: '橙色预警', count: windowSummary.value.levels.orange, tone: 'warning' },
  { label: '其他预警', count: windowSummary.value.levels.other, tone: 'info' },
])
const regionOptions = computed(() => [...new Set([...windowSummary.value.regions, regionFilter.value])].filter(Boolean).sort())
const channelOptions = computed(() => [...new Set([...windowSummary.value.channels, channelFilter.value])].filter(Boolean).sort())
const metrics = computed(() => [
  { label: '台账预警', value: storeTotal.value, detail: '全部在册记录', icon: DatabaseOutlined, tone: 'info' },
  { label: '红色预警', value: hasSnapshot.value ? windowSummary.value.levels.red : null, detail: windowLabel.value, icon: BellOutlined, tone: 'danger' },
  { label: '橙色预警', value: hasSnapshot.value ? windowSummary.value.levels.orange : null, detail: windowLabel.value, icon: BellOutlined, tone: 'warning' },
  { label: '待确认', value: lastSyncAt.value || urgentCards.value.length ? urgentCards.value.length : null, detail: '红色 / 橙色告警', icon: InboxOutlined, tone: 'info' },
  {
    label: deliveryMode.value === 'mock' ? '已触达（演练）' : deliveryMode.value ? '已触达' : '有成功回执',
    value: hasSnapshot.value ? windowSummary.value.reached : null,
    detail: windowLabel.value,
    icon: SendOutlined,
    tone: 'accent',
  },
])

const columns = computed(() => [
  { title: '预警标题', dataIndex: 'title_zh', key: 'title_zh' },
  { title: '灾种', key: 'hazard' },
  { title: '等级', key: 'level' },
  { title: '区域', key: 'regions' },
  { title: '通道', key: 'channels' },
  { title: reachTitle.value, key: 'reach' },
  { title: '生成时间（UTC+8，最新在上）', dataIndex: 'generated_at', key: 'generated_at' },
  { title: '操作', key: 'action' },
])

/**
 * 接口按"最近 N 条、旧→新"给（后端 `BoundedCollection.latest()` 的口径），照原样摆进这张表
 * 就是把刚发出去的预警压到最后一页。真机量过：29 条、每页 10 条，第 1 页是 00:22:40 → 01:09:11，
 * 而刚发布那条（01:14:34）在第 3 页——这张页的名字就叫"预警发布与靶向触达"。
 * 按时间倒序自己排，不依赖后端给的顺序。
 */
const orderedWarnings = computed<WarningRecord[]>(() =>
  [...warnings.value].sort((a, b) => (a.generated_at < b.generated_at ? 1 : a.generated_at > b.generated_at ? -1 : 0)),
)
const visibleWarnings = computed(() => {
  const query = search.value.trim().toLowerCase()
  return orderedWarnings.value.filter((warning) =>
    (levelFilter.value === '' || warning.risk_level === Number(levelFilter.value)) &&
    (!hazardFilter.value || warning.hazard_type === hazardFilter.value) &&
    (!regionFilter.value || warning.region_codes.includes(regionFilter.value)) &&
    (!channelFilter.value || warning.channels.includes(channelFilter.value)) &&
    (!query || [warning.warning_id, warning.title_zh, HAZARD_LABELS[warning.hazard_type], ...warning.region_codes]
      .some((text) => text?.toLowerCase().includes(query))),
  )
})
const mobileWarnings = computed(() => visibleWarnings.value.slice((tablePage.value - 1) * PAGE_SIZE, tablePage.value * PAGE_SIZE))
const filtersActive = computed(() => Boolean(search.value.trim() || levelFilter.value !== '' || hazardFilter.value || regionFilter.value || channelFilter.value))
const emptyTitle = computed(() => loading.value ? '正在读取预警' : loadError.value ? '暂时无法读取预警' : filtersActive.value ? '未找到符合条件的预警' : '还没有预警')
const deliveryLabels = { pending: '待投递', delivered: '已送达', failed: '投递失败', retried: '重试送达' }
watch([search, levelFilter, hazardFilter, regionFilter, channelFilter], () => { tablePage.value = 1 })
watch(visibleWarnings, (rows) => { tablePage.value = Math.min(tablePage.value, Math.max(1, Math.ceil(rows.length / PAGE_SIZE))) })

function resetFilters(): void {
  search.value = ''
  levelFilter.value = ''
  hazardFilter.value = ''
  regionFilter.value = ''
  channelFilter.value = ''
}

/**
 * 取满窗口 = 有预警在这一页之外。真机把这台进程演练到 113 条时量到：列表取 100 条，
 * 页面上"窗口/最近/上限"一个字都没有，6 个页码按钮看起来就是"所有预警"——
 * 13 条预警等于凭空消失。
 */
const windowNote = computed<string>(() => {
  if (warnings.value.length < WARNING_LIST_LIMIT) return ''
  const total = storeTotal.value
  if (total === null) return `已加载最近 ${WARNING_LIST_LIMIT} 条，台账总数暂未取得。`
  if (total <= WARNING_LIST_LIMIT) return ''
  return `已加载最近 ${WARNING_LIST_LIMIT} 条，台账共 ${total} 条。更早的 ${total - WARNING_LIST_LIMIT} 条未加载。`
})

async function load(): Promise<void> {
  if (loading.value) return
  loading.value = true
  try {
    const [data, legs] = await Promise.all([api.warnings({ limit: WARNING_LIST_LIMIT }), fetchIntegrations().catch(() => null)])
    warnings.value = data.items
    loadError.value = ''
    // 取到数的那一刻才算"更新于"；失败路径不动它，让"多旧"继续长大（比假装新鲜诚实）
    fetchedAt.value = new Date().toISOString()
    // 台账总数单独取一次，失败绝不把整次取数带红：窗口那句提示可以只报"最近 N 条"。
    try {
      const info = await api.ready()
      storeTotal.value = info?.store?.warning_count ?? null
      taskTotal.value = info?.store?.task_count ?? null
      chainTotal.value = info?.store?.chain_count ?? null
    } catch {
      storeTotal.value = null
      taskTotal.value = null
      chainTotal.value = null
    }
    if (legs) {
      const row = legs.items.find((item) => item.name === 'delivery')
      // 触达口径只认状态面那一条事实：`driver=mock` 时"4/4 通道成功"是演练出来的，
      // 与真实网关的送达不是一回事（铁律 7：两种口径分开报，别让 mock 数字冒充现场触达）
      deliveryMode.value = row?.driver ?? null
    }
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '网络请求未完成'
  } finally {
    loading.value = false
  }
}

async function inspect(record: WarningRecord): Promise<void> {
  const seq = ++inspectSeq
  current.value = record
  open.value = true
  currentTasks.value = []
  tasksError.value = null
  tasksLoading.value = true
  try {
    // 任务单元通过 event_id 关联：逐个按 ID 取回（后端提供 /tasks/{id}）
    const events = await api.events(EVENT_WINDOW)
    // 这一趟回查在路上时人可能已经点开另一条预警：迟到的结论只属于原来那条。
    if (seq !== inspectSeq) return
    const chain = events.items.find((item) => item.warning_id === record.warning_id)
    if (!chain) {
      // "最近窗口里找不到"不等于"这条预警没有任务单元"：把两者说成一句话，
      // 值班员就会以为这条预警没派任务——链路只保留最近若干条，这是取数边界不是事实。
      tasksError.value = `最近 ${EVENT_WINDOW} 条链路里没有这条预警的执行记录（链路保留窗口有限），任务单元无从判断`
      return
    }
    const units = await Promise.all((chain.task_units ?? []).map((id) => api.task(id).catch(() => null)))
    if (seq !== inspectSeq) return
    currentTasks.value = units.filter((unit): unit is TaskUnit => unit !== null)
    if (chain.task_units?.length && currentTasks.value.length < chain.task_units.length) {
      tasksError.value = `${chain.task_units.length} 个任务单元里有 ${chain.task_units.length - currentTasks.value.length} 个取不回来，上面只列取到的`
    }
  } catch (error) {
    if (seq === inspectSeq) tasksError.value = error instanceof Error ? `无法回查关联任务：${error.message}` : '无法回查关联任务'
  } finally {
    // 只由"当前这次点开"收尾：否则上一条的迟到响应会把这一条的加载圈关掉，
    // 看着像已经取完了。
    if (seq === inspectSeq) tasksLoading.value = false
  }
}

function isDelivered(delivery: WarningRecord['deliveries'][number]): boolean {
  return delivery.status === 'delivered' || delivery.status === 'retried'
}

function reachText(record: WarningRecord): string {
  if (!record.deliveries.length) return '无投递记录'
  const delivered = record.deliveries.filter(isDelivered).length
  return `${delivered}/${record.deliveries.length} 通道成功`
}

onMounted(() => {
  void load()
  // 刚发布的预警要能自己出现在列表里：这页原先只在挂载时取一次数，
  // 值班员发完一条预警盯着这页，看到的仍是打开那一刻的旧列表，而且没有任何提示。
  timer = setInterval(() => {
    if (document.visibilityState !== 'visible') return
    void load()
  }, REFRESH_MS)
})

onBeforeUnmount(() => {
  if (timer !== null) clearInterval(timer)
  timer = null
  drawerTrap?.detach()
  drawerTrap = null
})

/**
 * 抽屉的键盘焦点围栏。真机量过：打开详情抽屉后按 Tab，两次之内焦点就溜到 body，
 * 接着开始走顶栏链接——人在抽屉里、键盘在页面外，Esc 也关不掉（按键监听在抽屉上）。
 * antd-vue 的 Modal 自带焦点锁，Drawer 没有，这一层自己补。
 */
let drawerTrap: FocusTrapHandle | null = null
function onDrawerVisible(visible: boolean): void {
  drawerTrap?.detach()
  drawerTrap = null
  if (!visible) return
  drawerTrap = attachFocusTrap(document.querySelector('.warnings-inspector .ant-drawer-content-wrapper'), () => {
    open.value = false
  })
}
</script>

<template>
  <div class="warnings-page">
    <div class="warnings-board">
      <section class="warnings-panel warnings-ledger" aria-label="告警与任务">
        <PageHero class="warnings-heading" title="告警与任务" caption="查阅已生成的预警、投递回执与关联处置任务。">
          <template #actions>
            <router-link to="/monitor#drill" custom v-slot="{ href, navigate }">
              <a-button class="warnings-drill" type="primary" :href="href" @click="navigate">
                <template #icon><PlayCircleOutlined aria-hidden="true" /></template>
                处置演练
              </a-button>
            </router-link>
            <a-button class="warnings-refresh" :loading="loading" @click="load">
              <template #icon><ReloadOutlined aria-hidden="true" /></template>
              刷新
            </a-button>
          </template>
        </PageHero>

        <dl class="warnings-metrics" aria-label="预警指标">
          <div v-for="metric in metrics" :key="metric.label" class="warnings-metric" :data-tone="metric.tone">
            <dt>
              <span class="warnings-metric__icon" aria-hidden="true"><component :is="metric.icon" /></span>
              {{ metric.label }}
            </dt>
            <dd><strong>{{ metric.value ?? '—' }}</strong> <span v-if="metric.value !== null">条</span></dd>
            <dd class="warnings-metric__detail">{{ metric.detail }}</dd>
          </div>
        </dl>

        <div class="warning-filters" role="group" aria-label="预警查询">
          <h2 class="warnings-filter-title"><FilterOutlined aria-hidden="true" /> 预警查询</h2>
          <label class="warning-search">
            <SearchOutlined aria-hidden="true" />
            <input v-model="search" class="warning-filter" type="search" aria-label="查找预警" placeholder="标题、区域、预警编号" data-testid="warning-search" />
          </label>
          <div class="warning-selects">
            <select v-model="levelFilter" class="warning-filter" aria-label="风险等级" data-testid="warning-level">
              <option value="">全部等级</option>
              <option v-for="level in riskLevels" :key="level" :value="level">{{ RISK_LABELS[level] }}</option>
            </select>
            <select v-model="hazardFilter" class="warning-filter" aria-label="灾种" data-testid="warning-hazard">
              <option value="">全部灾种</option>
              <option v-for="(label, hazard) in HAZARD_LABELS" :key="hazard" :value="hazard">{{ label }}</option>
            </select>
            <select v-model="regionFilter" class="warning-filter" aria-label="区域" data-testid="warning-region">
              <option value="">全部区域</option>
              <option v-for="region in regionOptions" :key="region" :value="region">{{ region }}</option>
            </select>
            <select v-model="channelFilter" class="warning-filter" aria-label="通道" data-testid="warning-channel">
              <option value="">全部通道</option>
              <option v-for="channel in channelOptions" :key="channel" :value="channel">{{ channel }}</option>
            </select>
          </div>
          <a-button v-if="filtersActive" class="warning-reset" @click="resetFilters">清除筛选</a-button>
        </div>
        <FreshnessBar :fetched-at="fetchedAt" :label="freshLabel" :interval-ms="REFRESH_MS" :stacked="compact" />
        <a-alert
          v-if="loadError"
          type="error"
          show-icon
          class="warning-note"
          data-testid="warning-load-error"
          :message="`加载预警失败：${loadError}${warnings.length ? '。当前显示上次读取的记录' : ''}`"
        />
        <p v-if="filtersActive" class="warning-note" data-testid="warning-filter-count">最近加载的 {{ warnings.length }} 条中有 {{ visibleWarnings.length }} 条匹配</p>
        <p v-if="windowNote" class="warning-note" data-testid="window-note">
          {{ windowNote }}
        </p>
        <div v-if="compact" :aria-busy="loading">
          <ul v-if="mobileWarnings.length" class="ops-list">
            <li v-for="record in mobileWarnings" :key="record.warning_id" class="ops-record warning-row">
              <div class="ops-record__head">
                <span class="warning-risk" :style="{ '--warning-color': RISK_COLORS[record.risk_level] }">
                  {{ RISK_LABELS[record.risk_level] }}
                </span>
                <time class="warning-time" :datetime="record.generated_at">{{ formatOperatingTime(record.generated_at) }}</time>
              </div>
              <button class="warning-title-link" type="button" @click="inspect(record)">{{ record.title_zh }}</button>
              <div class="ops-record__meta">
                <span>{{ HAZARD_LABELS[record.hazard_type] }} · {{ record.region_codes.join('、') || '区域未标注' }}</span>
                <span>{{ reachTitle }} {{ reachText(record) }}</span>
              </div>
            </li>
          </ul>
          <a-pagination
            v-if="visibleWarnings.length > PAGE_SIZE"
            v-model:current="tablePage"
            class="warning-pagination"
            :total="visibleWarnings.length"
            :page-size="PAGE_SIZE"
            :show-size-changer="false"
            simple
          />
          <p v-if="!mobileWarnings.length" class="ops-record" data-testid="warnings-empty">{{ emptyTitle }}</p>
        </div>
        <a-table
          v-else
          class="warnings-table"
          :columns="columns"
          :data-source="visibleWarnings"
          row-key="warning_id"
          :pagination="{ pageSize: PAGE_SIZE, current: tablePage, showSizeChanger: false }"
          size="small"
          :scroll="{ x: 'max-content' }"
          @change="tablePage = $event.current ?? 1"
        >
          <!-- 空表落回 antd 默认的英文 "No data"（全新生起的后端实测）：中文值班台上一句英文，
               还分不清"确实还没有"与"取数没成功"，也不说下一步去哪造一条。 -->
          <template #emptyText>
            <div class="empty-hero" data-testid="warnings-empty">
              <span class="empty-hero__icon" aria-hidden="true">◇</span>
              <div>
                <div class="empty-hero__title">{{ emptyTitle }}</div>
                <div v-if="!loadError && !loading && !filtersActive" class="empty-hero__desc">现场预警和演练预警生成后会出现在此处。</div>
                <router-link v-if="!loadError && !loading && !filtersActive" class="empty-hero__action" to="/monitor">查看监测 →</router-link>
              </div>
            </div>
          </template>
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'title_zh'">
              <button class="warning-title-link" type="button" @click="inspect(record)">
                <AlertOutlined v-if="record.risk_level <= 2" class="warning-row-icon" :data-tone="record.risk_level === 1 ? 'danger' : 'warning'" aria-hidden="true" />
                {{ record.title_zh }}
              </button>
            </template>
            <template v-else-if="column.key === 'hazard'">{{ HAZARD_LABELS[record.hazard_type as keyof typeof HAZARD_LABELS] }}</template>
            <template v-else-if="column.key === 'level'">
              <a-tag :color="RISK_COLORS[record.risk_level as keyof typeof RISK_COLORS]">
                {{ RISK_LABELS[record.risk_level as keyof typeof RISK_LABELS] }}
              </a-tag>
            </template>
            <template v-else-if="column.key === 'regions'">
              <a-space wrap>
                <a-tag v-for="code in record.region_codes" :key="code">{{ code }}</a-tag>
              </a-space>
            </template>
            <template v-else-if="column.key === 'generated_at'">
              {{ formatOperatingTime(record.generated_at) }}
            </template>
            <template v-else-if="column.key === 'channels'">
              <a-space wrap>
                <a-tag v-for="channel in record.channels" :key="channel">{{ channel }}</a-tag>
              </a-space>
            </template>
            <template v-else-if="column.key === 'reach'">{{ reachText(record) }}</template>
            <template v-else-if="column.key === 'action'">
              <a-button class="warnings-detail" type="link" size="small" @click="inspect(record)">详情 <RightOutlined aria-hidden="true" /></a-button>
            </template>
          </template>
        </a-table>
      </section>

      <aside class="warnings-sidebar" aria-label="预警和任务概况">
        <section class="warnings-panel warnings-overview">
          <h2><InboxOutlined aria-hidden="true" /> 告警概览</h2>
          <div class="warning-side-total">
            <span class="warnings-metric__icon" aria-hidden="true"><DatabaseOutlined /></span>
            <div><strong>{{ storeTotal ?? '—' }}</strong><span>台账总数</span></div>
          </div>
          <p class="warning-side-caption">{{ windowLabel }} · 等级分布</p>
          <div v-if="warnings.length" class="warning-severity-track" aria-hidden="true">
            <span v-for="level in severityBreakdown" :key="level.tone" :data-tone="level.tone" :style="{ flexGrow: level.count }" />
          </div>
          <dl class="warning-side-list">
            <div v-for="level in severityBreakdown" :key="level.tone" :data-tone="level.tone">
              <dt><span class="warning-severity-dot" aria-hidden="true" />{{ level.label }}</dt>
              <dd>{{ hasSnapshot ? level.count : '—' }}<span v-if="hasSnapshot"> 条</span></dd>
            </div>
          </dl>
        </section>

        <section class="warnings-panel warnings-tasks">
          <h2><UnorderedListOutlined aria-hidden="true" /> 任务处置</h2>
          <div class="warning-side-total">
            <span class="warnings-metric__icon" aria-hidden="true"><UnorderedListOutlined /></span>
            <div><strong>{{ taskTotal ?? '—' }}</strong><span>在册任务单元</span></div>
          </div>
          <dl class="warning-side-list"><div><dt>链路记录</dt><dd>{{ chainTotal ?? '—' }}<span v-if="chainTotal !== null"> 条</span></dd></div></dl>
          <p class="warning-side-caption warnings-tasks__hint">在预警详情查看关联任务</p>
        </section>

        <section class="warnings-panel warnings-hazards">
          <h2><BarChartOutlined aria-hidden="true" /> 预警灾种分布</h2>
          <p class="warning-side-caption">{{ windowLabel }}</p>
          <ul v-if="windowSummary.hazards.length" class="warning-hazard-list">
            <li v-for="[hazard, count] in windowSummary.hazards" :key="hazard">
              <button type="button" :aria-pressed="hazardFilter === hazard" @click="hazardFilter = hazardFilter === hazard ? '' : hazard">
                <span>{{ HAZARD_LABELS[hazard] }}</span>
                <span class="warning-hazard-track" aria-hidden="true"><span :style="{ width: `${count / warnings.length * 100}%` }" /></span>
                <strong>{{ count }}</strong>
              </button>
            </li>
          </ul>
          <p v-else class="warning-side-caption">{{ hasSnapshot ? '暂无预警记录' : '尚未取得预警数据' }}</p>
        </section>
      </aside>
    </div>

    <a-drawer v-model:open="open" root-class-name="warnings-inspector" width="min(720px, 100vw)" title="预警详情与处置任务" @after-open-change="onDrawerVisible">
      <template v-if="current">
        <a-descriptions :column="1" bordered size="small">
          <!-- 助手页的「解释一条预警」要 wrn_ 编号，此前整个界面没有一处显示它，
               那条芯片点下去只能得到"缺少预警编号"。编号得在能读到它的地方。 -->
          <a-descriptions-item label="预警标识">{{ current.warning_id }}</a-descriptions-item>
          <a-descriptions-item label="标题">{{ current.title_zh }}</a-descriptions-item>
          <a-descriptions-item label="中文正文">{{ current.body_zh }}</a-descriptions-item>
          <a-descriptions-item label="藏文正文">
            <span v-if="current.body_bo">{{ current.body_bo }}</span>
            <a-tag v-else-if="current.translation_pending" color="orange">待译（未接入可信翻译，不伪造译文）</a-tag>
            <span v-else>—</span>
          </a-descriptions-item>
          <a-descriptions-item label="受众">
            <a-tag v-for="audience in current.audiences" :key="audience">{{ audience }}</a-tag>
          </a-descriptions-item>
          <a-descriptions-item label="发布时刻（UTC+8）">{{ current.released_at ? formatOperatingTime(current.released_at) : '未发布' }}</a-descriptions-item>
        </a-descriptions>

        <h4>通道投递与回执</h4>
        <a-table
          class="warning-deliveries"
          :data-source="current.deliveries"
          :columns="[
            { title: '通道', dataIndex: 'channel', key: 'channel' },
            { title: '状态', dataIndex: 'status', key: 'status' },
            { title: '覆盖人数', dataIndex: 'audience_count', key: 'audience_count' },
            { title: '回执时刻（UTC+8）', dataIndex: 'receipt_at', key: 'receipt_at' },
          ]"
          :row-key="(row: WarningRecord['deliveries'][number]) => `${row.channel}-${row.attempted_at}`"
          :pagination="false"
          size="small"
          :scroll="compact ? { x: 460 } : undefined"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'status'">{{ deliveryLabels[record.status as keyof typeof deliveryLabels] }}</template>
            <template v-if="column.key === 'receipt_at'">
              {{ record.receipt_at ? formatOperatingTime(record.receipt_at) : '无回执' }}
            </template>
          </template>
        </a-table>

        <h4>关联任务单元</h4>
        <a-alert
          v-if="tasksError"
          type="warning"
          show-icon
          :message="tasksError"
          data-testid="tasks-notice"
          style="margin-bottom: 8px"
        />
        <a-empty
          v-else-if="!tasksLoading && !currentTasks.length"
          description="这条预警的链路没有产出任务单元"
          data-testid="tasks-empty"
        />
        <a-table
          :data-source="currentTasks"
          :loading="tasksLoading"
          :columns="[
            { title: '任务标识', dataIndex: 'task_unit_id', key: 'task_unit_id' },
            { title: '任务', dataIndex: 'objective', key: 'objective' },
            { title: '时限', dataIndex: 'sla_seconds', key: 'sla_seconds' },
            { title: '责任角色', dataIndex: 'owner_role', key: 'owner_role' },
            { title: '产出方', dataIndex: 'created_by', key: 'created_by' },
          ]"
          row-key="task_unit_id"
          :pagination="false"
          size="small"
          :scroll="compact ? { x: 540 } : undefined"
        />
      </template>
    </a-drawer>
  </div>
</template>

<style scoped>
.warnings-page {
  container-type: inline-size;
  --warning-line: var(--aeg-color-command-line);
  --warning-accent: var(--aeg-color-overview-accent);
  --warning-panel: var(--aeg-color-command-panel);
  --warning-raised: var(--aeg-color-command-raised);
  --warning-deep: var(--aeg-color-command-deep);
  --warning-card-edge: color-mix(in srgb, var(--warning-accent) 34%, var(--warning-line));
  --warning-glow: var(--aeg-color-command-glow);
  font-variant-numeric: tabular-nums;
}
.warnings-board {
  display: grid;
  grid-template-columns: minmax(224px, 19.5fr) minmax(0, 80.5fr);
  grid-template-areas: 'sidebar ledger';
  align-items: start;
  gap: var(--aeg-space-s3);
}
.warnings-panel {
  min-width: 0;
  border: 1px solid transparent;
  border-radius: var(--aeg-radius-lg);
  background:
    radial-gradient(ellipse at 12% -12%, var(--warning-glow), transparent 61%) padding-box,
    linear-gradient(155deg, var(--warning-panel), var(--warning-deep) 68%, var(--warning-panel)) padding-box,
    linear-gradient(140deg, var(--warning-card-edge), var(--warning-line) 42%, color-mix(in srgb, var(--warning-accent) 42%, var(--warning-line))) border-box;
}
.warnings-ledger {
  grid-area: ledger;
  container-type: inline-size;
  padding: var(--aeg-space-s3);
}
.warnings-heading {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  grid-template-areas: 'title caption actions';
  align-items: center;
  gap: var(--aeg-space-s3) var(--aeg-space-s4);
  margin-bottom: var(--aeg-space-s4);
  padding-inline: var(--aeg-space-s1);
}
.warnings-heading :deep(.page-head__row) { display: contents; }
.warnings-heading :deep(.page-head__title) { grid-area: title; }
.warnings-heading :deep(.page-head__caption) {
  grid-area: caption;
  margin: 0;
  padding-inline-start: var(--aeg-space-s3);
  border-inline-start: 1px solid var(--warning-line);
  font-size: var(--aeg-font-sm);
}
.warnings-heading :deep(.page-head__actions) { grid-area: actions; }
.warnings-drill.ant-btn-primary {
  background: linear-gradient(120deg, var(--aeg-color-brand), var(--aeg-color-brand-active));
}
.warnings-refresh.ant-btn {
  border-color: var(--warning-line);
  background: var(--warning-raised);
}
.warnings-metrics {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: var(--aeg-space-s2);
  margin: 0 0 var(--aeg-space-s4);
}
.warnings-page :is([data-tone], .warning-side-total) { --warning-tone: var(--warning-accent); }
.warnings-page [data-tone='danger'] { --warning-tone: var(--aeg-color-status-danger); }
.warnings-page [data-tone='warning'] { --warning-tone: var(--aeg-tag-orange-text); }
.warnings-page [data-tone='info'] { --warning-tone: var(--aeg-color-status-info); }
.warnings-page [data-tone='accent'] { --warning-tone: var(--aeg-color-status-success); }
.warnings-metric {
  position: relative;
  min-width: 0;
  padding: var(--aeg-space-s2) var(--aeg-space-s2) var(--aeg-space-s2) 52px;
  border: 1px solid color-mix(in srgb, var(--warning-tone) 26%, var(--warning-line));
  border-radius: var(--aeg-radius-md);
  background:
    radial-gradient(ellipse at 4% 6%, color-mix(in srgb, var(--warning-tone) 20%, transparent), transparent 72%),
    linear-gradient(135deg, var(--warning-raised), var(--warning-panel) 82%);
}
.warnings-metric dt {
  color: var(--aeg-color-text);
  font-size: var(--aeg-font-sm);
  font-weight: var(--aeg-weight-semibold);
}
.warnings-metric dd {
  margin: 0;
  color: var(--aeg-color-text-secondary);
  font-size: var(--aeg-font-sm);
}
.warnings-metric dd strong,
.warning-side-total strong {
  color: var(--aeg-color-text);
  font-family: var(--aeg-font-display);
  font-size: var(--aeg-font-2xl);
  font-weight: var(--aeg-weight-bold);
  line-height: var(--aeg-lh-tight);
}
.warnings-metric .warnings-metric__detail {
  margin-top: 2px;
  font-size: var(--aeg-font-xs);
}
.warnings-metric__icon {
  display: grid;
  place-items: center;
  flex: 0 0 auto;
  width: 38px;
  height: 38px;
  border: 1px solid color-mix(in srgb, var(--warning-tone) 28%, transparent);
  border-radius: 50%;
  color: var(--warning-tone);
  background: color-mix(in srgb, var(--warning-tone) 16%, var(--warning-panel));
  font-size: var(--aeg-font-lg);
}
.warnings-metric .warnings-metric__icon {
  position: absolute;
  inset: 50% auto auto var(--aeg-space-s2);
  transform: translateY(-50%);
}
.warning-filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--aeg-space-s2) var(--aeg-space-s3);
  padding: var(--aeg-space-s2);
  margin-bottom: var(--aeg-space-s3);
  border: 1px solid var(--warning-line);
  border-radius: var(--aeg-radius-md);
  background: linear-gradient(105deg, color-mix(in srgb, var(--warning-raised) 66%, var(--warning-panel)), var(--warning-panel));
}
.warnings-filter-title {
  flex: 0 0 auto;
  margin: 0;
  padding-inline: var(--aeg-space-s1);
  color: var(--aeg-color-text);
  font-size: var(--aeg-font-base);
}
.warnings-filter-title :deep(.anticon),
.warnings-sidebar h2 :deep(.anticon) { color: var(--warning-accent); }
.warning-search {
  position: relative;
  flex: 1 1 190px;
  min-width: 0;
}
.warning-search :deep(.anticon) {
  position: absolute;
  inset: 50% auto auto var(--aeg-space-s2);
  transform: translateY(-50%);
  color: var(--aeg-color-text-secondary);
  pointer-events: none;
}
.warning-search .warning-filter { padding-inline-start: var(--aeg-space-s8); }
.warning-selects {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  flex: 1 1 430px;
  min-width: 0;
  gap: var(--aeg-space-s2);
}
.warning-filter {
  width: 100%;
  min-width: 0;
  min-height: 36px;
  padding: var(--aeg-space-s1) var(--aeg-space-s2);
  border: 1px solid var(--warning-line);
  border-radius: var(--aeg-radius-md);
  background: var(--warning-deep);
  color: var(--aeg-color-text);
  font: inherit;
}
.warning-filter:focus-visible,
.warning-title-link:focus-visible,
.warning-hazard-list button:focus-visible {
  outline: 2px solid var(--warning-accent);
  outline-offset: 2px;
}
.warning-filters select { color-scheme: light; }
html[data-theme='dark'] .warning-filters select { color-scheme: dark; }
.warning-filter::placeholder {
  color: var(--aeg-color-text-muted);
}
.warning-note,
.warning-pagination {
  margin-block: var(--aeg-space-s2);
}
.warning-note,
.warning-time {
  color: var(--aeg-color-text-secondary);
  font-size: var(--aeg-font-sm);
}
.warning-title-link {
  display: inline-flex;
  align-items: baseline;
  gap: var(--aeg-space-s2);
  padding: 0;
  border: none;
  background: none;
  color: var(--aeg-color-brand-hover);
  font-weight: var(--aeg-weight-semibold);
  text-align: start;
  overflow-wrap: anywhere;
  cursor: pointer;
}
.warning-row-icon { color: var(--warning-tone); }
.warnings-table :deep(.ant-table) {
  overflow: hidden;
  border: 1px solid var(--warning-line);
  border-radius: var(--aeg-radius-md);
  background: color-mix(in srgb, var(--warning-deep) 65%, var(--warning-panel));
}
.warnings-table :deep(.ant-table-thead > tr > th) {
  background: var(--warning-raised);
  color: var(--aeg-color-text);
  white-space: nowrap;
}
.warnings-table :deep(.ant-table-thead > tr > th::before) { display: none; }
.warnings-table :deep(.ant-table-tbody > tr:nth-child(even) > td) {
  background: color-mix(in srgb, var(--warning-raised) 36%, var(--warning-deep));
}
.warnings-table :deep(.ant-table-tbody > tr:hover > td) {
  background: color-mix(in srgb, var(--warning-accent) 12%, var(--warning-panel));
}
.warnings-table :deep(.ant-table-cell) { border-bottom-color: var(--warning-line); }
.warnings-table :deep(.ant-table-cell:not(:first-child)) { white-space: nowrap; }
.warnings-table :deep(.ant-table-cell .ant-tag) { margin-inline-end: 0; }
.warnings-detail.ant-btn { padding-inline: var(--aeg-space-s1); }
.warning-title-link:hover {
  text-decoration: underline;
}
.warning-row .warning-title-link {
  min-height: 36px;
}
.warning-risk {
  display: inline-flex;
  align-items: center;
  gap: var(--aeg-space-s1);
  color: var(--aeg-color-text);
}
.warning-risk::before {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--warning-color);
  content: '';
}
.warning-pagination {
  text-align: right;
}
.warnings-sidebar {
  grid-area: sidebar;
  display: grid;
  gap: var(--aeg-space-s3);
  min-width: 0;
}
.warnings-sidebar > section { padding: var(--aeg-space-s3); }
.warnings-sidebar h2 {
  display: flex;
  align-items: center;
  gap: var(--aeg-space-s2);
  margin: 0;
  padding-bottom: var(--aeg-space-s2);
  border-bottom: 1px solid color-mix(in srgb, var(--warning-line) 65%, transparent);
  color: var(--aeg-color-text);
  font-size: var(--aeg-font-md);
  line-height: var(--aeg-lh-snug);
}
.warnings-overview { position: relative; isolation: isolate; }
html[data-theme='dark'] .warnings-overview::before {
  position: absolute;
  z-index: -1;
  inset: 22px 1px auto auto;
  width: 68%;
  height: 90px;
  background: url('/backgrounds/aegis-glacier-ridge.png') center right / contain no-repeat;
  opacity: .45;
  pointer-events: none;
  content: '';
}
.warning-side-total {
  display: flex;
  align-items: center;
  gap: var(--aeg-space-s3);
  margin-block: var(--aeg-space-s3);
}
.warning-side-total .warnings-metric__icon { width: 48px; height: 48px; }
.warning-side-total div > span {
  display: block;
  color: var(--aeg-color-text-secondary);
  font-size: var(--aeg-font-sm);
}
.warning-side-caption {
  margin: 0 0 var(--aeg-space-s2);
  color: var(--aeg-color-text-secondary);
  font-size: var(--aeg-font-sm);
}
.warning-severity-track {
  display: flex;
  overflow: hidden;
  height: 5px;
  margin-bottom: var(--aeg-space-s1);
  border-radius: var(--aeg-radius-pill);
  background: var(--warning-line);
}
.warning-severity-track > span { flex: 0 1 0; background: var(--warning-tone); }
.warning-side-list { margin: 0; }
.warning-side-list > div {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--aeg-space-s2);
  min-height: 30px;
  border-bottom: 1px solid color-mix(in srgb, var(--warning-line) 54%, transparent);
  color: var(--aeg-color-text);
  font-size: var(--aeg-font-sm);
}
.warning-side-list > div:last-child { border-bottom: 0; }
.warning-side-list dt { display: flex; align-items: center; gap: var(--aeg-space-s2); }
.warning-side-list dd { margin: 0; font-weight: var(--aeg-weight-semibold); }
.warning-side-list dd span { color: var(--aeg-color-text-secondary); }
.warning-severity-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--warning-tone);
}
.warnings-tasks__hint { margin: var(--aeg-space-s2) 0 0; }
.warnings-hazards .warning-side-caption { margin-top: var(--aeg-space-s2); }
.warning-hazard-list { margin: 0; padding: 0; list-style: none; }
.warning-hazard-list button {
  display: grid;
  grid-template-columns: minmax(4em, auto) minmax(0, 1fr) 3ch;
  align-items: center;
  gap: var(--aeg-space-s2);
  width: 100%;
  min-height: 32px;
  padding-inline: var(--aeg-space-s1);
  border: 0;
  border-radius: var(--aeg-radius-sm);
  background: transparent;
  color: var(--aeg-color-text);
  font: inherit;
  font-size: var(--aeg-font-sm);
  text-align: left;
  cursor: pointer;
}
.warning-hazard-list button:is(:hover, [aria-pressed='true']) { background: var(--warning-raised); }
.warning-hazard-list button strong { justify-self: end; }
.warning-hazard-track {
  height: 5px;
  border-radius: var(--aeg-radius-pill);
  background: var(--warning-line);
}
.warning-hazard-track > span {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: var(--warning-accent);
}
@container (max-width: 1040px) {
  .warnings-board { grid-template-columns: minmax(0, 1fr); grid-template-areas: 'ledger' 'sidebar'; }
  .warnings-sidebar { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}
@container (max-width: 740px) {
  .warnings-heading { grid-template-columns: minmax(0, 1fr) auto; grid-template-areas: 'title actions' 'caption caption'; }
  .warnings-heading :deep(.page-head__caption) { padding: 0; border: 0; }
  .warnings-metric { padding-inline-start: var(--aeg-space-s2); }
  .warnings-metric .warnings-metric__icon { display: none; }
}
@container (max-width: 590px) {
  .warnings-sidebar { grid-template-columns: minmax(0, 1fr); }
  .warnings-metrics { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .warning-selects { flex-basis: 100%; grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .warning-search { flex-basis: 150px; }
}
@container (max-width: 410px) {
  .warnings-heading { column-gap: var(--aeg-space-s2); }
  .warnings-heading :deep(.page-head__title) { font-size: var(--aeg-font-lg); }
  .warnings-metric dd strong { font-size: var(--aeg-font-xl); }
}
@container (max-width: 325px) {
  .warnings-heading { grid-template-areas: 'title' 'caption' 'actions'; grid-template-columns: minmax(0, 1fr); }
  .warnings-heading :deep(.page-head__actions) { margin-inline-start: 0; }
}
@media (max-width: 720px) {
  .warnings-ledger, .warnings-sidebar > section {
    padding: var(--aeg-space-s2);
  }
  .warning-filter {
    min-height: 40px;
  }
}
</style>
