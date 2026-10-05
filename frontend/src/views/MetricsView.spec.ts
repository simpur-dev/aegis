/**
 * 指标页的三条契约：读不到就写「—」；交给表格与图的数据带着正确单位；
 * 模板取的是中性键名并按行单位格式化。
 *
 * 前一条是夜里 500 巡检暴露的：接口挂了，页面仍写着"协同事务总数 0 / 越限项数 0"——
 * 一行会让人安心的假消息。后两条钉的是这条链上出过一次的 1000 倍误读
 * （`ingest_end_to_end_seconds` 的 0.015 秒曾被键名说成毫秒）。
 * "数字 + 单位"怎么拼是 `utils/metricUnits` 的 spec 负责，这里不重复实现一遍。
 */

import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import api from '@/api/client'
import MetricsView from '@/views/MetricsView.vue'
import source from './MetricsView.vue?raw'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return { ...actual, default: { latency: vi.fn() } }
})

const mockedLatency = vi.mocked(api.latency)

const STUBS = {
  'a-card': { name: 'ACard', props: ['title', 'loading', 'size'], template: '<div class="card"><slot name="extra" /><slot /></div>' },
  'a-alert': { name: 'AAlert', props: ['message', 'description', 'type'], template: '<div class="alert">{{ message }}{{ description }}</div>' },
  'a-button': { name: 'AButton', props: ['size'], template: '<button class="button"><slot /></button>' },
  'a-row': { name: 'ARow', props: ['gutter'], template: '<div class="row"><slot /></div>' },
  'a-col': { name: 'ACol', props: ['span'], template: '<div class="col"><slot /></div>' },
  'a-statistic': {
    name: 'AStatistic',
    props: ['title', 'value', 'suffix'],
    template: '<div class="stat" :data-testid="`stat-${title}`">{{ value }}{{ suffix }}</div>',
  },
  'a-tag': { name: 'ATag', props: ['color'], template: '<span class="tag"><slot /></span>' },
  'a-table': { name: 'ATable', props: ['dataSource', 'columns', 'pagination', 'rowKey', 'size'], template: '<div class="table" />' },
  EChart: { name: 'EChart', props: ['option', 'height'], template: '<div class="chart" />' },
}

function report() {
  return {
    sla_thresholds: {},
    metrics: {
      ingest_end_to_end_seconds: { count: 476, unit: 's', p50: 0.008, p95: 0.015, p99: 0.02, max: 0.025, mean: 0.01, budget: 300, breaches: 0, breach_rate: 0 },
      warning_generation_ms: { count: 3, unit: 'ms', p50: 4200, p95: 5200, p99: 5300, max: 5400, mean: 4500, budget: 180000, breaches: 0, breach_rate: 0 },
      ingest_publish_ms: { count: 476, unit: 'ms', p50: 3.5, p95: 12.4, p99: 20, max: 25, mean: 5 },
      collab_txn: { count: 3, unit: 'ms', p50: 27.2, p95: 27.3, p99: 27.3, max: 27.3, mean: 21.9, budget: 10000, breaches: 0 },
    },
    collaboration: { transactions: 3, success_rate: 1, target: 0.9, pass: true },
    violations: {},
    gateway_counters: {},
    store: {},
  }
}

beforeEach(() => {
  mockedLatency.mockReset()
})

function rowsGivenToTable(wrapper: ReturnType<typeof mount>): Array<Record<string, unknown>> {
  const stub = wrapper.findComponent({ name: 'ATable' })
  expect(stub.exists(), '表格外壳没渲染出来：这一页的单位口径就没被测到').toBe(true)
  return (stub.props('dataSource') ?? []) as Array<Record<string, unknown>>
}

function chartSeries(wrapper: ReturnType<typeof mount>): Array<{ name: string; data: number[] }> {
  const stub = wrapper.findComponent({ name: 'EChart' })
  expect(stub.exists(), '图表外壳没渲染出来：毫秒轴换算这条路没被走到').toBe(true)
  return (stub.props('option') as { series: Array<{ name: string; data: number[] }> }).series
}

describe('指标页读数', () => {
  it('读不到账本时三个统计位都写 —，不写 0', async () => {
    mockedLatency.mockRejectedValue(new Error('Request failed with status code 500'))
    const wrapper = mount(MetricsView, { global: { stubs: STUBS } })
    await flushPromises()
    expect(wrapper.find('[data-testid="stat-协同事务总数"]').text()).toBe('—')
    expect(wrapper.find('[data-testid="stat-协同成功率"]').text()).toBe('—')
    expect(wrapper.find('[data-testid="stat-越限项数"]').text()).toBe('—')
  })

  it('每一行带着自己的单位交给表格，秒制与毫秒各行其是', async () => {
    mockedLatency.mockResolvedValue(report() as never)
    const wrapper = mount(MetricsView, { global: { stubs: STUBS } })
    await flushPromises()
    const rows = rowsGivenToTable(wrapper)
    expect(rows.find((row) => row.name === 'ingest_end_to_end_seconds')).toMatchObject({
      unit: 's',
      p95: 0.015,
      budget: 300,
      pass: true,
    })
    expect(rows.find((row) => row.name === 'warning_generation_ms')).toMatchObject({
      unit: 'ms',
      p95: 5200,
      budget: 180000,
      pass: true,
    })
    expect(new Set(rows.map((row) => row.unit))).toEqual(new Set(['s', 'ms']))
  })

  it('没设阈值的指标 pass 是 null，不是"达标"', async () => {
    mockedLatency.mockResolvedValue(report() as never)
    const wrapper = mount(MetricsView, { global: { stubs: STUBS } })
    await flushPromises()
    const publish = rowsGivenToTable(wrapper).find((row) => row.name === 'ingest_publish_ms')
    expect(publish?.budget).toBeNull()
    expect(publish?.pass).toBeNull()
  })

  it('超阈值的那条判超标并带出越限样本数', async () => {
    const broken = report()
    broken.metrics.warning_generation_ms = { ...broken.metrics.warning_generation_ms, p95: 200_000, breaches: 2 }
    mockedLatency.mockResolvedValue(broken as never)
    const wrapper = mount(MetricsView, { global: { stubs: STUBS } })
    await flushPromises()
    const row = rowsGivenToTable(wrapper).find((item) => item.name === 'warning_generation_ms')
    expect(row?.pass).toBe(false)
    expect(row?.breaches).toBe(2)
  })

  it('共用的毫秒轴先换算：秒制那几条进图时已乘 1000', async () => {
    mockedLatency.mockResolvedValue(report() as never)
    const wrapper = mount(MetricsView, { global: { stubs: STUBS } })
    await flushPromises()
    const p95 = chartSeries(wrapper).find((item) => item.name === 'P95')?.data ?? []
    const rows = rowsGivenToTable(wrapper)
    const secondsIndex = rows.findIndex((row) => row.name === 'ingest_end_to_end_seconds')
    const msIndex = rows.findIndex((row) => row.name === 'warning_generation_ms')
    expect(secondsIndex).toBeGreaterThanOrEqual(0)
    expect(p95[secondsIndex], '0.015 秒要画成 15 毫秒，否则对数毫秒轴上它比真毫秒还小').toBeCloseTo(15, 6)
    expect(p95[msIndex]).toBeCloseTo(5200, 6)
    expect(p95).not.toContain(0.015)
  })

  it('刷新按钮会再取一次数', async () => {
    mockedLatency.mockResolvedValue(report() as never)
    const wrapper = mount(MetricsView, { global: { stubs: STUBS } })
    await flushPromises()
    expect(mockedLatency).toHaveBeenCalledTimes(1)
    await wrapper.find('button').trigger('click')
    await flushPromises()
    expect(mockedLatency).toHaveBeenCalledTimes(2)
  })

  /**
   * 失败原因要留在页面上，而且首屏失败不能谎称"还留着上一次的数"。
   *
   * 只有 toast 的话，三秒后这一页就只剩一排看着一切正常的数字。
   */
  it('首屏取数失败：页面上写清原因，且不声称留有旧数', async () => {
    mockedLatency.mockRejectedValue(new Error('时延账本没有声明单位：ingest_end_to_end_seconds（拿到 undefined，只接受 ms / s）'))
    const wrapper = mount(MetricsView, { global: { stubs: STUBS } })
    await flushPromises()
    const line = wrapper.find('[data-testid="metrics-error"]')
    expect(line.text()).toContain('没有声明单位')
    expect(line.text()).toContain('ingest_end_to_end_seconds')
    expect(line.text()).not.toContain('上一次成功取到')
  })

  it('取到数之后再一次失败：说明下面显示的是上一次的数字', async () => {
    mockedLatency.mockResolvedValueOnce(report() as never)
    const wrapper = mount(MetricsView, { global: { stubs: STUBS } })
    await flushPromises()
    expect(wrapper.find('[data-testid="metrics-error"]').exists()).toBe(false)
    mockedLatency.mockRejectedValueOnce(new Error('502 upstream down'))
    await wrapper.find('button').trigger('click')
    await flushPromises()
    const line = wrapper.find('[data-testid="metrics-error"]')
    expect(line.text()).toContain('502 upstream down')
    expect(line.text()).toContain('上一次成功取到的数字')
  })
})

/**
 * 模板层：单元格必须"按行的单位"格式化，列绑的必须是中性键名。
 *
 * 这两条是源码断言而不是渲染断言——a-table 的 bodyCell 插槽在替身里跑不起来，
 * 而这里出错的方式恰好是"把 record.name 当单位传进去"或"列名又写回 p50_ms"，
 * 两种都只体现在模板文本里。渲染层能覆盖的部分（数据、单位、判定）已由上面几条钉住。
 */
describe('指标页模板取数', () => {
  it('单元格用 formatMetricValue(行单位, 行数值)', () => {
    expect(source).toContain("formatMetricValue(record.unit, record[column.key as 'p50'])")
    expect(source).not.toContain('formatMetricValue(record.name')
  })

  it('列绑中性键名，且没有列头写死 (ms)', () => {
    for (const key of ['p50', 'p95', 'max', 'budget']) {
      expect(source, `${key} 列必须存在`).toContain(`dataIndex: '${key}'`)
      expect(source, `${key}_ms 这种键名会把秒制行说成毫秒`).not.toContain(`dataIndex: '${key}_ms'`)
    }
    expect(source).not.toMatch(/title: '(P50|P95|最大|阈值)\s*[（(]ms[)（]/)
  })
})

/**
 * 指标页顶上的标题写着"数据来自运行时埋点，非人工填写"，可它原先只在挂载时取一次：
 * 真机静置 45 秒，样本数一直停在 `ingest_end_to_end_seconds 49616`，而同一时刻
 * `/readyz` 的 `telemetry_count` 已经是 50000 —— 页面上既没有"更新于"，也不会自己再取一次，
 * 站着看这页的人无从判断自己读的是多久以前的实况。监测页与预警页的同款问题都修过，这页是漏下的那一张。
 */
describe('指标页要自己更新，也要说清是什么时候取的数', () => {
  function mountView() {
    mockedLatency.mockResolvedValue(report() as never)
    return mount(MetricsView, { global: { stubs: STUBS } })
  }

  it('挂着这页就会每 15 秒自己再取一次；离开就停', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    const wrapper = mountView()
    await flushPromises()
    expect(mockedLatency).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(15_000)
    expect(mockedLatency, '页面开着却不再取数，读的人看到的是旧账').toHaveBeenCalledTimes(2)
    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(30_000)
    expect(mockedLatency, '离开这页还继续打接口是白耗').toHaveBeenCalledTimes(2)
    vi.useRealTimers()
  })

  it('页面上写着这一屏数字是什么时候取的、多久之前', async () => {
    const wrapper = mountView()
    await flushPromises()
    const bar = wrapper.find('[data-testid="freshness"]')
    expect(bar.exists(), '新鲜度条不见了就等于让人猜新旧').toBe(true)
    expect(bar.text(), '只报时刻不报"多久之前"，页面挂两小时也看不出来').toContain('取数')
    expect(bar.text()).toMatch(/秒前|分前|比预期周期慢/)
  })

  it('取数失败时时间戳不许冒充新的：那句话得说这是上一次的数', async () => {
    mockedLatency.mockRejectedValueOnce(new Error('503 upstream unavailable') as never)
    mockedLatency.mockResolvedValue(report() as never)
    const wrapper = mountView()
    await flushPromises()
    expect(wrapper.find('[data-testid="metrics-error"]').text()).toContain('503')
    // 第一次失败 → 一次数都没取到，此时只能报"没取到"，不许报时刻（有数可说才说时刻）
    const bar = wrapper.find('[data-testid="freshness"]')
    expect(bar.text()).toContain('尚未取到数据')
    expect(bar.text(), '没取到数却写"取数 …"，是凭空造出来的时间戳').not.toContain('取数')
  })
})
