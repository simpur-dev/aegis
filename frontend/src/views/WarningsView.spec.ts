/**
 * 预警发布页的口径与实时性。
 *
 * 三条都来自真机巡检，且都属于"界面没说谎、但读的人会看错"那一类：
 * ① mock 通道下"4/4 通道成功"与真实触达混成一句话（铁律 7 要求两种口径分开报）；
 * ② 详情里的任务单元靠回查最近 N 条链路得到，找不到就显示空列表——
 *    "取不到"与"确实没有"被写成同一句话；
 * ③ 只在挂载时取一次数，刚发布的预警不会自己出现。
 */

import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import api from '@/api/client'
import { fetchIntegrations } from '@/api/integrations'
import type { WarningRecord } from '@/api/types'
import WarningsView from '@/views/WarningsView.vue'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    default: { warnings: vi.fn(), events: vi.fn(), task: vi.fn(), ready: vi.fn() },
  }
})

vi.mock('@/api/integrations', () => ({ fetchIntegrations: vi.fn() }))

const mockedWarnings = vi.mocked(api.warnings)
const mockedEvents = vi.mocked(api.events)
const mockedTask = vi.mocked(api.task)
const mockedIntegrations = vi.mocked(fetchIntegrations)

function warning(overrides: Partial<WarningRecord> = {}): WarningRecord {
  return {
    warning_id: 'wrn_1',
    event_id: 'evt_1',
    trace_id: 'trc_1',
    hazard_type: 'debris_flow',
    region_codes: ['540121'],
    risk_level: 1,
    title_zh: '西藏泥石流预警（红色）',
    body_zh: '正文',
    body_bo: null,
    audiences: ['residents'],
    channels: ['sms', 'beidou'],
    deliveries: [
      { channel: 'sms', audience_count: 1, status: 'delivered', attempted_at: '2026-10-03T16:45:13.900Z', receipt_at: '2026-10-03T16:45:15.500Z', provider_msg_id: 'mock-x-sms' },
      { channel: 'beidou', audience_count: 1, status: 'failed', attempted_at: '', receipt_at: null, provider_msg_id: null },
    ],
    translation_pending: true,
    generated_at: '2026-10-03T16:45:13.840Z',
    released_at: '2026-10-03T16:45:14.000Z',
    ...overrides,
  } as WarningRecord
}

const STUBS = {
  'a-card': { name: 'ACard', props: ['title', 'loading', 'size'], template: '<div class="card"><slot /></div>' },
  // 替身必须把列头与 bodyCell 真渲染出来：这一页要验的正是"列头写的什么口径、
  // 单元格里算出的成功数对不对"，替身若是空壳就等于什么都没测。
  'a-table': {
    name: 'ATable',
    props: ['columns', 'dataSource', 'loading', 'pagination', 'rowKey', 'size'],
    template: `
      <div class="table">
        <div class="th" v-for="c in columns" :key="'h-' + c.key">{{ c.title }}</div>
        <div class="tr" v-for="(row, i) in dataSource" :key="'r-' + i">
          <div class="td" v-for="c in columns" :key="'c-' + c.key" :data-testid="'cell-' + c.key">
            <slot v-if="$slots.bodyCell" name="bodyCell" :column="c" :record="row" />
            <template v-else>{{ row[c.dataIndex] }}</template>
          </div>
        </div>
      </div>`,
  },
  'a-tag': { name: 'ATag', props: ['color'], template: '<span class="tag"><slot /></span>' },
  'a-space': { name: 'ASpace', props: ['wrap'], template: '<div class="space"><slot /></div>' },
  'a-button': { name: 'AButton', props: ['type', 'size'], template: '<button class="button"><slot /></button>' },
  'a-drawer': { name: 'ADrawer', props: ['open', 'width', 'title'], emits: ['update:open'], template: '<div class="drawer"><template v-if="open"><slot /></template></div>' },
  'a-descriptions': { name: 'ADescriptions', props: ['column', 'size', 'bordered'], template: '<div class="desc"><slot /></div>' },
  'a-descriptions-item': { name: 'ADescriptionsItem', props: ['label'], template: '<div class="desc-item">{{ label }}<slot /></div>' },
  'a-alert': { name: 'AAlert', props: ['message', 'type', 'showIcon'], template: '<div class="alert">{{ message }}</div>' },
  'a-empty': { name: 'AEmpty', props: ['description'], template: '<div class="empty">{{ description }}</div>' },
  'a-spin': { name: 'ASpin', props: ['spinning'], template: '<div class="spin"><slot /></div>' },
}

function headerText(wrapper: ReturnType<typeof mountView>, needle: string): string {
  const hit = wrapper.findAll('.th').find((n) => n.text().includes(needle))
  return hit ? hit.text() : ''
}

function mountView() {
  return mount(WarningsView, { global: { stubs: STUBS } })
}

beforeEach(() => {
  vi.stubGlobal('message', { error: vi.fn(), success: vi.fn() })
  mockedWarnings.mockResolvedValue({ items: [warning()] } as never)
  mockedIntegrations.mockResolvedValue({ items: [{ name: 'delivery', enabled: true, driver: 'mock', detail: {} }] } as never)
  mockedEvents.mockResolvedValue({ items: [] } as never)
  mockedTask.mockResolvedValue(null as never)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

describe('触达口径要分开报', () => {
  it('mock 通道下列名写"演练口径"，不让人把演练数字读成现场触达', async () => {
    const wrapper = mountView()
    await flushPromises()
    expect(headerText(wrapper, '触达')).toBe('触达（演练口径）')
  })

  it('接了真实网关后同一列改回"触达"：口径跟着状态面那条腿走，不是写死的', async () => {
    mockedIntegrations.mockResolvedValue({ items: [{ name: 'delivery', enabled: true, driver: 'http', detail: {} }] } as never)
    const wrapper = mountView()
    await flushPromises()
    expect(headerText(wrapper, '触达')).toBe('触达')
  })

  it('成功数取的是回执状态，不是通道总数（1 成功 1 失败要显示 1/2）', async () => {
    const wrapper = mountView()
    await flushPromises()
    expect(wrapper.find('[data-testid="cell-reach"]').text()).toBe('1/2 通道成功')
  })
})

describe('详情里的任务单元：取不到与没有是两件事', () => {
  it('最近链路窗口里找不到这条预警时，明说"无从判断"而不是显示空列表', async () => {
    const wrapper = mountView()
    await flushPromises()
    await wrapper.find('.button').trigger('click')
    await flushPromises()
    const notice = wrapper.find('[data-testid="tasks-notice"]')
    expect(notice.exists()).toBe(true)
    expect(notice.text()).toContain('无从判断')
    expect(wrapper.find('[data-testid="tasks-empty"]').exists()).toBe(false)
  })

  it('链路确实在窗口内、但没产出任务单元时，才说"没有产出"', async () => {
    mockedEvents.mockResolvedValue({ items: [{ warning_id: 'wrn_1', task_units: [] }] } as never)
    const wrapper = mountView()
    await flushPromises()
    await wrapper.find('.button').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="tasks-notice"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="tasks-empty"]').text()).toContain('没有产出任务单元')
  })

  it('部分任务单元取不回来时说明缺几个，不静默少列', async () => {
    mockedEvents.mockResolvedValue({ items: [{ warning_id: 'wrn_1', task_units: ['stu_1', 'stu_2'] }] } as never)
    mockedTask.mockImplementation((async (id: string) => (id === 'stu_1' ? { task_unit_id: 'stu_1', objective: '转移' } : null)) as never)
    const wrapper = mountView()
    await flushPromises()
    await wrapper.find('.button').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="tasks-notice"]').text()).toContain('1 个取不回来')
  })
})

describe('详情抽屉里的时间一律 UTC+8', () => {
  it('发布时刻与回执时刻不给裸 ISO（表格里改过，抽屉里是同一件事的另一半）', async () => {
    mockedEvents.mockResolvedValue({ items: [{ warning_id: 'wrn_1', task_units: [] }] } as never)
    const wrapper = mountView()
    await flushPromises()
    await wrapper.find('[data-testid="cell-action"] .button').trigger('click')
    await flushPromises()
    const text = wrapper.find('.drawer').text()
    expect(text).toContain('2026-10-04 00:45:14')  // 发布时刻（UTC+8，跨日进位）
    expect(text).toContain('2026-10-04 00:45:15')  // 回执时刻
    expect(text).not.toMatch(/\d{2}:\d{2}:\d{2}\.\d{3}Z/)
  })

  it('没有回执的通道写"无回执"，不留一个空格让人以为渲染坏了', async () => {
    mockedEvents.mockResolvedValue({ items: [{ warning_id: 'wrn_1', task_units: [] }] } as never)
    const wrapper = mountView()
    await flushPromises()
    await wrapper.find('[data-testid="cell-action"] .button').trigger('click')
    await flushPromises()
    expect(wrapper.find('.drawer').text()).toContain('无回执')
  })
})

describe('列表要会自己更新', () => {
  it('挂载后每 15 秒重新取一次预警', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    try {
      mountView()
      await flushPromises()
      const first = mockedWarnings.mock.calls.length
      await vi.advanceTimersByTimeAsync(15_000)
      await flushPromises()
      expect(mockedWarnings.mock.calls.length).toBeGreaterThan(first)
    } finally {
      vi.useRealTimers()
    }
  })
})
