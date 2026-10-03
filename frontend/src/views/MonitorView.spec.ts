import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import api from '@/api/client'
import type { TelemetryReading } from '@/api/types'
import MonitorView from '@/views/MonitorView.vue'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return { default: { telemetry: vi.fn(), drill: vi.fn() }, ApiError: actual.ApiError }
})

const mockedTelemetry = vi.mocked(api.telemetry)

const STUBS = {
  EChart: true,
  'a-card': { name: 'ACard', props: ['title', 'loading', 'size'], template: '<div class="card"><slot /><slot name="extra" /></div>' },
  'a-space': { name: 'ASpace', props: ['wrap'], template: '<div class="space"><slot /></div>' },
  'a-select': { name: 'ASelect', props: ['value', 'placeholder'], emits: ['update:value'], template: '<div class="select"><slot /></div>' },
  'a-select-option': { name: 'ASelectOption', props: ['value'], template: '<span class="option"><slot /></span>' },
  'a-button': { name: 'AButton', props: ['loading', 'type'], template: '<button class="button"><slot /></button>' },
  'a-row': { name: 'ARow', props: ['gutter'], template: '<div class="row"><slot /></div>' },
  'a-col': { name: 'ACol', props: ['span'], template: '<div class="col"><slot /></div>' },
  'a-table': { name: 'ATable', props: ['columns', 'dataSource', 'pagination', 'rowKey', 'size'], template: '<div class="table" />' },
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
    expect(mockedTelemetry).toHaveBeenCalledWith({ limit: 2_000 })
    await wrapper.find('[data-testid="open-report"]').trigger('click')
    await flushPromises()
    const labels = wrapper.findAll('.button').map((button) => button.text())
    expect(labels).toEqual(expect.arrayContaining(['刷新', '人工上报', '发起灾害演练（激增）', '发起背景演练（正常）']))
  })
})
