import { mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import FreshnessBar from '../FreshnessBar.vue'

const iso = (msAgo: number): string => new Date(Date.now() - msAgo).toISOString()

/**
 * 新鲜度条：左端口径、右端"几点取的 + 多久之前"，中间一道线。
 * 盯三件会骗人的事：没取到数却显示时间、数过期了还装作新鲜、年龄冻在挂载那一刻。
 */
describe('FreshnessBar', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('取到数：左端口径 + 右端"取数 … ｜ N 秒前"，中间那道线在位', () => {
    const wrapper = mount(FreshnessBar, { props: { fetchedAt: iso(4_000), label: '台账 6 条 ｜ 每 15 秒自刷' } })
    expect(wrapper.text()).toContain('台账 6 条 ｜ 每 15 秒自刷')
    expect(wrapper.text()).toContain('4 秒前')
    expect(wrapper.text()).toContain('取数')
    expect(wrapper.find('.fresh__rule').isVisible(), '两端之间那道线是这条判据的形状，不能没有').toBe(true)
  })

  it('一次都没取到时说"尚未取到数据"，不许拿当前时间冒充"更新于"', () => {
    const wrapper = mount(FreshnessBar, { props: { fetchedAt: null, label: '台账 6 条' } })
    expect(wrapper.text()).toContain('尚未取到数据')
    expect(wrapper.text()).not.toContain('取数')
    expect(wrapper.classes()).not.toContain('is-stale')
  })

  it('超过两个自刷周期：上 warn 态并明说"比预期周期慢"', () => {
    const wrapper = mount(FreshnessBar, { props: { fetchedAt: iso(40_000), intervalMs: 15_000 } })
    expect(wrapper.classes()).toContain('is-stale')
    expect(wrapper.text()).toContain('比预期周期慢')
  })

  it('年龄要自己长大：不重新取数，5 秒后得从"3 秒前"变成"8 秒前"', async () => {
    const wrapper = mount(FreshnessBar, { props: { fetchedAt: iso(3_000) } })
    expect(wrapper.text()).toContain('3 秒前')
    await vi.advanceTimersByTimeAsync(5_000)
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).toContain('8 秒前')
  })

  it('后端给了个不像时间的字符串：按"没取到"处理，不显示 NaN', () => {
    const wrapper = mount(FreshnessBar, { props: { fetchedAt: '昨天下午' } })
    expect(wrapper.text()).toContain('尚未取到数据')
    expect(wrapper.text()).not.toContain('NaN')
  })
})
