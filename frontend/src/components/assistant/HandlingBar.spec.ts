/**
 * HandlingBar：呈现与处置的最小组件测试。
 *
 * 状态机在 `useHandlingSpace`（另有 spec），这里钉住三件事：
 * - 没有空间时空态要说明"预警落地后自动建立"，不是一块空白；
 * - 认领/移交按钮按任务状态出现，点完按钮本地任务就位（不在组件里再写一份状态机）；
 * - 版本冲突与席位缺失的话术原样透出——那两句是后端口径，改了就与后端漂移。
 */

import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import HandlingBar from '@/components/assistant/HandlingBar.vue'
import { resetHandlingSpace } from '@/composables/useHandlingSpace'
import type { HandlingTaskDto, SpaceDetailDto, SpaceSummaryDto } from '@/api/spaces'

const SPACE_A = 'wrn_aaaaaaaaaaaa'
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

function byTestid(wrapper: ReturnType<typeof mount>, id: string) {
  const el = wrapper.find(`[data-testid="${id}"]`)
  expect(el.exists(), `找不到 data-testid=${id}`).toBe(true)
  return el
}

describe('HandlingBar', () => {
  beforeEach(() => {
    listMock.mockReset().mockResolvedValue({ count: 0, items: [], persistent: false })
    detailMock.mockReset().mockRejectedValue(new Error('no space selected'))
    claimMock.mockReset()
    ackMock.mockReset()
    transferMock.mockReset()
    resetHandlingSpace()
  })

  it('没有空间时给空态说明，而不是空白一块', async () => {
    const wrapper = mount(HandlingBar)
    await flushPromises()
    expect(byTestid(wrapper, 'space-empty').text()).toContain('一预警一空间')
    expect(wrapper.find('[data-testid="space-tasks"]').exists()).toBe(false)
  })

  it('任务按状态给按钮：认领后本地就位，回执按钮出现', async () => {
    listMock.mockResolvedValue({ count: 1, items: [summary(SPACE_A, { tasks_total: 1, tasks_open: 1 })], persistent: false })
    detailMock.mockResolvedValue(detailOf([task(STU_A), task(STU_B, { status: 'closed' })]))
    claimMock.mockResolvedValue({ task: task(STU_A, { status: 'claimed', assignee: 'duty-01', version: 2 }), space_id: SPACE_A })

    const wrapper = mount(HandlingBar)
    await flushPromises()
    expect(byTestid(wrapper, 'space-facts').text()).toContain('任务 2')
    // 已关闭的任务不给任何处置按钮
    const closedRow = wrapper.findAll('[data-testid="space-task"]')[1]!
    expect(closedRow.find('[data-testid="task-claim"]').exists()).toBe(false)
    expect(closedRow.find('[data-testid="task-transfer"]').exists()).toBe(false)

    await byTestid(wrapper, 'task-claim').trigger('click')
    await flushPromises()
    expect(claimMock).toHaveBeenCalledWith(SPACE_A, STU_A, 1)
    await flushPromises()
    expect(wrapper.findAll('[data-testid="space-task"]')[0]!.text()).toContain('已认领')
    expect(wrapper.findAll('[data-testid="space-task"]')[0]!.find('[data-testid="task-ack"]').exists()).toBe(true)
  })

  it('移交要填受理席位；不填就地拦下', async () => {
    listMock.mockResolvedValue({ count: 1, items: [summary(SPACE_A, { tasks_total: 1, tasks_open: 1 })], persistent: false })
    detailMock.mockResolvedValue(detailOf([task(STU_A)]))
    const wrapper = mount(HandlingBar)
    await flushPromises()

    await byTestid(wrapper, 'task-transfer').trigger('click')
    await byTestid(wrapper, 'task-transfer-confirm').trigger('click')
    await flushPromises()
    expect(byTestid(wrapper, 'space-message').text()).toContain('受理席位')
    expect(transferMock).not.toHaveBeenCalled()

    await byTestid(wrapper, 'task-transfer-target').setValue('lead-01')
    transferMock.mockResolvedValue({ task: task(STU_A, { assignee: 'lead-01', version: 2 }), space_id: SPACE_A })
    await byTestid(wrapper, 'task-transfer-confirm').trigger('click')
    await flushPromises()
    expect(transferMock).toHaveBeenCalledWith(SPACE_A, STU_A, 'lead-01', 1)
  })
})
