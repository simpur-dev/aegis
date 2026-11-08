/**
 * 告警卡片状态（模块级单例，与 `useAssistantSession` 同一取向）。
 *
 * 数据有两条来源，谁是真源要分清：
 * - **台账（GET /alerts）是真源**：状态、确认人、稍后时间都以它为准；
 * - SSE `platform.alert.*` 只负责"快"：卡片先出现（标题来自广播 payload），
 *   随后一次去抖同步把 alert_id 与台账状态补齐——广播先于落账到达是常态，
 *   所以卡片允许短暂没有 alert_id，处置动作在此之前禁用。
 *
 * 分级（需求 D3）：Ⅰ/Ⅱ 自动展开抽屉 + 顶部条；Ⅲ 展开、不抢输入焦点；Ⅳ 只进角标。
 * 演练卡（drill=true）走同一分级呈现，但绝不触发升级——升级在后端就排除了演练。
 */

import { computed, ref, watch, type Ref } from 'vue'

import { AlertApiError, alertApi, type AlertDto } from '@/api/alerts'
import { notificationApi, type NotificationDto } from '@/api/notifications'
import { operatorSeat } from '@/api/operatorSeat'
import type { StreamEvent } from '@/composables/useEventStream'

const MAX_CARDS = 200
const MAX_NOTICES = 20
const SYNC_DEBOUNCE_MS = 400

const cards = ref<AlertDto[]>([])
const syncing = ref(false)
const syncError = ref('')
const lastSyncAt = ref(0)
const notices = ref<NotificationDto[]>([])
const noticeError = ref('')

/** 见过的 warning_id：同一预警的重复广播与重连补发都不再出第二张卡。 */
const seen = new Set<string>()
let syncTimer: number | undefined
let attached: Ref<StreamEvent[]> | null = null

const ALERT_SUBJECT = 'platform.alert.'

function isTerminal(status: string): boolean {
  return status === 'merged' || status === 'closed'
}

function isOpen(card: AlertDto): boolean {
  return !isTerminal(card.status)
}

function snoozeActive(card: AlertDto): boolean {
  return card.snooze_until !== null && Date.parse(card.snooze_until) > Date.now()
}

function sortByUrgency(rows: AlertDto[]): void {
  rows.sort((a, b) => a.level - b.level || Date.parse(b.created_at) - Date.parse(a.created_at))
}

/** 保留本地卡上比台账更丰富的展示字段（广播先到、台账 title 缺失时用本地的）。 */
function mergeAlert(row: AlertDto, local?: AlertDto): AlertDto {
  return {
    ...row,
    title: row.title || local?.title || '',
    hazard_type: row.hazard_type || local?.hazard_type || '',
  }
}

async function sync(): Promise<void> {
  if (syncing.value) return
  syncing.value = true
  syncError.value = ''
  try {
    const body = await alertApi.list({ open_only: true, limit: 100 })
    const byWarning = new Map(cards.value.map((card) => [card.warning_id, card]))
    const merged = body.items.map((row) => mergeAlert(row, byWarning.get(row.warning_id)))
    // 本地有、台账没有的（刚广播还没落账）保留在队尾，等下一次同步补
    for (const card of cards.value) {
      if (!body.items.some((row) => row.warning_id === card.warning_id)) merged.push(card)
    }
    sortByUrgency(merged)
    cards.value = merged.slice(0, MAX_CARDS)
    lastSyncAt.value = Date.now()
  } catch (error) {
    syncError.value = error instanceof AlertApiError ? error.message : '告警对账失败'
  } finally {
    syncing.value = false
  }
}

async function syncNotices(): Promise<void> {
  if (operatorSeat.value === '') return
  try {
    const body = await notificationApi.list({ unread_only: true, limit: MAX_NOTICES })
    const byId = new Map(notices.value.map((item) => [item.id, item]))
    for (const row of body.items) byId.set(row.id, row)
    notices.value = [...byId.values()].sort((a, b) => b.id - a.id).slice(0, MAX_NOTICES)
    noticeError.value = ''
  } catch (error) {
    noticeError.value = error instanceof Error ? error.message : '通知对账失败'
  }
}

function dismissNotice(id: number): void {
  notices.value = notices.value.filter((item) => item.id !== id)
  void notificationApi.read(id).catch(() => {})
}

function scheduleSync(): void {
  if (syncTimer !== undefined) window.clearTimeout(syncTimer)
  syncTimer = window.setTimeout(() => void sync(), SYNC_DEBOUNCE_MS)
}

function onEvents(events: StreamEvent[]): void {
  let fresh = 0
  for (const event of events) {
    if (event.subject === 'notify.sent') {
      const payload = event.payload ?? {}
      const recipient = String(payload.recipient ?? '')
      if (operatorSeat.value === '' || recipient !== operatorSeat.value) continue
      const id = Number(payload.id ?? 0)
      if (id && notices.value.some((item) => item.id === id)) continue
      notices.value.unshift({
        id,
        recipient,
        message: String(payload.message ?? ''),
        kind: String(payload.kind ?? 'lead'),
        sender: String(payload.sender ?? ''),
        space_id: typeof payload.space_id === 'string' ? payload.space_id : null,
        created_at: typeof payload.created_at === 'string' ? payload.created_at : event.ts,
        read_at: null,
      })
      notices.value = notices.value.slice(0, MAX_NOTICES)
      continue
    }
    if (!event.subject.startsWith(ALERT_SUBJECT)) continue
    const warningId = String(event.payload?.warning_id ?? '')
    if (!warningId || seen.has(warningId)) continue
    seen.add(warningId)
    fresh += 1
    cards.value.unshift({
      alert_id: '',
      warning_id: warningId,
      level: Number(event.payload?.risk_level ?? 4),
      region_code: Array.isArray(event.payload?.regions) ? String(event.payload.regions[0] ?? '') : '',
      status: 'raised',
      ack_by: null,
      ack_at: null,
      assigned_to: null,
      snooze_until: null,
      escalated_at: null,
      drill: event.payload?.drill === true,
      created_at: event.ts,
      title: typeof event.payload?.title === 'string' ? event.payload.title : '',
      hazard_type: '',
    })
  }
  if (fresh > 0) scheduleSync()
}

/** 处置动作的统一出口：失败返回一句可读原因，成功返回空串并就地更新卡片。 */
async function runAction(card: AlertDto, run: (alertId: string) => Promise<AlertDto>): Promise<string> {
  let alertId = card.alert_id
  if (!alertId) {
    await sync()
    alertId = cards.value.find((row) => row.warning_id === card.warning_id)?.alert_id ?? ''
  }
  if (!alertId) return '告警尚未落账，稍后再试'
  try {
    const updated = await run(alertId)
    const index = cards.value.findIndex((row) => row.alert_id === alertId)
    if (index >= 0) cards.value.splice(index, 1, mergeAlert(updated, card))
    return ''
  } catch (error) {
    if (error instanceof AlertApiError) {
      if (error.status === 401) return '先在页脚选择席位，再执行处置'
      if (error.status === 403) return error.message || '当前席位没有这个处置权限'
      if (error.status === 409) return '这条告警已被处置，刷新后以台账为准'
      if (error.status === 404) return '告警不存在或台账形态不可用'
    }
    return error instanceof Error ? error.message : '处置失败'
  }
}

export function useAlerts() {
  /**
   * 挂到共享事件流上（App.vue 调用一次；幂等）。恢复连接时自动对账一次：
   * 断流期间的告警不靠本地缓存脑补，以台账为准。
   */
  function attachStream(handle: { events: Ref<StreamEvent[]>; connected: Ref<boolean> }): void {
    if (attached === handle.events) return
    attached = handle.events
    // immediate：挂上时流里已有的历史告警也要出卡（seen 集合保证重复广播不出第二张）
    watch(handle.events, onEvents, { immediate: true })
    watch(handle.connected, (now, prev) => {
      if (now && !prev) {
        scheduleSync()
        void syncNotices()
      }
    })
  }

  /** 主动对账（页面进入 / 手动刷新时）。 */
  function refresh(): void {
    void sync()
    void syncNotices()
  }

  function ack(card: AlertDto): Promise<string> {
    return runAction(card, (id) => alertApi.ack(id).then((body) => body.alert))
  }

  function transfer(card: AlertDto, assignee: string): Promise<string> {
    return runAction(card, (id) => alertApi.transfer(id, assignee).then((body) => body.alert))
  }

  function snooze(card: AlertDto, minutes: number): Promise<string> {
    return runAction(card, (id) => alertApi.snooze(id, minutes).then((body) => body.alert))
  }

  return {
    cards,
    syncing,
    syncError,
    lastSyncAt,
    notices,
    noticeError,
    dismissNotice,
    /** 还在桌上的（未合并、未关闭），已按严重度排序。 */
    openCards: computed(() => cards.value.filter(isOpen)),
    /** Ⅰ/Ⅱ 级且未被稍后提醒挡住：触发自动展开与顶部条。 */
    urgentCards: computed(() => cards.value.filter((card) => isOpen(card) && card.level <= 2 && !snoozeActive(card))),
    /** 角标数：桌上的全部告警（含被稍后的——它们仍是未确认）。 */
    badgeCount: computed(() => cards.value.filter(isOpen).length),
    attachStream,
    refresh,
    ack,
    transfer,
    snooze,
    snoozeActive,
  }
}
