/**
 * useHandlingSpace：SSE 按 subject 分流、版本闸、409 冲突与席位缺失的话术。
 *
 * 与 `useAlerts.spec.ts` 同一手法：`vi.resetModules()` + 动态 import 拿干净的模块实例，
 * 台账（`/spaces`）是真源，SSE 只负责"快"——所以断言永远落在"版本只前进不后退"上。
 * fake timers 开 `shouldAdvanceTime`：去抖是 400ms 真等待，测试要能快进，waitFor 也不能卡死。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'

import type { HandlingTaskDto, SpaceDetailDto, SpaceSummaryDto } from '@/api/spaces'

const SPACE_A = 'wrn_aaaaaaaaaaaa'
const SPACE_B = 'wrn_bbbbbbbbbbbb'
const STU_A = 'stu_0123456789abcdef'
const STU_B = 'stu_fedcba9876543210'

const listMock = vi.fn()
const detailMock = vi.fn()
const claimMock = vi.fn()
const ackMock = vi.fn()
const transferMock = vi.fn()

vi.mock('@/api/spaces', () => ({
  spaceApi: {
    list: (...args: unknown[]) => listMock(...args),
    detail: (...args: unknown[]) => detailMock(...args),
    claim: (...args: unknown[]) => claimMock(...args),
    ack: (...args: unknown[]) => ackMock(...args),
    transfer: (...args: unknown[]) => transferMock(...args),
  },
  SpaceApiError: class extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  },
}))

function summary(spaceId: string, overrides: Partial<SpaceSummaryDto> = {}): SpaceSummaryDto {
  return {
    space_id: spaceId,
    warning_id: spaceId,
    status: 'open',
    created_at: new Date().toISOString(),
    closed_at: null,
    tasks_total: 0,
    tasks_open: 0,
    tasks_closed: 0,
    ...overrides,
  }
}

function task(taskId: string, overrides: Partial<HandlingTaskDto> = {}): HandlingTaskDto {
  return {
    task_id: taskId,
    space_id: SPACE_A,
    stu_id: taskId,
    assignee: null,
    status: 'pending',
    version: 1,
    updated_at: new Date().toISOString(),
    ...overrides,
  }
}

function detailOf(tasks: HandlingTaskDto[], overrides: Partial<SpaceDetailDto> = {}): SpaceDetailDto {
  const counts: Record<string, number> = {}
  for (const row of tasks) counts[row.status] = (counts[row.status] ?? 0) + 1
  return {
    space: summary(SPACE_A),
    tasks,
    participants: [...new Set(tasks.map((row) => row.assignee).filter((id): id is string => id !== null))].sort(),
    status_counts: counts,
    tasks_total: tasks.length,
    persistent: false,
    ...overrides,
  }
}

function taskEvent(spaceId: string, row: HandlingTaskDto, verb = 'claim'): Record<string, unknown> {
  return {
    subject: 'handling.task.changed',
    trace_id: 'trc_t',
    payload: {
      verb,
      space_id: spaceId,
      task_id: row.task_id,
      stu_id: row.stu_id,
      status: row.status,
      assignee: row.assignee,
      version: row.version,
      updated_at: row.updated_at,
    },
    ts: new Date().toISOString(),
  }
}

function otherEvent(): Record<string, unknown> {
  return { subject: 'notify.sent', trace_id: 'trc_n', payload: {}, ts: new Date().toISOString() }
}

async function fresh() {
  vi.resetModules()
  const { useHandlingSpace } = await import('@/composables/useHandlingSpace')
  return useHandlingSpace()
}

describe('useHandlingSpace', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    listMock.mockReset().mockResolvedValue({ count: 0, items: [], persistent: false })
    detailMock.mockReset().mockRejectedValue(new Error('no space selected'))
    claimMock.mockReset()
    ackMock.mockReset()
    transferMock.mockReset()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('对账以台账为准：自动选中第一个空间并填充详情', async () => {
    listMock.mockResolvedValue({
      count: 1,
      items: [summary(SPACE_A, { tasks_total: 2, tasks_open: 1, tasks_closed: 1 })],
      persistent: true,
    })
    detailMock.mockResolvedValue(detailOf([task(STU_A), task(STU_B, { status: 'closed' })], { persistent: true }))
    const space = await fresh()
    space.refresh()
    await vi.waitFor(() => expect(space.tasks.value).toHaveLength(2))
    expect(space.currentSpaceId.value).toBe(SPACE_A)
    expect(space.statusCounts.value).toEqual({ pending: 1, closed: 1 })
    expect(space.persistent.value).toBe(true)
  })

  it('只认 handling.task.changed，且旧版本帧不把界面改回去', async () => {
    detailMock.mockResolvedValue(detailOf([task(STU_A, { version: 1 })]))
    const space = await fresh()
    space.select(SPACE_A)
    await vi.waitFor(() => expect(space.tasks.value[0]?.version).toBe(1))

    const events = ref<Record<string, unknown>[]>([])
    space.attachStream({ events: events as never, connected: ref(true) as never })
    // 新帧先到、旧帧后到：乱序到达的旧帧不得回退
    events.value = [taskEvent(SPACE_A, task(STU_A, { version: 3, status: 'claimed', assignee: 'duty-01' }))] as never
    await vi.waitFor(() => expect(space.tasks.value[0]?.version).toBe(3))
    events.value = [taskEvent(SPACE_A, task(STU_A, { version: 2, status: 'pending' }))] as never
    expect(space.tasks.value[0]?.version).toBe(3)
    events.value = [otherEvent()] as never
    expect(space.tasks.value[0]?.version).toBe(3)
    // 参与人由任务推导，认领后即出现
    expect(space.participants.value).toEqual(['duty-01'])
  })

  it('其他空间的事件只触发列表对账，不打当前详情', async () => {
    listMock.mockResolvedValue({ count: 1, items: [summary(SPACE_A)], persistent: false })
    detailMock.mockResolvedValue(detailOf([task(STU_A)]))
    const space = await fresh()
    space.refresh()
    await vi.waitFor(() => expect(space.spaces.value).toHaveLength(1))
    listMock.mockClear()

    const events = ref<Record<string, unknown>[]>([])
    space.attachStream({ events: events as never, connected: ref(true) as never })
    events.value = [taskEvent(SPACE_B, task(STU_B, { space_id: SPACE_B }))] as never
    await vi.advanceTimersByTimeAsync(450)
    expect(listMock).toHaveBeenCalledTimes(1)
  })

  it('409 说"已被他人处置"并重新对账，不拿旧视图覆盖', async () => {
    detailMock.mockResolvedValue(detailOf([task(STU_A)]))
    const { SpaceApiError } = await import('@/api/spaces')
    claimMock.mockRejectedValue(new SpaceApiError(409, '已被抢先', { detail: { code: 'E_TASK_STATE_CONFLICT' } }))
    const space = await fresh()
    space.select(SPACE_A)
    await vi.waitFor(() => expect(space.tasks.value).toHaveLength(1))

    const note = await space.claim(space.tasks.value[0]!)
    expect(note).toContain('已被他人处置')
    expect(space.versionConflict.value).toContain('已被他人处置')
    await vi.advanceTimersByTimeAsync(450)
    expect(listMock).toHaveBeenCalledTimes(1)
  })

  it('没选席位时按后端口径提示，不把 401 说成网络错误', async () => {
    detailMock.mockResolvedValue(detailOf([task(STU_A)]))
    const { SpaceApiError } = await import('@/api/spaces')
    ackMock.mockRejectedValue(new SpaceApiError(401, '需要 X-Aegis-Operator 席位头'))
    const space = await fresh()
    space.select(SPACE_A)
    await vi.waitFor(() => expect(space.tasks.value).toHaveLength(1))
    const note = await space.ack(space.tasks.value[0]!)
    expect(note).toContain('席位')
  })

  it('移交走 lead 口径，成功后本地任务就位', async () => {
    detailMock.mockResolvedValue(detailOf([task(STU_A)]))
    transferMock.mockResolvedValue({ task: task(STU_A, { assignee: 'duty-02', version: 2 }), space_id: SPACE_A })
    const space = await fresh()
    space.select(SPACE_A)
    await vi.waitFor(() => expect(space.tasks.value).toHaveLength(1))
    const note = await space.transfer(space.tasks.value[0]!, 'duty-02')
    expect(note).toBe('')
    expect(space.tasks.value[0]?.assignee).toBe('duty-02')
    expect(transferMock).toHaveBeenCalledWith(SPACE_A, STU_A, 'duty-02', 1)
  })
})
