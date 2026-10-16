import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it } from 'vitest'
import { nextTick } from 'vue'

import ServiceTokenBar from './ServiceTokenBar.vue'
import { authRejected, clearServiceToken, serviceToken, setServiceToken } from '@/api/serviceToken'

describe('ServiceTokenBar 服务令牌条', () => {
  beforeEach(() => {
    clearServiceToken()
    authRejected.value = false
  })

  it('未设置时说清"这台开了鉴权才要填"，且不给清除钮', () => {
    const wrapper = mount(ServiceTokenBar)
    expect(wrapper.get('[data-testid="service-token-hint"]').text()).toContain('未设置')
    expect(wrapper.find('[data-testid="service-token-clear"]').exists()).toBe(false)
  })

  it('填进去点保存才落地：状态、localStorage 与清除钮同时跟上', async () => {
    const wrapper = mount(ServiceTokenBar)
    await wrapper.get('[data-testid="service-token-input"]').setValue('  s3cr3t ')
    expect(serviceToken.value, '没点保存之前不许偷偷改全局状态').toBe('')

    await wrapper.get('[data-testid="service-token-save"]').trigger('click')
    await nextTick()
    expect(serviceToken.value).toBe('s3cr3t')
    expect(wrapper.get('[data-testid="service-token-hint"]').text()).toContain('已保存')
    expect(wrapper.find('[data-testid="service-token-clear"]').exists()).toBe(true)
  })

  it('被 401 拒过时换成"该核对令牌"这句话（不是"后端挂了"）', async () => {
    setServiceToken('旧的')
    const wrapper = mount(ServiceTokenBar)
    authRejected.value = true
    await nextTick()

    expect(wrapper.get('[data-testid="service-token-hint"]').text()).toContain('401')
    expect(wrapper.get('[data-testid="service-token-hint"]').text()).toContain('令牌')
    expect(wrapper.get('[data-testid="service-token-bar"]').classes()).toContain('is-rejected')
  })

  it('点清除是真的清掉，不留一个空串占位', async () => {
    setServiceToken('s3cr3t')
    const wrapper = mount(ServiceTokenBar)
    await wrapper.get('[data-testid="service-token-clear"]').trigger('click')
    expect(serviceToken.value).toBe('')
    expect((wrapper.get('[data-testid="service-token-input"]').element as HTMLInputElement).value).toBe('')
  })
})
