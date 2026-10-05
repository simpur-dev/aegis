<script setup lang="ts">
import { message } from 'ant-design-vue'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import api, { ApiError } from '@/api/client'
import { fetchIntegrations } from '@/api/integrations'
import type { TaskUnit, WarningRecord } from '@/api/types'
import { HAZARD_LABELS, RISK_COLORS, RISK_LABELS } from '@/api/types'
import PageHero from '@/components/PageHero.vue'
import { formatOperatingTime } from '@/utils/clock'

const REFRESH_MS = 15_000
/**
 * 详情里回查链路时取多少条最近链路。写死一处，"找不到"的提示才与实取窗口一致。
 */
const EVENT_WINDOW = 200
/** 列表取多少条预警。也写死一处，"这只是最近 N 条"那句话才与实取窗口一致。 */
const WARNING_LIST_LIMIT = 100

const warnings = ref<WarningRecord[]>([])
const loading = ref(false)
const open = ref(false)
const current = ref<WarningRecord | null>(null)
const currentTasks = ref<TaskUnit[]>([])
const tasksLoading = ref(false)
const tasksError = ref<string | null>(null)
/** 触达通道的形态（mock / http / null=没读到），来自状态面那条 delivery 腿。 */
const deliveryMode = ref<string | null>(null)
/** 服务端一共多少条预警（`/readyz` 的台账数）；读不到就是 null，此时只报窗口不报总数。 */
const storeTotal = ref<number | null>(null)
let timer: ReturnType<typeof setInterval> | null = null
/**
 * 第几次点开详情。抽屉里同时挂着"这条预警的正文"和"它的任务单元"，
 * 而任务单元要回查最近 200 条链路才能算出来——真机量到过：先点的那条回查慢了 2.5 秒，
 * 人在这期间点了另一条，于是抽屉挂着 B 的标题、表里是 B 的 4 个任务单元，
 * 顶上却写着 A 那句"最近 200 条链路里没有这条预警的执行记录"。
 */
let inspectSeq = 0

const reachTitle = computed(() => (deliveryMode.value === 'mock' ? '触达（演练口径）' : '触达'))

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

/**
 * 取满窗口 = 有预警在这一页之外。真机把这台进程演练到 113 条时量到：列表取 100 条，
 * 页面上"窗口/最近/上限"一个字都没有，6 个页码按钮看起来就是"所有预警"——
 * 13 条预警等于凭空消失。
 */
const windowNote = computed<string>(() => {
  if (warnings.value.length < WARNING_LIST_LIMIT) return ''
  const total = storeTotal.value
  if (total === null) return `列表取的是最近 ${WARNING_LIST_LIMIT} 条（服务端总数没读到，这一页可能还有更早的预警）`
  if (total <= WARNING_LIST_LIMIT) return ''
  return `列表取的是最近 ${WARNING_LIST_LIMIT} 条，服务端共 ${total} 条——另外 ${total - WARNING_LIST_LIMIT} 条在这一页之外（本页没有翻页到更早数据的入口）`
})

async function load(): Promise<void> {
  loading.value = true
  try {
    const [data, legs] = await Promise.all([api.warnings({ limit: WARNING_LIST_LIMIT }), fetchIntegrations().catch(() => null)])
    warnings.value = data.items
    // 台账总数单独取一次，失败绝不把整次取数带红：窗口那句提示可以只报"最近 N 条"。
    try {
      const info = await api.ready()
      storeTotal.value = info?.store?.warning_count ?? null
    } catch {
      storeTotal.value = null
    }
    if (legs) {
      const row = legs.items.find((item) => item.name === 'delivery')
      // 触达口径只认状态面那一条事实：`driver=mock` 时"4/4 通道成功"是演练出来的，
      // 与真实网关的送达不是一回事（铁律 7：两种口径分开报，别让 mock 数字冒充现场触达）
      deliveryMode.value = row?.driver ?? null
    }
  } catch (error) {
    message.error(error instanceof ApiError ? `加载预警失败：${error.message}` : '加载预警失败')
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
  } finally {
    // 只由"当前这次点开"收尾：否则上一条的迟到响应会把这一条的加载圈关掉，
    // 看着像已经取完了。
    if (seq === inspectSeq) tasksLoading.value = false
  }
}

function reachText(record: WarningRecord): string {
  const delivered = record.deliveries.filter((d) => d.status === 'delivered' || d.status === 'retried').length
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
})
</script>

<template>
  <div>
    <PageHero icon="警" title="预警发布" badge="靶向触达" caption="红色预警含北斗短报文兜底通道。">
      <template #actions>
        <a-button @click="load">刷新</a-button>
      </template>
    </PageHero>

    <a-card size="small" title="预警发布与靶向触达" :loading="loading">
      <p v-if="windowNote" data-testid="window-note" style="margin: 0 0 6px; color: #5a6072; font-size: 12px">
        {{ windowNote }}
      </p>
      <a-table :columns="columns" :data-source="orderedWarnings" row-key="warning_id" :pagination="{ pageSize: 10 }" size="small">
        <!-- 空表落回 antd 默认的英文 "No data"（全新生起的后端实测）：中文值班台上一句英文，
             还分不清"确实还没有"与"取数没成功"，也不说下一步去哪造一条。 -->
        <template #emptyText>
          <div class="empty-hero" data-testid="warnings-empty">
            <span class="empty-hero__icon" aria-hidden="true">◇</span>
            <div>
              <div class="empty-hero__title">还没有预警</div>
              <div class="empty-hero__desc">这个进程启动后没有产出过预警——在监测页发起一次演练，或提交一条会命中阈值的上报，这里就会有内容。</div>
              <router-link class="empty-hero__action" to="/monitor">去监测页发起演练 →</router-link>
            </div>
          </div>
        </template>
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'hazard'">{{ HAZARD_LABELS[record.hazard_type as keyof typeof HAZARD_LABELS] }}</template>
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
            <a-button type="link" size="small" @click="inspect(record)">详情</a-button>
          </template>
        </template>
      </a-table>
    </a-card>

    <a-drawer v-model:open="open" :width="720" title="预警详情与处置任务">
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
          :data-source="current.deliveries"
          :columns="[
            { title: '通道', dataIndex: 'channel', key: 'channel' },
            { title: '状态', dataIndex: 'status', key: 'status' },
            { title: '覆盖人数', dataIndex: 'audience_count', key: 'audience_count' },
            { title: '回执时刻（UTC+8）', dataIndex: 'receipt_at', key: 'receipt_at' },
          ]"
          row-key="(r: { channel: string; attempted_at: string }) => r.channel + r.attempted_at"
          :pagination="false"
          size="small"
        >
          <template #bodyCell="{ column, record }">
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
        />
      </template>
    </a-drawer>
  </div>
</template>
