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
        <!-- 真表在 dataSource 为空时渲染 emptyText 插槽；替身不render它，空态断言就是空跑 -->
        <div v-if="!dataSource || dataSource.length === 0" class="empty-text"><slot name="emptyText" /></div>
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

/**
 * 真机量到的一条（`.tmp-verify/warnings-detail-race.mjs`，把第一趟 /api/v1/events 人为拖 2.5 秒）：
 * 先点 A（它那条不在链路窗口里），500ms 后点 B（B 的链路真有 4 个任务单元）。
 * B 的表正常列出来了，可 A 迟到的那句也贴了上来——抽屉里同时写着
 * "最近 200 条链路里没有这条预警的执行记录"和 4 行任务单元，自相矛盾，
 * 而且那句是对 B 的一个谎：值班员会以为这条预警没派任务。
 */
describe('点开另一条预警时，迟到的回查不许写在新的那条上', () => {
  function twoWarnings() {
    mockedWarnings.mockResolvedValue({
      items: [warning(), warning({ warning_id: 'wrn_2', event_id: 'evt_2', trace_id: 'trc_2', title_zh: '西藏滑坡预警（橙色）' })],
    } as never)
  }

  it('A 的回查慢了一步、抽屉已换成 B：B 不该带上 A 那句"无从判断"', async () => {
    twoWarnings()
    let release: (value: unknown) => void = () => {}
    const stalled = new Promise((resolve) => {
      release = resolve
    })
    let calls = 0
    mockedEvents.mockImplementation((() => {
      calls += 1
      return calls === 1 ? stalled : Promise.resolve({ items: [{ warning_id: 'wrn_2', task_units: ['stu_a', 'stu_b'] }] })
    }) as never)
    mockedTask.mockImplementation(
      (async (id: string) => ({ task_unit_id: id, objective: '巡查' + id, sla_seconds: 60, owner_role: '巡护员', created_by: 'plan.mock' })) as never,
    )
    const wrapper = mountView()
    await flushPromises()
    const buttons = wrapper.findAll('[data-testid="cell-action"] .button')
    await buttons[0].trigger('click')
    await buttons[1].trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('巡查stu_a')
    expect(wrapper.text()).toContain('巡查stu_b')

    release({ items: [] })
    await flushPromises()
    expect(wrapper.find('[data-testid="tasks-notice"]').exists(), 'A 那句"没有执行记录"不该贴到 B 的抽屉上').toBe(false)
    expect(wrapper.text()).not.toContain('无从判断')
  })

  it('A 的迟到响应不许把 B 判成"没有产出任务单元"，B 自己回来才轮到说这句话', async () => {
    twoWarnings()
    const gates: Array<{ promise: Promise<unknown>; resolve: (value: unknown) => void }> = []
    mockedEvents.mockImplementation((() => {
      let resolve: ((value: unknown) => void) | null = null
      const promise = new Promise<unknown>((r) => {
        resolve = r
      })
      gates.push({ promise, resolve: (value: unknown) => resolve?.(value) })
      return promise
    }) as never)
    const wrapper = mountView()
    await flushPromises()
    const buttons = wrapper.findAll('[data-testid="cell-action"] .button')
    await buttons[0].trigger('click')
    await buttons[1].trigger('click')
    await flushPromises()
    expect(gates).toHaveLength(2)

    gates[0].resolve({ items: [] })
    await flushPromises()
    expect(wrapper.find('[data-testid="tasks-notice"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="tasks-empty"]').exists(), 'B 那一趟还在路上，不能被 A 的迟到响应判完结').toBe(false)

    gates[1].resolve({ items: [{ warning_id: 'wrn_2', task_units: [] }] })
    await flushPromises()
    expect(wrapper.find('[data-testid="tasks-empty"]').text()).toContain('没有产出任务单元')
  })
})

describe('点开另一条预警时，迟到的任务单元回查也不许盖上来', () => {
  it('A 的任务单元取得慢、抽屉已换成 B：A 那一串不许替换掉 B 的表', async () => {
    mockedWarnings.mockResolvedValue({
      items: [
        warning(),
        warning({ warning_id: 'wrn_2', event_id: 'evt_2', trace_id: 'trc_2', title_zh: '西藏滑坡预警（橙色）' }),
      ],
    } as never)
    mockedEvents.mockImplementation(
      ((async () => ({
        items: [
          { warning_id: 'wrn_1', task_units: ['stu_a1', 'stu_a2'] },
          { warning_id: 'wrn_2', task_units: ['stu_b1'] },
        ],
      })) as never),
    )
    let releaseTasks: (value: unknown) => void = () => {}
    const slow = new Promise((resolve) => {
      releaseTasks = resolve
    })
    mockedTask.mockImplementation(((id: string) => {
      if (id.startsWith('stu_a')) return slow
      return Promise.resolve({ task_unit_id: id, objective: '巡查' + id, sla_seconds: 60, owner_role: '巡护员', created_by: 'plan.mock' })
    }) as never)
    const wrapper = mountView()
    await flushPromises()
    const buttons = wrapper.findAll('[data-testid="cell-action"] .button')
    await buttons[0].trigger('click')
    await buttons[1].trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('巡查stu_b1')

    releaseTasks({ task_unit_id: 'stu_a1', objective: '巡查stu_a1', sla_seconds: 60, owner_role: '巡护员', created_by: 'plan.mock' })
    await flushPromises()
    const drawer = wrapper.find('.drawer').text()
    expect(drawer, 'A 那两条迟到落地也不能把 B 的表换掉').not.toContain('巡查stu_a1')
    expect(drawer).toContain('巡查stu_b1')
  })
})

/**
 * 接口按"最近 N 条、旧→新"给（后端 `BoundedCollection.latest()` 的口径），照原样摆进这张表
 * 就是把刚发出去的预警压到最后一页。真机量过：29 条、每页 10 条时，第 1 页是
 * `00:22:40 → 01:09:11`，而刚发布那条（`01:14:34`）在第 3 页——这张页的名字叫
 * "预警发布与靶向触达"，值班员发完红色预警回到这页，第一屏看不到自己刚发的那条。
 * 这与总览"链路执行记录"那张表先前是同一件事（那边已按"最新在上"改过）。
 */
describe('刚发布的预警不许被压到最后一页', () => {
  function threeWarnings() {
    mockedWarnings.mockResolvedValue({
      items: [
        warning({ warning_id: 'wrn_old', title_zh: '最早那条', generated_at: '2026-10-03T10:00:00.000Z' }),
        warning({ warning_id: 'wrn_mid', title_zh: '中间那条', generated_at: '2026-10-03T12:00:00.000Z' }),
        warning({ warning_id: 'wrn_new', title_zh: '刚发那条', generated_at: '2026-10-03T14:00:00.000Z' }),
      ],
    } as never)
  }

  it('列表自己按时间倒序排，不依赖后端给的顺序', async () => {
    threeWarnings()
    const wrapper = mountView()
    await flushPromises()
    // 替身表格只渲染有 bodyCell 分支的那几列（标题列是纯 dataIndex，落到默认输出会被 bodyCell 吞掉），
    // 所以按"生成时间"这一列对账：三条的 generated_at 各差两小时，顺序错一位就看得出。
    const times = wrapper
      .findAll('.tr')
      .map((row) => row.find('[data-testid="cell-generated_at"]').text())
    expect(times).toEqual(['2026-10-03 22:00:00', '2026-10-03 20:00:00', '2026-10-03 18:00:00'])
  })

  it('列头写明"最新在上"：读的人不用先猜这张表是怎么排的', async () => {
    threeWarnings()
    const wrapper = mountView()
    await flushPromises()
    expect(headerText(wrapper, '生成时间')).toContain('最新在上')
  })
})

/**
 * 空表要说人话，也要说下一步。
 *
 * 全新生起的后端（0 条预警、0 条链路）走一遍每个页面量到的：这两张表落回 antd 的默认英文
 * "No data"——一个中文值班台界面上唯一的一句英文，而且分不清"确实还没有"与"取数没成功"。
 * 这台机器的现场形态是新站点上线第一天，第一屏就是这个样子。
 */
describe('空表要说人话也要说下一步', () => {
  it('一条预警都没有时，给出中文说明与造出预警的路径', async () => {
    mockedWarnings.mockResolvedValue({ count: 0, items: [] } as never)
    const wrapper = mountView()
    await flushPromises()
    const empty = wrapper.find('[data-testid="warnings-empty"]')
    expect(empty.exists(), '空表不能只剩 antd 默认的英文 "No data"').toBe(true)
    expect(empty.text()).toContain('还没有预警')
    expect(empty.text()).toContain('演练')
    expect(wrapper.text()).not.toContain('No data')
  })
})

/**
 * 助手页有三条芯片要一个标识符（`解释 wrn_…`、`链路追踪 trc_…`、`查询任务单元`），
 * 而此前整个界面没有一处显示 `wrn_`/`stu_` 编号——点芯片得到的"缺少 …ID"根本没法自助解决。
 * 后端那句回答现在会指路到这张抽屉，所以抽屉里必须真能读到那两个标识。
 */
describe('抽屉里要能读到助手指名要的那个标识', () => {
  it('预警标识写在抽屉顶上：助手「解释 wrn_…」缺参数时指的就是这里', async () => {
    const wrapper = mountView()
    await flushPromises()
    await wrapper.find('[data-testid="cell-action"] .button').trigger('click')
    await flushPromises()
    const text = wrapper.find('.drawer').text()
    expect(text).toContain('预警标识')
    expect(text).toContain('wrn_1')
  })

  it('任务单元表带任务标识列：助手「查询任务单元」缺参数时指的就是这里', async () => {
    mockedEvents.mockResolvedValue({ items: [{ warning_id: 'wrn_1', task_units: ['stu_a1'] }] } as never)
    mockedTask.mockResolvedValue({ task_unit_id: 'stu_a1', objective: '巡查', sla_seconds: 60, owner_role: '巡护员', created_by: 'plan.mock' } as never)
    const wrapper = mountView()
    await flushPromises()
    await wrapper.find('[data-testid="cell-action"] .button').trigger('click')
    await flushPromises()
    const text = wrapper.find('.drawer').text()
    expect(text).toContain('任务标识')
    expect(text).toContain('stu_a1')
  })
})
