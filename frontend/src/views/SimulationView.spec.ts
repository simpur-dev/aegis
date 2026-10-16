/**
 * 推演沙盒页（§4.4 的报告出口）的四条判据：
 * ① 没推演就不许出现任何概率数字——空表与"跑过了"在页面上必须长得不一样；
 * ② 角色档那一格要说清"应问几次、实问几次、几次回落"，后端给的漏接提示必须原样可见；
 * ③ 时间线每个在场角色一列（票是四票，不是一票）；
 * ④ 越界参数在发出前就拦住，失败原因留在页面上而不只是 toast。
 *
 * 表格/图的外壳用 stub 接住，断言打在"交给渲染层的数据"上：这与指标页同一手法，
 * 目的是让契约变化（后端加一列、少一格）在这里红，而不是在真机上白屏。
 */

import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import SimulationView from '@/views/SimulationView.vue'
import { runSimulation, SIMULATION_DEFAULTS } from '@/api/simulation'

vi.mock('@/api/simulation', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/simulation')>()
  return { ...actual, runSimulation: vi.fn() }
})

const mockedRun = vi.mocked(runSimulation)

const STUBS = {
  'a-card': { name: 'ACard', props: ['title', 'size'], template: '<div class="card"><slot /></div>' },
  'a-alert': {
    name: 'AAlert',
    props: ['message', 'description', 'type', 'showIcon'],
    template: '<div class="alert">{{ message }}<slot name="description" /></div>',
  },
  'a-button': { name: 'AButton', props: ['loading', 'type'], template: '<button class="button"><slot /></button>' },
  'a-form': { name: 'AForm', props: ['layout'], template: '<form class="form"><slot /></form>' },
  'a-form-item': { name: 'AFormItem', props: ['label'], template: '<label class="form-item">{{ label }}<slot /></label>' },
  'a-input-number': { name: 'AInputNumber', props: ['value', 'min', 'max', 'step', 'controls'], template: '<input class="num" />' },
  'a-select': { name: 'ASelect', props: ['value', 'options'], template: '<select class="select" />' },
  'a-statistic': {
    name: 'AStatistic',
    props: ['title', 'value'],
    template: '<div class="stat" :data-testid="`stat-${title}`">{{ value }}</div>',
  },
  'a-table': {
    name: 'ATable',
    props: ['dataSource', 'columns', 'pagination', 'rowKey', 'size'],
    template: '<div class="table" />',
  },
  'a-empty': { name: 'AEmpty', props: ['description'], template: '<div class="empty">{{ description }}</div>' },
}

const ROLES = ['commander', 'rescue', 'residents', 'monitor']

function report(overrides: Record<string, unknown> = {}) {
  return {
    mode: 'demo_sandbox',
    note: '概率/暴露/缓解系数为未实测初值；情景参数由调用方给定，不声称现场达标。',
    cost: { requested_scenarios: 8, effective_scenarios: 8, downscaled: false },
    probability_matrix: [{ rain_bucket_mm: '40-50', scenarios: 8, mean_secondary_rate: 0.41, max_secondary_rate: 0.55 }],
    timeline: ROLES.map((_, index) => ({
      round: index,
      rain_mm: 42,
      mudlevel_cm: 3.3,
      actions: { commander: '转移', rescue: '转移', residents: '转移', monitor: '转移' },
      applied_playbook: '转移',
      mitigation: 0.6,
      trigger_probability: 0.132,
    })),
    playbook_comparison: [
      { playbook: '转移', mitigation: 0.6, mean_secondary_rate: 0.13, exposure_proxy: 26 },
      { playbook: '不行动', mitigation: 0.0, mean_secondary_rate: 0.28, exposure_proxy: 65 },
    ],
    roles: {
      policy: 'rule',
      roles: [
        { key: 'commander', title: '指挥员', concern: '在迟滞与误报之间取舍' },
        { key: 'rescue', title: '救援队', concern: '通路可通行性' },
        { key: 'residents', title: '受影响群众', concern: '预警是否来得及' },
        { key: 'monitor', title: '监测员', concern: '读数趋势' },
      ],
      rounds: 4,
      asked: 16,
      calls: 16,
      fallbacks: 0,
      distinct_actions: ['转移'],
      note: '',
    },
    ...overrides,
  }
}

function tablesGiven(wrapper: ReturnType<typeof mount>): Array<{ columns: Array<{ title: string; key: string }>; rows: unknown[]; testid: string }> {
  return wrapper.findAllComponents({ name: 'ATable' }).map((stub) => ({
    columns: (stub.props('columns') ?? []) as Array<{ title: string; key: string }>,
    rows: (stub.props('dataSource') ?? []) as unknown[],
    testid: String(stub.attributes('data-testid') ?? ''),
  }))
}

beforeEach(() => {
  mockedRun.mockReset()
})

describe('推演页的显示口径', () => {
  it('刚进页面：一个概率数字都没有，只说尚未推演', () => {
    const wrapper = mount(SimulationView, { global: { stubs: STUBS } })
    expect(wrapper.find('[data-testid="sim-empty"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="sim-cost"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('0.41')
  })

  it('点「发起推演」按表单当前取值调用出口，并把后端那句口径原样显示', async () => {
    mockedRun.mockResolvedValue(report() as never)
    const wrapper = mount(SimulationView, { global: { stubs: STUBS } })
    await wrapper.find('[data-testid="sim-run"]').trigger('click')
    await flushPromises()

    expect(mockedRun).toHaveBeenCalledTimes(1)
    expect(mockedRun.mock.calls[0]?.[0]).toMatchObject({ scenarios: SIMULATION_DEFAULTS.scenarios, playbook: '转移' })
    expect(wrapper.find('[data-testid="sim-note"]').text()).toContain('未实测初值')
    expect(wrapper.find('[data-testid="stat-角色档位"]').text()).toBe('rule')
    expect(wrapper.find('[data-testid="stat-推演轮数"]').text()).toBe('4')
    // 规则档也得报票数：不报就等于把"角色到底被问过没有"藏进语气里
    expect(wrapper.find('[data-testid="sim-roles"]').text()).toContain('应问 16 次、实问 16 次、回落 0 次')
  })

  it('时间线给每个在场角色一列，票面跟着角色走而不是写死四列', async () => {
    const threeRoles = report()
    threeRoles.roles = { ...threeRoles.roles, roles: threeRoles.roles.roles.slice(0, 3), asked: 12, calls: 12 }
    mockedRun.mockResolvedValue(threeRoles as never)
    const wrapper = mount(SimulationView, { global: { stubs: STUBS } })
    await wrapper.find('[data-testid="sim-run"]').trigger('click')
    await flushPromises()

    const timeline = tablesGiven(wrapper).find((table) => table.testid === 'sim-timeline')
    expect(timeline?.columns.map((column) => column.title)).toEqual([
      '轮次',
      '雨强 (mm)',
      '泥位 (cm)',
      '指挥员',
      '救援队',
      '受影响群众',
      '本轮生效',
      '触发概率',
    ])
    expect(timeline?.rows).toHaveLength(4)
  })

  it('后端说"有角色没被问到"时这句话上页面，不改写成成功语气', async () => {
    const leaky = report()
    leaky.roles = { ...leaky.roles, policy: 'llm', calls: 12, note: '应问 16 次、实问 12 次：有角色没被问到' }
    mockedRun.mockResolvedValue(leaky as never)
    const wrapper = mount(SimulationView, { global: { stubs: STUBS } })
    await wrapper.find('[data-testid="sim-run"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="sim-roles"]').text()).toContain('有角色没被问到')
  })

  it('LLM 档的回落次数写进那一格：几轮不是模型选的，读的人要能看见', async () => {
    const llm = report()
    llm.roles = { ...llm.roles, policy: 'llm', fallbacks: 3 }
    mockedRun.mockResolvedValue(llm as never)
    const wrapper = mount(SimulationView, { global: { stubs: STUBS } })
    await wrapper.find('[data-testid="sim-run"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="sim-roles"]').text()).toContain('应问 16 次、实问 16 次、回落 3 次')
  })

  it('预算降档要写明请求几条、实跑几条，不能只出一个看起来正常的 K', async () => {
    const cut = report()
    cut.cost = { requested_scenarios: 64, effective_scenarios: 12, downscaled: true }
    mockedRun.mockResolvedValue(cut as never)
    const wrapper = mount(SimulationView, { global: { stubs: STUBS } })
    await wrapper.find('[data-testid="sim-run"]').trigger('click')
    await flushPromises()

    const cost = wrapper.find('[data-testid="sim-cost"]').text()
    expect(cost).toContain('请求 64 条')
    expect(cost).toContain('12 条')
    expect(wrapper.find('[data-testid="stat-本次生效情景"]').text()).toBe('12')
  })

  it('接口失败时原因留在页面上，报告区不出现', async () => {
    mockedRun.mockRejectedValue(new Error('推演接口调用失败（HTTP 503）：服务不可用') as never)
    const wrapper = mount(SimulationView, { global: { stubs: STUBS } })
    await wrapper.find('[data-testid="sim-run"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="sim-error"]').text()).toContain('HTTP 503')
    expect(wrapper.find('[data-testid="sim-matrix"]').exists()).toBe(false)
  })

  it('越界的参数根本不发出去：拦下来说明是哪一格、后端要求多少', async () => {
    const wrapper = mount(SimulationView, { global: { stubs: STUBS } })
    const vm = wrapper.vm as unknown as { draft: Record<string, number | string | null> }
    vm.draft.scenarios = 999
    await wrapper.find('[data-testid="sim-run"]').trigger('click')
    await flushPromises()

    expect(mockedRun).not.toHaveBeenCalled()
    expect(wrapper.find('[data-testid="sim-error"]').text()).toContain('情景条数 999 超出后端的 1..64')
  })

  it('矩阵与预案对比交给表格的行来自报告，页面不做任何换算', async () => {
    mockedRun.mockResolvedValue(report() as never)
    const wrapper = mount(SimulationView, { global: { stubs: STUBS } })
    await wrapper.find('[data-testid="sim-run"]').trigger('click')
    await flushPromises()

    const given = tablesGiven(wrapper)
    const matrix = given.find((table) => table.testid === 'sim-matrix')
    const comparison = given.find((table) => table.testid === 'sim-comparison')
    expect((matrix?.rows as Array<Record<string, unknown>>).map((row) => row.mean_secondary_rate)).toEqual([0.41])
    expect((matrix?.rows as Array<Record<string, unknown>>).map((row) => row.rain_bucket_mm)).toEqual(['40-50'])
    expect(comparison?.rows).toHaveLength(2)
    expect((comparison?.rows as Array<Record<string, unknown>>).map((row) => row.playbook)).toEqual(['转移', '不行动'])
  })
})
