/**
 * useAlerts：SSE 分流去重、台账对账合并、紧急集筛选。
 *
 * 用 `vi.resetModules()` + 动态 import 拿干净的模块实例——告警状态是模块级单例，
 * 用例之间不重置就会互相看见对方的卡片（与 useAssistantSession 的取舍不同：
 * 会话要跨页接得上，告警不需要）。
 */

import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { ref } from 'vue'

import type { AlertDto } from '@/api/alerts'

const listMock = vi.fn()
const ackMock = vi.fn()
const noticeMocks = vi.hoisted(() => ({
  seat: { value: '' },
  list: vi.fn(),
  read: vi.fn(),
}))

vi.mock('@/api/operatorSeat', () => ({
  operatorSeat: noticeMocks.seat,
  setOperatorSeat: (value: string) => {
    noticeMocks.seat.value = value
  },
  clearOperatorSeat: () => {
    noticeMocks.seat.value = ''
  },
  operatorHeaders: () => (noticeMocks.seat.value ? { 'X-Aegis-Operator': noticeMocks.seat.value } : {}),
  OPERATOR_HEADER: 'X-Aegis-Operator',
}))

vi.mock('@/api/notifications', () => ({
  notificationApi: {
    list: (...args: unknown[]) => noticeMocks.list(...args),
    read: (...args: unknown[]) => noticeMocks.read(...args),
  },
}))

vi.mock('@/api/alerts', () => ({
  alertApi: {
    list: (...args: unknown[]) => listMock(...args),
    ack: (...args: unknown[]) => ackMock(...args),
    transfer: vi.fn(),
    snooze: vi.fn(),
  },
  AlertApiError: class extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  },
}))

function alertEvent(warningId: string, level: number, drill = false): Record<string, unknown> {
  return {
    subject: `platform.alert.${level}.540121`,
    trace_id: 'trc_x',
    payload: { warning_id: warningId, risk_level: level, regions: ['540121'], title: `告警${level}`, drill },
    ts: new Date().toISOString(),
  }
}

function otherEvent(): Record<string, unknown> {
  return { subject: 'ops.feedback.status', trace_id: 'trc_y', payload: {}, ts: new Date().toISOString() }
}

function noticeEvent(id: number, recipient = 'op-duty'): Record<string, unknown> {
  return {
    subject: 'notify.sent',
    trace_id: 'trc_n',
    payload: {
      id,
      recipient,
      message: `消息${id}`,
      kind: 'lead',
      sender: 'op-lead',
      space_id: null,
      created_at: new Date().toISOString(),
    },
    ts: new Date().toISOString(),
  }
}

function noticeRow(id: number): Record<string, unknown> {
  return {
    id,
    recipient: 'op-duty',
    message: `补回消息${id}`,
    kind: 'group',
    sender: 'op-lead',
    space_id: null,
    created_at: new Date().toISOString(),
    read_at: null,
  }
}

function ledgerRow(warningId: string, level: number, overrides: Partial<AlertDto> = {}): AlertDto {
  return {
    alert_id: `alt_${warningId.slice(4, 12)}`,
    warning_id: warningId,
    level,
    region_code: '540121',
    status: 'raised',
    ack_by: null,
    ack_at: null,
    assigned_to: null,
    snooze_until: null,
    escalated_at: null,
    drill: false,
    created_at: new Date().toISOString(),
    title: '',
    hazard_type: '',
    ...overrides,
  }
}

async function fresh() {
  vi.resetModules()
  const { useAlerts } = await import('@/composables/useAlerts')
  return useAlerts()
}

describe('useAlerts', () => {
  beforeEach(() => {
    listMock.mockReset().mockResolvedValue({ count: 0, items: [], persistent: false, escalation_minutes: 5 })
    ackMock.mockReset()
    noticeMocks.seat.value = ''
    noticeMocks.list.mockReset().mockResolvedValue({ count: 0, items: [] })
    noticeMocks.read.mockReset().mockResolvedValue({ notification: {} })
  })

  it('只把 platform.alert.* 变成卡片，同一预警重复广播只出一张', async () => {
    const alerts = await fresh()
    const events = ref<Record<string, unknown>[]>([])
    alerts.attachStream({ events: events as never, connected: ref(true) as never })
    events.value = [otherEvent(), alertEvent('wrn_a', 1), alertEvent('wrn_a', 1), alertEvent('wrn_b', 3)] as never
    await vi.waitFor(() => expect(alerts.cards.value.length).toBe(2))
    expect(alerts.cards.value.map((card) => card.warning_id)).toEqual(['wrn_b', 'wrn_a'])
    // 紧急集只含 Ⅰ/Ⅱ 级
    expect(alerts.urgentCards.value.map((card) => card.warning_id)).toEqual(['wrn_a'])
  })

  it('对账以台账为准合并：补上 alert_id 与状态，本地标题在台账缺失时保留', async () => {
    listMock.mockResolvedValue({
      count: 1,
      items: [ledgerRow('wrn_a', 1, { title: '', status: 'acked', ack_by: 'op-duty' })],
      persistent: true,
      escalation_minutes: 5,
    })
    const alerts = await fresh()
    const events = ref<Record<string, unknown>[]>([alertEvent('wrn_a', 1)] as never)
    alerts.attachStream({ events: events as never, connected: ref(true) as never })
    await vi.waitFor(() => expect(alerts.cards.value[0]?.alert_id).toBe('alt_a'))
    expect(alerts.cards.value[0]?.status).toBe('acked')
    expect(alerts.cards.value[0]?.title).toBe('告警1')
    expect(alerts.cards.value[0]?.ack_by).toBe('op-duty')
  })

  it('断线重连触发对账，翻 true 的沿才翻', async () => {
    const alerts = await fresh()
    const connected = ref(false)
    alerts.attachStream({ events: ref([]) as never, connected })
    await Promise.resolve()
    expect(listMock).not.toHaveBeenCalled()
    connected.value = true
    await vi.waitFor(() => expect(listMock).toHaveBeenCalled())
  })

  it('接手成功回写卡片；失败返回可读原因而不是抛异常', async () => {
    listMock.mockResolvedValue({ count: 1, items: [ledgerRow('wrn_a', 1)], persistent: false, escalation_minutes: 5 })
    const alerts = await fresh()
    await alerts.refresh()
    const card = alerts.cards.value[0]
    expect(card).toBeTruthy()

    ackMock.mockResolvedValue({ alert: ledgerRow('wrn_a', 1, { status: 'acked', ack_by: 'op-duty' }), already: false })
    expect(await alerts.ack(card!)).toBe('')
    expect(alerts.cards.value[0]?.status).toBe('acked')

    const other = await fresh()
    listMock.mockResolvedValue({ count: 1, items: [ledgerRow('wrn_b', 2)], persistent: false, escalation_minutes: 5 })
    await other.refresh()
    const error = new (await import('@/api/alerts')).AlertApiError(401, '需要席位')
    ;(ackMock as Mock).mockRejectedValue(error)
    const target = other.cards.value[0]
    expect(await other.ack(target!)).toBe('先在页脚选择席位，再执行处置')
  })

  it('被稍后提醒挡住的告警不进紧急集', async () => {
    listMock.mockResolvedValue({
      count: 2,
      items: [
        ledgerRow('wrn_a', 1, { snooze_until: new Date(Date.now() + 60_000).toISOString() }),
        ledgerRow('wrn_b', 2),
      ],
      persistent: false,
      escalation_minutes: 5,
    })
    const alerts = await fresh()
    await alerts.refresh()
    expect(alerts.urgentCards.value.map((card) => card.warning_id)).toEqual(['wrn_b'])
    expect(alerts.badgeCount.value).toBe(2)
  })

  it('notify.sent 只投给当前席位且按通知 id 去重', async () => {
    noticeMocks.seat.value = 'op-duty'
    const alerts = await fresh()
    const events = ref<Record<string, unknown>[]>([
      noticeEvent(1),
      noticeEvent(1),
      noticeEvent(2),
      noticeEvent(3, 'op-lead'),
      otherEvent(),
    ] as never)
    alerts.attachStream({ events: events as never, connected: ref(true) as never })
    await vi.waitFor(() => expect(alerts.notices.value.length).toBe(2))
    expect(alerts.notices.value.map((notice) => notice.id)).toEqual([2, 1])
    expect(alerts.notices.value.every((notice) => notice.recipient === 'op-duty')).toBe(true)
  })

  it('刷新把断线期间的通知补回来，重复对账不叠加', async () => {
    noticeMocks.seat.value = 'op-duty'
    noticeMocks.list.mockResolvedValue({ count: 1, items: [noticeRow(9)] })
    const alerts = await fresh()
    const events = ref<Record<string, unknown>[]>([noticeEvent(1)] as never)
    alerts.attachStream({ events: events as never, connected: ref(true) as never })
    await vi.waitFor(() => expect(alerts.notices.value.length).toBe(1))

    alerts.refresh()
    await vi.waitFor(() => expect(alerts.notices.value.map((notice) => notice.id)).toEqual([9, 1]))
    alerts.refresh()
    await vi.waitFor(() => expect(noticeMocks.list).toHaveBeenCalledTimes(2))
    expect(alerts.notices.value.map((notice) => notice.id)).toEqual([9, 1])
    expect(alerts.noticeError.value).toBe('')
  })

  it('关闭通知立刻从列表移除并标为已读', async () => {
    noticeMocks.seat.value = 'op-duty'
    const alerts = await fresh()
    const events = ref<Record<string, unknown>[]>([noticeEvent(7)] as never)
    alerts.attachStream({ events: events as never, connected: ref(true) as never })
    await vi.waitFor(() => expect(alerts.notices.value.length).toBe(1))

    alerts.dismissNotice(7)
    expect(alerts.notices.value).toEqual([])
    await vi.waitFor(() => expect(noticeMocks.read).toHaveBeenCalledWith(7))
  })

  it('通知对账失败如实外露，不静默当成空清单', async () => {
    noticeMocks.seat.value = 'op-duty'
    noticeMocks.list.mockRejectedValue(new Error('通知对账失败'))
    const alerts = await fresh()
    alerts.refresh()
    await vi.waitFor(() => expect(alerts.noticeError.value).toBe('通知对账失败'))
  })
})
