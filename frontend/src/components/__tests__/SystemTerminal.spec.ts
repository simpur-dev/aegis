import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import SystemTerminal from '../SystemTerminal.vue'

interface StreamStub {
  events: { value: { subject: string; trace_id: string; payload: Record<string, unknown>; ts: string }[] }
  connected: { value: boolean }
  stalled: { value: boolean }
}

/**
 * `vi.hoisted` 里不能碰模块顶层 import 的 `ref`（提升后先于初始化，实测报
 * "Cannot access '__vi_import_1__' before initialization"），所以在 mock 工厂内部动态 import。
 */
const holder = vi.hoisted(() => ({ current: null as StreamStub | null }))

vi.mock('@/composables/useEventStream', async () => {
  const { ref } = await import('vue')
  holder.current = { events: ref([]), connected: ref(true), stalled: ref(false) } as unknown as StreamStub
  return { useEventStream: () => holder.current as StreamStub }
})

const stream = (): StreamStub => holder.current as StreamStub

describe('SystemTerminal', () => {
  beforeEach(() => {
    stream().events.value = []
    stream().connected.value = true
    stream().stalled.value = false
  })

  it('默认收起：展开态有 188px 高，压在监测页右下角会盖住表格，得让人自己点开', async () => {
    const wrapper = mount(SystemTerminal)
    expect(wrapper.find('.sys-term__body').exists()).toBe(false)
    await wrapper.find('[data-testid="sys-term-toggle"]').trigger('click')
    expect(wrapper.find('.sys-term__body').exists(), '点开之后要有内容区').toBe(true)
    await wrapper.find('[data-testid="sys-term-toggle"]').trigger('click')
    expect(wrapper.find('.sys-term__body').exists()).toBe(false)
  })

  it('一条事件都没有时说清"还没有"，并解释什么时候会有', async () => {
    const wrapper = mount(SystemTerminal)
    await wrapper.find('[data-testid="sys-term-toggle"]').trigger('click')
    expect(wrapper.find('.sys-term__empty').text()).toContain('还没有事件')
  })

  it('事件按序成行：时间戳、subject、trace 各占一栏，芯片上的计数跟着走', async () => {
    stream().events.value = [
      { subject: 'platform.alert', trace_id: 'trc_a1', payload: {}, ts: '2026-10-05T04:12:09.000Z' },
      { subject: 'workflow.node.succeeded', trace_id: 'trc_b2', payload: {}, ts: '2026-10-05T04:12:31.000Z' },
    ]
    const wrapper = mount(SystemTerminal)
    await wrapper.find('[data-testid="sys-term-toggle"]').trigger('click')
    const rows = wrapper.findAll('.sys-term__row')
    expect(rows).toHaveLength(2)
    // 东八区固定换算：04:12Z → 12:12
    expect(rows[0]!.find('.sys-term__time').text()).toBe('12:12:09')
    expect(rows[0]!.find('.sys-term__subject').text()).toBe('platform.alert')
    expect(rows[1]!.find('.sys-term__trace').text()).toBe('trc_b2')
    expect(wrapper.find('.sys-term__count').text()).toBe('2')
  })

  it('流断了要在面板里说"正在重连"，不许静默留一排旧行装作还在跑', async () => {
    stream().events.value = [{ subject: 'platform.alert', trace_id: 'trc_a1', payload: {}, ts: '2026-10-05T04:12:09.000Z' }]
    stream().connected.value = false
    stream().stalled.value = true
    const wrapper = mount(SystemTerminal)
    await wrapper.find('[data-testid="sys-term-toggle"]').trigger('click')
    expect(wrapper.find('.sys-term__warn').text()).toContain('没心跳')
    expect(wrapper.find('.sys-term__warn').text()).toContain('重连')
  })

  it('trace 为空时写破折号，不留一个空格让人以为看错了', async () => {
    stream().events.value = [{ subject: 'platform.alert', trace_id: '', payload: {}, ts: '2026-10-05T04:12:09.000Z' }]
    const wrapper = mount(SystemTerminal)
    await wrapper.find('[data-testid="sys-term-toggle"]').trigger('click')
    expect(wrapper.find('.sys-term__trace').text()).toBe('—')
  })
})
