/**
 * 抽屉壳的组件测试：它接管了 `--assistant-drawer-w` 与 `assistant-drawer-open` 两个全局契约，
 * 但在此之前一个用例都没有——开合坏了，只有真机点一下才知道。
 *
 * 会话内容用 stub 替掉：这里测的是"壳"，不是 Panel（Panel 的渲染由 `AssistantView.spec.ts` 覆盖）。
 */

import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it } from 'vitest'

import AssistantDock from './AssistantDock.vue'

const STUBS = {
  AssistantPanel: {
    name: 'AssistantPanel',
    props: ['mode'],
    template: '<div class="panel-stub" :data-mode="mode" />',
  },
}

function render() {
  return mount(AssistantDock, { global: { stubs: STUBS }, attachTo: document.body })
}

async function clickDock(wrapper: ReturnType<typeof render>, clientY = 400): Promise<void> {
  const dock = wrapper.find('[data-testid="assistant-dock"]')
  await dock.trigger('pointerdown', { button: 0, pointerId: 1, clientY })
  await dock.trigger('pointerup', { button: 0, pointerId: 1, clientY })
}

describe('AssistantDock 的开合与容器契约', () => {
  beforeEach(() => {
    localStorage.clear()
    document.documentElement.className = ''
    document.documentElement.style.removeProperty('--assistant-drawer-w')
  })

  it('点图标开抽屉，抽屉里挂的是同一个 AssistantPanel（dock 模式）', async () => {
    const wrapper = render()
    const drawer = wrapper.find('[data-testid="assistant-drawer"]')
    expect(drawer.attributes('aria-hidden')).toBe('true')

    await clickDock(wrapper)

    expect(drawer.attributes('aria-hidden')).toBe('false')
    expect(drawer.classes()).toContain('is-open')
    expect(wrapper.find('.panel-stub').attributes('data-mode')).toBe('dock')
    wrapper.unmount()
  })

  it('开抽屉时给根元素挂上让位类，关掉就摘掉', async () => {
    const wrapper = render()
    await clickDock(wrapper)
    expect(document.documentElement.classList.contains('assistant-drawer-open')).toBe(true)

    await clickDock(wrapper)
    expect(document.documentElement.classList.contains('assistant-drawer-open')).toBe(false)
    wrapper.unmount()
  })

  it('Esc 关闭抽屉', async () => {
    const wrapper = render()
    await clickDock(wrapper)
    expect(wrapper.find('[data-testid="assistant-drawer"]').classes()).toContain('is-open')

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await wrapper.vm.$nextTick()

    expect(wrapper.find('[data-testid="assistant-drawer"]').classes()).not.toContain('is-open')
    wrapper.unmount()
  })

  it('开关状态写进 localStorage，重挂后还原', async () => {
    const first = render()
    await clickDock(first)
    const saved = JSON.parse(localStorage.getItem('aegis.assistant.dock') ?? '{}') as { open?: boolean }
    expect(saved.open).toBe(true)
    first.unmount()

    const second = render()
    await second.vm.$nextTick()
    expect(second.find('[data-testid="assistant-drawer"]').classes()).toContain('is-open')
    second.unmount()
  })

  it('卸载时把全局变量与类一起收干净（不给下一页留残留宽度）', async () => {
    const wrapper = render()
    await clickDock(wrapper)
    expect(document.documentElement.style.getPropertyValue('--assistant-drawer-w')).not.toBe('')

    wrapper.unmount()
    expect(document.documentElement.style.getPropertyValue('--assistant-drawer-w')).toBe('')
    expect(document.documentElement.classList.contains('assistant-drawer-open')).toBe(false)
  })
})
