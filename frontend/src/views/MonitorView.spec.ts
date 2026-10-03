import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import api from '@/api/client'
import type { TelemetryReading } from '@/api/types'
import MonitorView from '@/views/MonitorView.vue'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return { default: { telemetry: vi.fn(), drill: vi.fn() }, ApiError: actual.ApiError }
})

const mockedTelemetry = vi.mocked(api.telemetry)

const STUBS = {
  EChart: true,
  'a-card': { name: 'ACard', props: ['title', 'loading', 'size'], template: '<div class="card"><slot /><slot name="extra" /></div>' },
  'a-space': { name: 'ASpace', props: ['wrap'], template: '<div class="space"><slot /></div>' },
  'a-select': { name: 'ASelect', props: ['value', 'placeholder'], emits: ['update:value'], template: '<div class="select"><slot /></div>' },
  'a-select-option': { name: 'ASelectOption', props: ['value'], template: '<span class="option"><slot /></span>' },
  'a-button': { name: 'AButton', props: ['loading', 'type', 'disabled'], template: '<button class="button" :disabled="disabled"><slot /></button>' },
  'a-row': { name: 'ARow', props: ['gutter'], template: '<div class="row"><slot /></div>' },
  'a-col': { name: 'ACol', props: ['span'], template: '<div class="col"><slot /></div>' },
  'a-table': { name: 'ATable', props: ['columns', 'dataSource', 'pagination', 'rowKey', 'size', 'scroll'], template: '<div class="table" />' },
  'a-tag': { name: 'ATag', props: ['color'], template: '<span class="tag"><slot /></span>' },
  'a-modal': {
    name: 'AModal',
    props: ['open', 'title', 'footer', 'width'],
    emits: ['update:open'],
    template: '<div class="modal"><slot v-if="open" /></div>',
  },
  ReportForm: { name: 'ReportForm', props: ['initialRegionCode', 'initialReporter'], template: '<div class="report-form" />' },
}

const READING: TelemetryReading = {
  station_id: 'RG-01',
  metric: 'rain_10min',
  value: 95,
  unit: 'mm',
  region_code: '540121',
  observed_at: '2026-10-03T04:00:00Z',
  ingested_at: '2026-10-03T04:00:01Z',
  source: 'mqtt',
  quality_flag: 'ok',
}

function mountView() {
  return mount(MonitorView, { global: { stubs: STUBS } })
}

beforeEach(() => {
  mockedTelemetry.mockReset().mockResolvedValue({ count: 1, items: [READING] })
})

describe('监测页的实时性：标题写着"最新"，数据就不能冻在打开那一刻', () => {
  function withTimers() {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
  }

  /**
   * 表格必须在自己家横向滚动，而不是把整页撑宽。
   *
   * 真机 900px 宽（值班指挥员的平板）走过一次：遥测明细最后两列"观测时刻 + 接入时延"
   * 被推到 931–975px 处，页面出现横向滚动，右侧内容看不全——而这一页没有任何提示。
   * 布局本身测不出来（jsdom 没有排版），这里钉的是"声明了内部滚动"这一条前提。
   */
  it('遥测明细声明了内部横向滚动，窄屏不把整页撑宽', () => {
    const wrapper = mountView()
    const table = wrapper.findComponent({ name: 'ATable' })
    expect(table.props('scroll')).toEqual({ x: 'max-content' })
  })

  it('每 15 秒自己再取一次遥测', async () => {
    withTimers()
    try {
      mountView()
      await flushPromises()
      const first = mockedTelemetry.mock.calls.length
      await vi.advanceTimersByTimeAsync(15_000)
      await flushPromises()
      expect(mockedTelemetry.mock.calls.length).toBeGreaterThan(first)
    } finally {
      vi.useRealTimers()
    }
  })

  it('离开页面就停表：切走了还在打接口是白耗', async () => {
    withTimers()
    try {
      const wrapper = mountView()
      await flushPromises()
      wrapper.unmount()
      const atUnmount = mockedTelemetry.mock.calls.length
      await vi.advanceTimersByTimeAsync(60_000)
      expect(mockedTelemetry.mock.calls.length).toBe(atUnmount)
    } finally {
      vi.useRealTimers()
    }
  })

  it('页签在后台时跳过这一轮（不是把定时器关掉）', async () => {
    withTimers()
    try {
      const original = document.visibilityState
      Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true })
      mountView()
      await flushPromises()
      const first = mockedTelemetry.mock.calls.length
      await vi.advanceTimersByTimeAsync(45_000)
      expect(mockedTelemetry.mock.calls.length).toBe(first)
      Object.defineProperty(document, 'visibilityState', { value: original, configurable: true })
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('演练按钮的重复点击防护', () => {
  /** 演练接口的替身：挂住不返回，模拟"正在跑"的那几秒。 */
  function hangingDrill() {
    let release: ((value: unknown) => void) | null = null
    vi.mocked(api.drill).mockReturnValue(new Promise((resolve) => { release = resolve }) as never)
    return () => release?.({ ingest: { readings: 0 }, regions: 0, chains: [] })
  }

  it('一次演练没跑完之前，再点不会重复发请求', async () => {
    const release = hangingDrill()
    const wrapper = mountView()
    await flushPromises()
    const surge = wrapper.find('[data-testid="drill-surge"]')
    await surge.trigger('click')
    await surge.trigger('click')
    await surge.trigger('click')
    expect(vi.mocked(api.drill)).toHaveBeenCalledTimes(1)
    release()
    await flushPromises()
  })

  it('跑完之前按钮是禁用的（不是只转个圈：转圈不挡点击）', async () => {
    const release = hangingDrill()
    const wrapper = mountView()
    await flushPromises()
    await wrapper.find('[data-testid="drill-surge"]').trigger('click')
    expect(wrapper.find('[data-testid="drill-surge"]').attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-testid="drill-normal"]').attributes('disabled')).toBeDefined()
    release()
    await flushPromises()
    expect(wrapper.find('[data-testid="drill-surge"]').attributes('disabled')).toBeUndefined()
  })
})

describe('监测页的人工上报入口（B5）', () => {
  it('按钮在，点开才挂表单：不点弹窗就不该把上报接口打出去', async () => {
    const wrapper = mountView()
    await flushPromises()
    expect(wrapper.find('.report-form').exists()).toBe(false)
    await wrapper.find('[data-testid="open-report"]').trigger('click')
    await flushPromises()
    expect(wrapper.findComponent({ name: 'ReportForm' }).exists()).toBe(true)
  })

  it('当前筛选的区域带进表单，上报人不必再抄一遍区划码', async () => {
    const wrapper = mountView()
    await flushPromises()
    // 区域选择器是本页第一个 a-select（第二个是指标）
    wrapper.findAllComponents({ name: 'ASelect' })[0]?.vm.$emit('update:value', '540121')
    await flushPromises()
    await wrapper.find('[data-testid="open-report"]').trigger('click')
    await flushPromises()
    expect(wrapper.findComponent({ name: 'ReportForm' }).props('initialRegionCode')).toBe('540121')
  })

  it('打开上报弹窗不影响监测读数与演练入口（同页共存，不是替换）', async () => {
    const wrapper = mountView()
    await flushPromises()
    expect(mockedTelemetry).toHaveBeenCalledWith({ limit: 2_000 })
    await wrapper.find('[data-testid="open-report"]').trigger('click')
    await flushPromises()
    const labels = wrapper.findAll('.button').map((button) => button.text())
    expect(labels).toEqual(expect.arrayContaining(['刷新', '人工上报', '发起灾害演练（激增）', '发起背景演练（正常）']))
  })
})
