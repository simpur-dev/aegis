import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { IntegrationSnapshot } from '@/api/integrations'
import { fetchIntegrations } from '@/api/integrations'
import LegStatusPanel from '@/components/system/LegStatusPanel.vue'

vi.mock('@/api/integrations', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/integrations')>()
  return { ...actual, fetchIntegrations: vi.fn() }
})

const mockedFetch = vi.mocked(fetchIntegrations)

/**
 * 只 stub 到"能把本面板用到的 props 和 slot 透出来"的程度：
 * 这些替身不代表 antd 的真实行为，代表的是面板对 antd 的三点依赖（tag 的颜色、alert 的文案、card 的 loading）。
 */
const STUBS = {
  'a-card': {
    name: 'ACard',
    props: ['title', 'loading', 'size'],
    template: '<div class="card" :data-loading="String(loading)"><div class="extra"><slot name="extra" /></div><slot /></div>',
  },
  'a-space': { name: 'ASpace', props: ['size'], template: '<div class="space"><slot /></div>' },
  'a-tag': { name: 'ATag', props: ['color'], template: '<span class="tag" :data-color="color"><slot /></span>' },
  'a-button': { name: 'AButton', props: ['loading', 'size'], template: '<button class="button"><slot /></button>' },
  'a-alert': { name: 'AAlert', props: ['message', 'type', 'showIcon'], template: '<div class="alert">{{ message }}</div>' },
}

interface RowSeed {
  name?: string
  enabled?: boolean
  driver?: string
  detail?: Record<string, unknown>
}

function snapshot(items: RowSeed[] = [], extra: Partial<IntegrationSnapshot> = {}): IntegrationSnapshot {
  return {
    items: items.map((item) => ({ name: 'store', enabled: true, driver: 'memory', detail: {}, ...item })),
    degraded: [],
    all_enabled: false,
    ...extra,
  }
}

async function renderPanel(items: RowSeed[] = [], extra: Partial<IntegrationSnapshot> = {}, refreshMs?: number) {
  mockedFetch.mockResolvedValue(snapshot(items, extra))
  const wrapper = mount(LegStatusPanel, {
    props: refreshMs === undefined ? {} : { refreshMs },
    global: { stubs: STUBS },
  })
  await flushPromises()
  return wrapper
}

function byTestid(wrapper: ReturnType<typeof mount>, id: string) {
  return wrapper.find(`[data-testid="${id}"]`)
}

beforeEach(() => {
  mockedFetch.mockReset()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('腿面板：行由后端决定，不由前端清单决定', () => {
  it('每条上报的腿都渲染一行，并贴上中文标签', async () => {
    const wrapper = await renderPanel([{ name: 'weather', driver: 'http' }, { name: 'tracing', driver: 'otlp' }])
    expect(wrapper.findAll('.leg-row')).toHaveLength(2)
    expect(byTestid(wrapper, 'leg-weather').text()).toContain('气象拉取腿')
    expect(byTestid(wrapper, 'leg-tracing').text()).toContain('链路追踪')
  })

  it('后端新增一条没登记标签的腿：照常出现，不被过滤掉', async () => {
    const wrapper = await renderPanel([{ name: 'kafka', driver: 'nats' }])
    expect(byTestid(wrapper, 'leg-kafka').exists()).toBe(true)
    expect(byTestid(wrapper, 'leg-kafka').text()).toContain('kafka')
  })

  it('详情键值原样展示，但凭据类键连值一起不落到 DOM', async () => {
    const wrapper = await renderPanel([
      {
        name: 'store',
        detail: {
          target: '127.0.0.1:5432',
          pg_dsn: 'postgresql://aegis:sup3rs3cr3t@127.0.0.1:5432/aegis',
          neo4j_password: 'hunter2',
        },
      },
    ])
    expect(byTestid(wrapper, 'detail-store-target').text()).toContain('127.0.0.1:5432')
    expect(byTestid(wrapper, 'detail-store-pg_dsn').exists()).toBe(false)
    expect(byTestid(wrapper, 'detail-store-neo4j_password').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('sup3rs3cr3t')
    expect(wrapper.text()).not.toContain('hunter2')
  })

  it('driver 缺失显示为 —，不显示空串', async () => {
    const wrapper = await renderPanel([{ name: 'mqtt', enabled: false, driver: '' }])
    expect(byTestid(wrapper, 'leg-mqtt').text()).toContain('driver=—')
  })

  it('触达通道那种对象数组要摊平显示，不能剩 [object Object]', async () => {
    // 真实形状来自后端 `dispatcher.channel_status()`：每个通道一条 {channel, mode, sent, failed}。
    // 这条用例盯的是"整条腿唯一要看的字段全成了占位符"——面板不报错、接口 200，只有人看得见。
    const wrapper = await renderPanel([
      {
        name: 'delivery',
        driver: 'http',
        detail: {
          channels: [
            { channel: 'sms', mode: 'http', sent: 3, delivered: 3, failed: 0 },
            { channel: 'broadcast', mode: 'http', sent: 1, delivered: 0, failed: 1 },
          ],
        },
      },
    ])
    const leg = byTestid(wrapper, 'leg-delivery')
    const fact = byTestid(wrapper, 'detail-delivery-channels')
    expect(leg.text()).not.toContain('[object Object]')
    expect(fact.text()).toContain('channel=sms')
    // 截断是既有设计，全文挂 title：这里验的是"截了但没丢"
    expect(fact.attributes('title')).toContain('channel=broadcast')
    expect(fact.attributes('title')).toContain('failed=1')
  })

  it('空串值显示为 —，不留 `endpoint=` 这种断头', async () => {
    const wrapper = await renderPanel([{ name: 'tracing', detail: { endpoint: '' } }])
    expect(byTestid(wrapper, 'detail-tracing-endpoint').text()).toBe('endpoint=—')
  })

  it('超长详情截断进 DOM，全文挂到 title 上：一行放不下不等于可以丢', async () => {
    const long = '内'.repeat(120)
    const wrapper = await renderPanel([{ name: 'knowledge', detail: { dataset: long } }])
    const fact = byTestid(wrapper, 'detail-knowledge-dataset')
    expect(fact.text()).toContain('…')
    expect(fact.attributes('title')).toBe(long)
  })
})

describe('腿面板：三态要一眼分开', () => {
  it.each([
    [{ enabled: false, driver: 'off' }, '未启用', 'default'],
    [{ detail: { target: 'x' } }, '运行中', 'green'],
    [{ detail: { degraded: 'dense-embedder' } }, '降级运行', 'orange'],
  ] as [RowSeed, string, string][])(
    '%o → %s（色 %s）',
    async (override, label, color) => {
      const wrapper = await renderPanel([{ name: 'knowledge', ...override }])
      const tag = byTestid(wrapper, 'state-knowledge')
      expect(tag.text()).toBe(label)
      expect(tag.attributes('data-color')).toBe(color)
    },
  )

  it('未启用与降级运行不许同色：这两种在处置上是两回事', async () => {
    const wrapper = await renderPanel([
      { name: 'mqtt', enabled: false },
      { name: 'knowledge', detail: { degraded: 'reranker' } },
    ])
    expect(byTestid(wrapper, 'state-mqtt').attributes('data-color')).not.toBe(
      byTestid(wrapper, 'state-knowledge').attributes('data-color'),
    )
  })
})

describe('腿面板：读不到 ≠ 没问题', () => {
  it('接口失败时角标显示"读不到装配事实"，并把错误文案摊出来', async () => {
    mockedFetch.mockRejectedValue(new Error('装配事实读取失败: timeout of 8000ms exceeded'))
    const wrapper = mount(LegStatusPanel, { global: { stubs: STUBS } })
    await flushPromises()
    expect(byTestid(wrapper, 'panel-state').text()).toBe('读不到装配事实')
    expect(byTestid(wrapper, 'panel-error').text()).toContain('timeout of 8000ms exceeded')
  })

  it('失败后的空状态说"不可读"，不说"暂无"这种像正常的话', async () => {
    mockedFetch.mockRejectedValue(new Error('502'))
    const wrapper = mount(LegStatusPanel, { global: { stubs: STUBS } })
    await flushPromises()
    expect(byTestid(wrapper, 'legs-empty').text()).toBe('装配事实不可读')
  })

  it('恢复后错误痕迹清干净，角标回到按真实状态给的文案', async () => {
    mockedFetch.mockRejectedValueOnce(new Error('502')).mockResolvedValue(
      snapshot([{ name: 'store' }], { all_enabled: true }),
    )
    const wrapper = mount(LegStatusPanel, { global: { stubs: STUBS } })
    await flushPromises()
    await wrapper.find('.button').trigger('click')
    await flushPromises()
    expect(byTestid(wrapper, 'panel-error').exists()).toBe(false)
    expect(byTestid(wrapper, 'panel-state').text()).toBe('全部启用')
  })

  it('读到了但全部腿未启用时，角标是"有腿未启用"而不是"读不到"', async () => {
    const wrapper = await renderPanel([{ name: 'weather', enabled: false, driver: 'off' }])
    expect(byTestid(wrapper, 'panel-state').text()).toBe('有腿未启用')
  })
})

describe('腿面板：轮询与卸载', () => {
  it('按 refreshMs 周期重复拉取', async () => {
    vi.useFakeTimers()
    mockedFetch.mockResolvedValue(snapshot([{ name: 'store' }]))
    mount(LegStatusPanel, { props: { refreshMs: 20_000 }, global: { stubs: STUBS } })
    await vi.advanceTimersByTimeAsync(0)
    expect(mockedFetch).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(40_000)
    expect(mockedFetch).toHaveBeenCalledTimes(3)
  })

  it('卸载后停止轮询：定时器留在页面上会一直打接口', async () => {
    vi.useFakeTimers()
    mockedFetch.mockResolvedValue(snapshot([{ name: 'store' }]))
    const wrapper = mount(LegStatusPanel, { props: { refreshMs: 10_000 }, global: { stubs: STUBS } })
    await vi.advanceTimersByTimeAsync(0)
    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(60_000)
    expect(mockedFetch).toHaveBeenCalledTimes(1)
  })

  it('点刷新立即再拉一次', async () => {
    const wrapper = await renderPanel([{ name: 'store' }])
    await wrapper.find('.button').trigger('click')
    await flushPromises()
    expect(mockedFetch).toHaveBeenCalledTimes(2)
  })

  it('页签切到后台时定时轮询停发，切回来自己恢复', async () => {
    vi.useFakeTimers()
    mockedFetch.mockResolvedValue(snapshot([{ name: 'store' }]))
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' })
    const wrapper = mount(LegStatusPanel, { props: { refreshMs: 10_000 }, global: { stubs: STUBS } })
    await vi.advanceTimersByTimeAsync(0)
    await vi.advanceTimersByTimeAsync(30_000)
    expect(mockedFetch).toHaveBeenCalledTimes(1)

    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' })
    await vi.advanceTimersByTimeAsync(10_000)
    expect(mockedFetch).toHaveBeenCalledTimes(2)
    wrapper.unmount()
    delete (document as { visibilityState?: unknown }).visibilityState
  })

  it('首轮结果回来前不显示"后端未上报任何可选腿"', async () => {
    mockedFetch.mockReturnValue(new Promise<IntegrationSnapshot>(() => undefined))
    const wrapper = mount(LegStatusPanel, { global: { stubs: STUBS } })
    await flushPromises()
    expect(byTestid(wrapper, 'legs-empty').exists()).toBe(false)
    expect(wrapper.find('.card').attributes('data-loading')).toBe('true')
  })
})
