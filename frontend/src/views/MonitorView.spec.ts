import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'

import api, { type TelemetryQuery } from '@/api/client'
import mapApi from '@/api/map'
import type { TelemetryReading } from '@/api/types'
import MonitorView from '@/views/MonitorView.vue'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return { default: { telemetry: vi.fn(), drill: vi.fn() }, ApiError: actual.ApiError }
})

// 区域下拉的底仓来自锚点清单：不 mock 掉，用例就会去发真 XHR
vi.mock('@/api/map', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/map')>()
  return { ...actual, default: { ...actual.default, regionAnchors: vi.fn() } }
})

const mockedTelemetry = vi.mocked(api.telemetry)
const mockedAnchors = vi.mocked(mapApi.regionAnchors)

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
  mockedAnchors.mockReset().mockResolvedValue({
    items: [
      { code: '540121', name_zh: '拉萨市辖区', lon: 91.1, lat: 29.65 },
      { code: '540700', name_zh: '那曲市', lon: 92.0, lat: 31.5 },
    ],
  } as never)
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
    // 两条各管一件事：不筛的取样给下拉当底仓，带参数的那条才是页面在显示的内容
    expect(mockedTelemetry).toHaveBeenCalledWith({ limit: 2_000 })
    expect(mockedTelemetry).toHaveBeenCalledWith({ limit: 2_000, metric: 'rain_10min' })
    await wrapper.find('[data-testid="open-report"]').trigger('click')
    await flushPromises()
    const labels = wrapper.findAll('.button').map((button) => button.text())
    expect(labels).toEqual(expect.arrayContaining(['刷新', '人工上报', '发起灾害演练（激增）', '发起背景演练（正常）']))
  })
})

/**
 * 筛选语义：区域与指标是**查询参数**，不是"最新 2000 条里的本地过滤器"。
 *
 * 真机量出来的问题：台账里 50,000 条读数，本地窗口只装 2,000 条，
 * 而窗口里只出现过 3 个区域——其余区域的下拉项压根不存在，
 * 页面表现就是"这个区域没有遥测"，可后端按区域单查能填满 2,000 条。
 * 一张图页一直是发参数查的，两页同一个控件却是两种语义。
 */
describe('监测页的筛选是后端查询', () => {
  function selectPair(wrapper: ReturnType<typeof mountView>) {
    const [regionSelect, metricSelect] = wrapper.findAllComponents({ name: 'ASelect' })
    if (!regionSelect || !metricSelect) throw new Error('两个下拉没渲染出来')
    return [regionSelect, metricSelect] as const
  }

  it('选区域后发的是 region_code 查询参数', async () => {
    const wrapper = mountView()
    await flushPromises()
    const [regionSelect] = selectPair(wrapper)
    mockedTelemetry.mockClear()
    regionSelect.vm.$emit('update:value', '540221')
    await flushPromises()
    expect(mockedTelemetry).toHaveBeenCalledWith({ limit: 2_000, region_code: '540221', metric: 'rain_10min' })
  })

  it('换指标也发查询参数，不再靠本地挑', async () => {
    const wrapper = mountView()
    await flushPromises()
    const [, metricSelect] = selectPair(wrapper)
    mockedTelemetry.mockClear()
    metricSelect.vm.$emit('update:value', 'debris_level')
    await flushPromises()
    expect(mockedTelemetry).toHaveBeenCalledWith({ limit: 2_000, metric: 'debris_level' })
  })

  it('区域下拉列出锚点里的全部区域，不只是窗口里出现过的', async () => {
    const wrapper = mountView()
    await flushPromises()
    const options = wrapper.findAllComponents({ name: 'ASelectOption' }).map((option) => String(option.props('value')))
    // 540700 在读数里从没出现过（READING 只有 540121），但它是在册区域：以前这种区域选不到
    expect(options).toContain('540700')
  })

  /**
   * 翻页后换筛选条件，页码不能留在原地。
   *
   * antd 表格在数据换一批时不会自己把 `current` 拉回第 1 页：今天每个区域都有
   * 2,000 条读数（真的存在第 200 页），翻到很远再换成一个只有几十条的指标，
   * 表格就会停在一个不存在的页上——一行都没有，也不说为什么，
   * 读的人只能得出"这个区域这个指标没数据"。
   */
  it('翻页是受控的，换筛选条件时回到第 1 页', async () => {
    const wrapper = mountView()
    await flushPromises()
    const table = wrapper.findComponent({ name: 'ATable' })
    table.vm.$emit('change', { current: 180 }, {}, {})
    await nextTick()
    expect((table.props('pagination') as { current: number }).current).toBe(180)

    const [regionSelect] = selectPair(wrapper)
    regionSelect.vm.$emit('update:value', '540221')
    await flushPromises()
    expect((table.props('pagination') as { current: number }).current).toBe(1)
  })

  /**
   * 连点两个区域时，先发出的那个更慢回来是常态而不是意外。
   * 没有序号守卫的话，慢回来的旧数据会盖掉新筛选的结果——
   * 下拉显示 B 区域，表格里却是 A 区域的读数。
   */
  it('迟到的旧筛选响应不能盖掉新筛选的结果', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    try {
      mockedTelemetry.mockImplementation((query: TelemetryQuery = {}) => {
        if (query.region_code === 'SLOW') {
          return new Promise((resolve) => {
            setTimeout(() => resolve({ count: 1, items: [{ ...READING, station_id: 'ST-慢区' }] }), 400)
          })
        }
        return Promise.resolve({ count: 1, items: [{ ...READING, station_id: 'ST-快区' }] })
      })
      const wrapper = mountView()
      await flushPromises()
      const [regionSelect] = selectPair(wrapper)
      regionSelect.vm.$emit('update:value', 'SLOW')
      await flushPromises()
      regionSelect.vm.$emit('update:value', 'FAST')
      await flushPromises()
      await vi.advanceTimersByTimeAsync(800)
      await flushPromises()
      const rows = wrapper.findComponent({ name: 'ATable' }).props('dataSource') as TelemetryReading[]
      expect(rows.map((row) => row.station_id)).toEqual(['ST-快区'])
    } finally {
      vi.useRealTimers()
    }
  })
})

/**
 * 台账表格（每页 10 条）与时序图共用同一个 `readings` 数组，而接口给的是**旧→新**：
 * 500 条窗口下第一页摆的是最旧的 10 条，最新那条要翻到第 49 页才看到——
 * 这张页的名字是"实时监测"，值班员刷新后想看的是刚刚那一次读数。
 *
 * 但时间轴反过来是对的（左到右就该是旧到新），所以不能整体翻转 `readings`，
 * 只能给表格单独排一份。三条用例分别钉住：表格倒序、图表正序、列头写明口径。
 */
describe('监测台账要最新在上，而时序图不许跟着翻', () => {
  const oldest = { ...READING, observed_at: '2026-10-03T02:00:00Z', value: 1 }
  const middle = { ...READING, observed_at: '2026-10-03T04:00:00Z', value: 2 }
  const newest = { ...READING, observed_at: '2026-10-03T06:00:00Z', value: 3 }

  function threeRows() {
    mockedTelemetry.mockResolvedValue({ count: 3, items: [oldest, middle, newest] } as never)
  }

  it('台账第一行是最新那条读数', async () => {
    threeRows()
    const wrapper = mountView()
    await flushPromises()
    const rows = wrapper.findComponent({ name: 'ATable' }).props('dataSource') as TelemetryReading[]
    expect(rows.map((row) => row.observed_at)).toEqual(['2026-10-03T06:00:00Z', '2026-10-03T04:00:00Z', '2026-10-03T02:00:00Z'])
  })

  it('时序图仍按旧→新排：翻表格不能把时间轴也翻过去', async () => {
    threeRows()
    const wrapper = mountView()
    await flushPromises()
    const option = wrapper.findComponent({ name: 'EChart' }).props('option') as {
      xAxis: { data: string[] }
      series: Array<{ data: Array<number | null> }>
    }
    // 轴与数据要成对判：只查 series 时，把 xAxis 翻过去的改动能躲过这条用例（实测躲过一次）。
    // operatingClock 是 UTC+8 的时分秒：02:00Z→10:00:00、04:00Z→12:00:00、06:00Z→14:00:00。
    expect(option.xAxis.data).toEqual(['10:00:00', '12:00:00', '14:00:00'])
    expect(option.series[0].data).toEqual([1, 2, 3])
  })

  it('列头写明"最新在上"：读的人不用先猜这张表是怎么排的', async () => {
    threeRows()
    const wrapper = mountView()
    await flushPromises()
    const columns = wrapper.findComponent({ name: 'ATable' }).props('columns') as Array<{ key: string; title: string }>
    expect(columns.find((column) => column.key === 'observed_at')?.title).toContain('最新在上')
  })
})
