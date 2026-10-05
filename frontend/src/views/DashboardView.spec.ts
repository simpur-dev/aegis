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

import api from '@/api/client'
import { workflowApi } from '@/api/workflow'
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

vi.mock('@/api/workflow', () => ({
  workflowApi: { instances: vi.fn() },
}))

const mockedInstances = vi.mocked(workflowApi.instances)

vi.mock('@/composables/useEventStream', async () => {
  const { ref } = await import('vue')
  return {
    // 必须是真 ref：模板的自动解包只认 `isRef`，给 `{value: [...]}` 会在 `events.slice` 上炸。
    // 每次调用新建一份并留给用例引用，因为"事件流来了新链路"这层行为要靠改它来触发。
    useEventStream: () => {
      const events = ref([
        { trace_id: 'trc_1', ts: '2026-10-03T16:45:13.840Z', subject: 'warning.published', payload: { title: '西藏滑坡预警（红色）' } },
      ])
      capturedEvents = events as never
      return { events }
    },
  }
})

let capturedEvents: { value: Array<Record<string, unknown>> } | null = null

const mocked = vi.mocked(api, true) as unknown as Record<string, ReturnType<typeof vi.fn>>

function responses(overrides: { warning_count?: number; warnings?: number } = {}) {
  const warningCount = overrides.warning_count ?? 6
  mocked.ready.mockResolvedValue({ agents_online: 5, store: { warning_count: warningCount, task_count: 28, chain_count: 6, telemetry_count: 100 } } as never)
  mocked.agents.mockResolvedValue({ items: [] } as never)
  mocked.warnings.mockResolvedValue({ items: Array.from({ length: overrides.warnings ?? warningCount }, (_, i) => ({ warning_id: `wrn_${i}` })) } as never)
  mocked.events.mockResolvedValue({ items: [] } as never)
  mocked.latency.mockResolvedValue({ metrics: {}, sla_thresholds: {}, collaboration: {}, violations: {} } as never)
}

/** 一条最小可用的链路摘要：表格里只用到 trace_id / stages / errors / task_units。 */
function chainOf(traceId: string) {
  return {
    trace_id: traceId,
    event_id: `evt_${traceId.slice(-4)}`,
    ok: true,
    acted: true,
    stages: [{ name: 'perceive', mode: 'local', ok: true, latency_ms: 1, note: '' }],
    errors: [],
    task_units: ['stu_0001'],
    degradations: [],
    risk: null,
  }
}

const STUBS = {
  'a-card': { name: 'ACard', props: ['title', 'loading', 'size'], template: '<div class="card"><div class="card-title">{{ title }}</div><slot /></div>' },
  'a-row': { name: 'ARow', props: ['gutter'], template: '<div class="row"><slot /></div>' },
  'a-col': { name: 'ACol', props: ['span'], template: '<div class="col"><slot /></div>' },
  'a-statistic': { name: 'AStatistic', props: ['title', 'value', 'suffix'], template: '<div class="stat" :data-testid="`stat-${title}`">{{ value }}{{ suffix }}</div>' },
  'a-tag': { name: 'ATag', props: ['color'], template: '<span class="tag"><slot /></span>' },
  'a-table': {
    name: 'ATable',
    props: ['columns', 'dataSource', 'pagination', 'rowKey', 'size'],
    // 真表在 dataSource 为空时渲染 emptyText 插槽；替身不 render 它，空态断言就是空跑
    template:
      '<div class="table"><div v-if="!dataSource || dataSource.length === 0" class="empty-text"><slot name="emptyText" /></div></div>',
  },
  'a-empty': { name: 'AEmpty', props: ['description'], template: '<div class="empty">{{ description }}</div>' },
  'a-button': { name: 'AButton', props: ['loading', 'size'], template: '<button class="button" :disabled="loading"><slot /></button>' },
  'a-alert': { name: 'AAlert', props: ['message', 'type', 'showIcon'], template: '<div class="alert">{{ message }}</div>' },
  'a-timeline': { name: 'ATimeline', template: '<div class="tl"><slot /></div>' },
  'a-timeline-item': { name: 'ATimelineItem', props: ['color'], template: '<div class="tl-item"><slot /></div>' },
  'router-link': { name: 'RouterLink', props: ['to'], template: '<a class="router-link" :data-to="to"><slot /></a>' },
}

beforeEach(() => {
  vi.stubGlobal('message', { error: vi.fn(), success: vi.fn() })
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'setTimeout', 'clearTimeout'] })
  mockedInstances.mockResolvedValue({ count: 0, items: [] } as never)
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

  /**
   * 后端 `BoundedCollection.latest()` 给的是"最近 N 条、旧→新"，这个契约本身没错，
   * 错在照原样摆进一张叫"链路执行记录"的表：真机连点两次上报，时间线立刻出
   * "西藏泥石流预警（橙色）"，这张表第一页一个字没变——刚发生的那条在第 2 页，
   * "最近做了什么"要翻到最后一页才看得见。
   */
  it('链路执行记录把最新一条排在最前，标题也这么说', async () => {
    responses()
    mocked.events.mockResolvedValue({ items: [chainOf('trc_a'), chainOf('trc_b'), chainOf('trc_c')] } as never)
    const wrapper = mount(DashboardView, { global: { stubs: STUBS } })
    await flushPromises()
    const table = wrapper.findAllComponents({ name: 'ATable' })[0]
    const rows = table.props('dataSource') as Array<{ trace_id: string }>
    expect(rows.map((row) => row.trace_id)).toEqual(['trc_c', 'trc_b', 'trc_a'])
    expect(wrapper.text()).toContain('最新在上')
  })

  /**
   * 时间线是即时的，这张表原先最长要等 15 秒才补上——同一块屏幕上
   * "已经发生"与"记录里没有"并存。看到新链路的头一帧就补一次取数，
   * 一次演练连发十几条也只补一次（否则就是请求风暴）。
   */
  it('事件流出现新链路时补一次取数，一串事件只补一次', async () => {
    responses()
    // 表里已经有 trc_a：末尾再推它一次时不该打接口
    mocked.events.mockResolvedValue({ items: [chainOf('trc_a')] } as never)
    const wrapper = mount(DashboardView, { global: { stubs: STUBS } })
    await flushPromises()
    const before = mocked.events.mock.calls.length

    if (capturedEvents === null) throw new Error('useEventStream 的替身没被抓到，这条用例空跑了')
    capturedEvents.value = [{ trace_id: 'trc_new_1', ts: 'x', subject: 'warning.published', payload: {} }]
    await vi.advanceTimersByTimeAsync(600)
    capturedEvents.value = [{ trace_id: 'trc_new_2', ts: 'x', subject: 'feedback.status', payload: {} }, ...capturedEvents.value]
    await vi.advanceTimersByTimeAsync(700)
    await flushPromises()
    expect(mocked.events.mock.calls.length).toBe(before + 1)

    // 已经在这张表里的链路再推一帧：不该打接口
    const afterBurst = mocked.events.mock.calls.length
    capturedEvents.value = [{ trace_id: 'trc_a', ts: 'x', subject: 'warning.published', payload: {} }]
    await vi.advanceTimersByTimeAsync(2_000)
    await flushPromises()
    expect(mocked.events.mock.calls.length).toBe(afterBurst)
    wrapper.unmount()
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

  /**
   * 页脚写"取数失败"，四个大数字却写着 0——那这三行就是假消息：
   * 值班员读到的是"现场一个智能体都没在线、一条预警都没发"，
   * 而真实情况是"我们没能问到"。夜里那轮 500 巡检就是把这两处一起暴露出来的。
   */
  it('首屏取数失败时 KPI 写 —，不写成 0', async () => {
    mocked.ready.mockRejectedValue(new Error('500'))
    mocked.agents.mockRejectedValue(new Error('500'))
    mocked.warnings.mockRejectedValue(new Error('500'))
    mocked.events.mockRejectedValue(new Error('500'))
    mocked.latency.mockRejectedValue(new Error('500'))
    const wrapper = mount(DashboardView, { global: { stubs: STUBS } })
    await flushPromises()
    expect(wrapper.find('[data-testid="stat-在线智能体"]').text()).toBe('—个')
    expect(wrapper.find('[data-testid="stat-已发布预警"]').text()).toBe('—条')
    expect(wrapper.find('[data-testid="stat-任务单元"]').text()).toBe('—个')
    expect(wrapper.find('[data-testid="stat-在线智能体"]').text()).not.toContain('0')
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

/**
 * 这张表有两个写手：15 秒轮询（`refresh()`）与事件流触发的补数（watch 里那次）。
 * 真机不会自然撞上，得把其中一趟响应拖住才看得出来（本机接口 1ms 级）：
 * 轮询先发、补数后发却先回，于是刚补上的那条新链路排在表头；
 * 等轮询那份**更早**的快照迟到落地，它又把表整个替换回旧的一份——
 * 补数这件事本来就是为"时间线有了、这张表还没有"而加的，结果被一次迟到的轮询抹平。
 */
describe('链路执行记录的两个写手不许互相抹', () => {
  it('轮询的旧快照迟到时，不许把刚补上的那条新链路又抹掉', async () => {
    responses()
    let releaseRefresh: (value: unknown) => void = () => {}
    const pending = new Promise((resolve) => {
      releaseRefresh = resolve
    })
    let calls = 0
    mocked.events.mockImplementation((() => {
      calls += 1
      // 第 1 趟是挂载时的轮询（它的快照更早），故意让它迟回；第 2 趟是补数，立刻回。
      return calls === 1 ? pending : Promise.resolve({ items: [chainOf('trc_new_1'), chainOf('trc_a')] })
    }) as never)
    const wrapper = mount(DashboardView, { global: { stubs: STUBS } })
    await flushPromises()
    if (capturedEvents === null) throw new Error('useEventStream 的替身没被抓到，这条用例空跑了')

    capturedEvents.value = [{ trace_id: 'trc_new_1', ts: 'x', subject: 'warning.published', payload: {} }]
    await vi.advanceTimersByTimeAsync(1_300)
    await flushPromises()
    const rows = () => wrapper.findAllComponents({ name: 'ATable' })[0].props('dataSource') as Array<{ trace_id: string }>
    expect(rows().map((row) => row.trace_id)).toContain('trc_new_1')

    releaseRefresh({ items: [chainOf('trc_a')] })
    await flushPromises()
    expect(rows().map((row) => row.trace_id), '更旧的快照迟到就该丢掉，不然新那条又从表里消失了').toContain('trc_new_1')
    wrapper.unmount()
  })

  it('反过来也一样：补数迟到时，不许盖掉更新的那一次轮询', async () => {
    responses()
    let releaseBackfill: (value: unknown) => void = () => {}
    const pending = new Promise((resolve) => {
      releaseBackfill = resolve
    })
    let calls = 0
    mocked.events.mockImplementation((() => {
      calls += 1
      if (calls === 1) return Promise.resolve({ items: [chainOf('trc_a')] })
      // 第 2 趟是补数（先发但迟回），第 3 趟是 15 秒轮询（后发、快照更新）。
      if (calls === 2) return pending
      return Promise.resolve({ items: [chainOf('trc_new_1'), chainOf('trc_a')] })
    }) as never)
    const wrapper = mount(DashboardView, { global: { stubs: STUBS } })
    await flushPromises()
    if (capturedEvents === null) throw new Error('useEventStream 的替身没被抓到，这条用例空跑了')

    capturedEvents.value = [{ trace_id: 'trc_new_1', ts: 'x', subject: 'warning.published', payload: {} }]
    await vi.advanceTimersByTimeAsync(1_300)
    await vi.advanceTimersByTimeAsync(14_000)
    await flushPromises()
    const rows = () => wrapper.findAllComponents({ name: 'ATable' })[0].props('dataSource') as Array<{ trace_id: string }>
    expect(rows().map((row) => row.trace_id)).toContain('trc_new_1')

    releaseBackfill({ items: [chainOf('trc_a')] })
    await flushPromises()
    expect(rows().map((row) => row.trace_id), '补数那趟比轮询先发，迟到了就该让位').toContain('trc_new_1')
    wrapper.unmount()
  })
})

/**
 * 全新生起的后端（0 条链路、0 条预警）走查每个页面时量到的：这两张表落回 antd 默认的
 * 英文 "No data"。整站是简体中文值班台，第一屏唯一的一句英文既不说明"确实还没有"，
 * 也不说下一步去哪儿造一条——而这台机器上线第一天的默认画面就是这个样子。
 */
describe('空表要说人话也要说下一步', () => {
  it('还没有链路时给出中文说明与"去哪儿跑一条"', async () => {
    responses()
    mocked.events.mockResolvedValue({ items: [] } as never)
    const wrapper = mount(DashboardView, { global: { stubs: STUBS } })
    await flushPromises()
    const empty = wrapper.find('[data-testid="chains-empty"]')
    expect(empty.exists(), '不能只剩 antd 默认的英文 "No data"').toBe(true)
    expect(empty.text()).toContain('还没有链路执行记录')
    expect(empty.text()).toContain('演练')
    expect(wrapper.text()).not.toContain('No data')
    wrapper.unmount()
  })
})

/**
 * 这张表取的是最近 20 条链路。真机把这台进程演练到 108 条之后量到：卡片只说
 * "最新在上"，看不出 88 条在表外——`/readyz` 的 chain_count 才是总数。
 */
describe('链路表的窗口口径', () => {
  it('卡片标题说出窗口与总数：与 /readyz 的台账对得上', async () => {
    responses()
    mocked.ready.mockResolvedValue({
      agents_online: 5,
      store: { warning_count: 113, task_count: 504, chain_count: 108, telemetry_count: 50_000 },
    } as never)
    mocked.events.mockResolvedValue({ items: Array.from({ length: 20 }, (_, i) => chainOf(`trc_${String(i).padStart(2, '0')}`)) } as never)
    const wrapper = mount(DashboardView, { global: { stubs: STUBS } })
    await flushPromises()
    const title =
      wrapper
        .findAllComponents({ name: 'ACard' })
        .map((card) => String(card.props('title')))
        .find((t) => t.startsWith('链路执行记录')) ?? ''
    expect(title).toContain('最近 20 条')
    expect(title).toContain('共 108 条')
    wrapper.unmount()
  })

  it('链路没超过窗口时不说"共 N 条"，免得凭空造出一个对比', async () => {
    responses()
    mocked.ready.mockResolvedValue({
      agents_online: 5,
      store: { warning_count: 2, task_count: 6, chain_count: 2, telemetry_count: 5_000 },
    } as never)
    mocked.events.mockResolvedValue({ items: [chainOf('trc_a'), chainOf('trc_b')] } as never)
    const wrapper = mount(DashboardView, { global: { stubs: STUBS } })
    await flushPromises()
    const title =
      wrapper
        .findAllComponents({ name: 'ACard' })
        .map((card) => String(card.props('title')))
        .find((t) => t.startsWith('链路执行记录')) ?? ''
    expect(title).toContain('最近 20 条')
    expect(title).not.toContain('共')
    wrapper.unmount()
  })
})

/**
 * 两张来自同一窗口的视图要一起说实话，且网格不许把任务单元号的一段数字当成区划代码。
 *
 * 原代码：`chain.risk?.region_code ?? chain.task_units[0]?.slice(4, 10) ?? '未知区域'`——
 * 一条没研判出风险的链路，区域是从 `stu_…` 上第 5 位起截 6 个字符得来的。
 * `stu_5401210abcd` 会截出 `540121`：一个长得完全像真区划代码的数，摆进"风险区域网格"里，
 * 读的人没有任何办法知道这是拼出来的。
 */
describe('风险区域网格不许拼出假的区划代码', () => {
  it('没研判出风险的链路标成"未标注区域"，而不是从任务单元号里截一段', async () => {
    responses()
    mocked.events.mockResolvedValue({
      items: [{ ...chainOf('trc_norisk'), risk: null, task_units: ['stu_5401210abcd'] }],
    } as never)
    const wrapper = mount(DashboardView, { global: { stubs: STUBS } })
    await flushPromises()
    const regions = wrapper.findAll('.region').map((cell) => cell.text())
    expect(regions, `网格里出现了拼出来的区划代码：${regions.join(',')}`).toEqual(['未标注区域'])
    expect(wrapper.find('.cell').text()).toContain('无风险')
    wrapper.unmount()
  })

  it('网格与链路表同吃一个窗口，标题就得同样说出窗口', async () => {
    responses()
    mocked.ready.mockResolvedValue({
      agents_online: 5,
      store: { warning_count: 113, task_count: 504, chain_count: 108, telemetry_count: 50_000 },
    } as never)
    mocked.events.mockResolvedValue({ items: [chainOf('trc_a')] } as never)
    const wrapper = mount(DashboardView, { global: { stubs: STUBS } })
    await flushPromises()
    const title =
      wrapper
        .findAllComponents({ name: 'ACard' })
        .map((card) => String(card.props('title')))
        .find((t) => t.startsWith('风险区域网格')) ?? ''
    expect(title, '链路表已经写"最近 20 条"，紧挨着的网格不能什么都别说').toContain('最近 20 条')
    expect(title).toContain('共 108 条')
    wrapper.unmount()
  })
})

/**
 * 值班员上班第一眼看的是"有没有单在等我"。
 *
 * 真机量过：这台进程 2 张工单等着签，落地页上"待签/等人签/核签/工单"
 * 字样 0 次——工单在等，页面不吭声；人要自己想到去流程页、再把右栏滚到底。
 * 这里钉住两件事：有单时报数并给入口；没人等签时不摆"0 张"的假动作。
 */
describe('总览把等人签的工单顶到第一屏', () => {
  function ticket(id: string, status: string) {
    return { instance_id: id, status }
  }

  it('有单在等时点名叫人处理，并给出去流程页的入口', async () => {
    responses()
    mockedInstances.mockResolvedValue({
      count: 3,
      items: [ticket('wfi_a', 'waiting'), ticket('wfi_b', 'waiting'), ticket('wfi_c', 'succeeded')],
    } as never)
    const wrapper = mount(DashboardView, { global: { stubs: STUBS } })
    await flushPromises()
    const banner = wrapper.find('[data-testid="dashboard-todos"]')
    expect(banner.exists(), '"有工单在等人签"必须出现在页面上').toBe(true)
    expect(banner.text()).toContain('2 张工单在等人签')
    expect(banner.find('.router-link').attributes('data-to'), '入口要通向流程页').toBe('/workflow')
    wrapper.unmount()
  })

  it('没人等签时不摆待办条，也不给"0 张"的假动作', async () => {
    responses()
    mockedInstances.mockResolvedValue({ count: 1, items: [ticket('wfi_a', 'succeeded')] } as never)
    const wrapper = mount(DashboardView, { global: { stubs: STUBS } })
    await flushPromises()
    expect(wrapper.find('[data-testid="dashboard-todos"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('工单数取不到时什么都不说，不拿 0 冒充"没有单在等"', async () => {
    responses()
    mockedInstances.mockRejectedValue(new Error('503'))
    const wrapper = mount(DashboardView, { global: { stubs: STUBS } })
    await flushPromises()
    expect(wrapper.find('[data-testid="dashboard-todos"]').exists()).toBe(false)
    // 取数失败本身有页面级告警兜底，这里只确认没有把"未知"画成"没有"
    expect(wrapper.find('[data-testid="dashboard-error"]').exists()).toBe(true)
    wrapper.unmount()
  })
})
