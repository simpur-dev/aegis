/**
 * 态势总览的取数与新鲜度契约。
 *
 * 这个视图此前一份 spec 都没有，于是两个缺陷一路活到真机巡检：
 * ① 只 `onMounted` 取一次数，既不轮询也没有刷新按钮——预警发出去了数字还冻在加载那一刻，
 *    而同页的 SSE 面板一直在动，看起来"页面是活的"，没人会怀疑四个大数字；
 * ② "已发布预警"取的是 `warnings.length`，而那份列表是按 `limit: 50` 拉的——
 *    过 50 条之后这个数字永远显示 50，且没有任何异常迹象。
 * 两条都在这里钉住，另加"取数失败必须留在页面上"和"SSE 时间按 UTC+8 显示"。
 */

import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'

import api from '@/api/client'
import DashboardView from '@/views/DashboardView.vue'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    default: {
      ready: vi.fn(),
      agents: vi.fn(),
      warnings: vi.fn(),
      events: vi.fn(),
      latency: vi.fn(),
    },
  }
})

vi.mock('@/composables/useEventStream', () => ({
  // 必须是真 ref：模板的自动解包只认 `isRef`，给 `{value: [...]}` 会在 `events.slice` 上炸
  useEventStream: () => ({
    events: ref([{ trace_id: 'trc_1', ts: '2026-10-03T16:45:13.840Z', subject: 'warning.published', payload: { title: '西藏滑坡预警（红色）' } }]),
  }),
}))

const mocked = vi.mocked(api, true) as unknown as Record<string, ReturnType<typeof vi.fn>>

function responses(overrides: { warning_count?: number; warnings?: number } = {}) {
  const warningCount = overrides.warning_count ?? 6
  mocked.ready.mockResolvedValue({ agents_online: 5, store: { warning_count: warningCount, task_count: 28, chain_count: 6, telemetry_count: 100 } } as never)
  mocked.agents.mockResolvedValue({ items: [] } as never)
  mocked.warnings.mockResolvedValue({ items: Array.from({ length: overrides.warnings ?? warningCount }, (_, i) => ({ warning_id: `wrn_${i}` })) } as never)
  mocked.events.mockResolvedValue({ items: [] } as never)
  mocked.latency.mockResolvedValue({ metrics: {}, sla_thresholds: {}, collaboration: {}, violations: {} } as never)
}

const STUBS = {
  'a-card': { name: 'ACard', props: ['title', 'loading', 'size'], template: '<div class="card"><slot /></div>' },
  'a-row': { name: 'ARow', props: ['gutter'], template: '<div class="row"><slot /></div>' },
  'a-col': { name: 'ACol', props: ['span'], template: '<div class="col"><slot /></div>' },
  'a-statistic': { name: 'AStatistic', props: ['title', 'value', 'suffix'], template: '<div class="stat" :data-testid="`stat-${title}`">{{ value }}{{ suffix }}</div>' },
  'a-tag': { name: 'ATag', props: ['color'], template: '<span class="tag"><slot /></span>' },
  'a-table': { name: 'ATable', props: ['columns', 'dataSource', 'pagination', 'rowKey', 'size'], template: '<div class="table"></div>' },
  'a-empty': { name: 'AEmpty', props: ['description'], template: '<div class="empty">{{ description }}</div>' },
  'a-button': { name: 'AButton', props: ['loading', 'size'], template: '<button class="button" :disabled="loading"><slot /></button>' },
  'a-alert': { name: 'AAlert', props: ['message', 'type', 'showIcon'], template: '<div class="alert">{{ message }}</div>' },
  'a-timeline': { name: 'ATimeline', template: '<div class="tl"><slot /></div>' },
  'a-timeline-item': { name: 'ATimelineItem', props: ['color'], template: '<div class="tl-item"><slot /></div>' },
}

beforeEach(() => {
  vi.stubGlobal('message', { error: vi.fn(), success: vi.fn() })
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('态势总览的取数', () => {
  it('预警数取后端口径的总数，不取被 limit 截断的列表长度', () => {
    // 后端一共 137 条，列表按 limit 只回了 50 条：取长度就会永远显示 50
    responses({ warning_count: 137, warnings: 50 })
    const wrapper = mount(DashboardView, { global: { stubs: STUBS } })
    return flushPromises().then(() => {
      expect(wrapper.find('[data-testid="stat-已发布预警"]').text()).toContain('137')
    })
  })

  it('挂载后每 15 秒自己再取一次：数字不能冻在加载那一刻', async () => {
    responses()
    mount(DashboardView, { global: { stubs: STUBS } })
    await flushPromises()
    const first = mocked.ready.mock.calls.length
    await vi.advanceTimersByTimeAsync(15_000)
    await flushPromises()
    expect(mocked.ready.mock.calls.length).toBeGreaterThan(first)
  })

  it('卸载就停表：离开这页还继续打接口是白耗', async () => {
    responses()
    const wrapper = mount(DashboardView, { global: { stubs: STUBS } })
    await flushPromises()
    wrapper.unmount()
    const atUnmount = mocked.ready.mock.calls.length
    await vi.advanceTimersByTimeAsync(45_000)
    expect(mocked.ready.mock.calls.length).toBe(atUnmount)
  })

  it('页面上有"更新于"与刷新按钮：读数字的人得知道它是几点取的', async () => {
    responses()
    const wrapper = mount(DashboardView, { global: { stubs: STUBS } })
    await flushPromises()
    expect(wrapper.find('[data-testid="dashboard-updated"]').text()).toContain('更新于')
    expect(wrapper.find('[data-testid="dashboard-updated"]').text()).toContain('UTC+8')
    const before = mocked.ready.mock.calls.length
    await wrapper.find('[data-testid="dashboard-refresh"]').trigger('click')
    await flushPromises()
    expect(mocked.ready.mock.calls.length).toBeGreaterThan(before)
  })

  it('取数失败时把原因留在页面上，而不是只弹一条三秒就消失的提示', async () => {
    mocked.ready.mockRejectedValue(new Error('502 upstream down'))
    mocked.agents.mockRejectedValue(new Error('502 upstream down'))
    mocked.warnings.mockRejectedValue(new Error('502'))
    mocked.events.mockRejectedValue(new Error('502'))
    mocked.latency.mockRejectedValue(new Error('502'))
    const wrapper = mount(DashboardView, { global: { stubs: STUBS } })
    await flushPromises()
    const alert = wrapper.find('[data-testid="dashboard-error"]')
    expect(alert.exists()).toBe(true)
    expect(alert.text()).toContain('502')
    expect(alert.text()).toContain('上一次成功取到')
  })

  it('SSE 事件时间按 UTC+8 显示，不给裸 ISO（跨日要进位）', async () => {
    responses()
    const wrapper = mount(DashboardView, { global: { stubs: STUBS } })
    await flushPromises()
    const timeline = wrapper.find('.tl-item')
    expect(timeline.text()).toContain('2026-10-04 00:45:13')
    expect(timeline.text()).not.toMatch(/\d{2}:\d{2}:\d{2}\.\d+Z/)
  })
})
