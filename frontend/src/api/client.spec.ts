import { mount } from '@vue/test-utils'
import axios, { AxiosError } from 'axios'
import { defineComponent, nextTick } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import { createApiClient } from './client'
import { HAZARD_LABELS, RISK_LABELS } from './types'
import { useEventStream } from '@/composables/useEventStream'

/** 每个用例注入独立适配器：不依赖真实后端，也不会污染其他用例。 */
function clientWith(handler: (url: string) => { status: number; data: unknown }) {
  const instance = axios.create({
    baseURL: '/',
    adapter: async (config) => {
      const result = handler(String(config.url ?? ''))
      if (result.status >= 400) {
        throw new AxiosError('request failed', 'ERR_BAD_REQUEST', config, {}, {
          status: result.status,
          statusText: 'error',
          headers: {},
          data: result.data,
          config,
        })
      }
      return { data: result.data, status: result.status, statusText: 'OK', headers: {}, config }
    },
  })
  return createApiClient(instance)
}

describe('api client', () => {
  it('遥测查询返回后端结构', async () => {
    const client = clientWith(() => ({ status: 200, data: { count: 1, items: [{ station_id: 'RG-01' }] } }))
    const result = await client.telemetry({ limit: 10 })
    expect(result.count).toBe(1)
    expect(result.items[0].station_id).toBe('RG-01')
  })

  it('预警详情走路径参数', async () => {
    const client = clientWith((url) => ({ status: 200, data: { warning_id: url.split('/').pop() } }))
    expect((await client.warning('wrn_abc')).warning_id).toBe('wrn_abc')
  })

  it('422 被包装为 ApiError 并保留状态码与 detail', async () => {
    const client = clientWith(() => ({ status: 422, data: { detail: 'limit 必须为正' } }))
    const error = await client.telemetry({ limit: 0 }).catch((e: unknown) => e)
    expect(error).toBeInstanceOf(Error)
    expect((error as { status: number }).status).toBe(422)
    expect((error as { detail: unknown }).detail).toEqual({ detail: 'limit 必须为正' })
  })

  it('404 状态码可被上层区分', async () => {
    const client = clientWith(() => ({ status: 404, data: { detail: '预警不存在' } }))
    const error = await client.warning('wrn_missing').catch((e: unknown) => e)
    expect((error as { status: number }).status).toBe(404)
  })

  it('网络不可达时状态码为 0', async () => {
    const instance = axios.create({
      adapter: async () => {
        throw new AxiosError('connect ECONNREFUSED', 'ERR_NETWORK')
      },
    })
    const error = await createApiClient(instance).health().catch((e: unknown) => e)
    expect((error as { status: number }).status).toBe(0)
  })

  it('演练以 POST + body 提交', async () => {
    let seen: { method?: string; url?: string; body: unknown } = { body: null }
    const instance = axios.create({
      adapter: async (config) => {
        seen = { method: config.method, url: config.url, body: config.data }
        return { data: { ingest: {}, chains: [], regions: 0 }, status: 200, statusText: 'OK', headers: {}, config }
      },
    })
    await createApiClient(instance).drill({ scenario: 'surge', ticks: 2 })
    expect(seen.method).toBe('post')
    expect(seen.url).toBe('/api/v1/drill/run')
    const body = typeof seen.body === 'string' ? JSON.parse(seen.body) : seen.body
    expect(body).toMatchObject({ scenario: 'surge', ticks: 2 })
  })
})

describe('契约常量与后端口径一致', () => {
  it('灾种枚举覆盖 5 类高原灾种', () => {
    for (const hazard of ['landslide', 'rockfall', 'debris_flow', 'avalanche', 'lake_outburst']) {
      expect(HAZARD_LABELS[hazard as keyof typeof HAZARD_LABELS]).toBeTruthy()
    }
  })

  it('风险等级 1-5 全有中文标签，1 为红色最高', () => {
    expect(RISK_LABELS[1]).toBe('红色')
    expect(RISK_LABELS[5]).toBe('无风险')
    expect(Object.keys(RISK_LABELS)).toHaveLength(5)
  })
})

describe('useEventStream', () => {
  class FakeEventSource {
    static latest: FakeEventSource | null = null
    onopen: (() => void) | null = null
    onmessage: ((event: { data: string }) => void) | null = null
    onerror: (() => void) | null = null
    closed = false
    constructor() {
      FakeEventSource.latest = this
    }
    close(): void {
      this.closed = true
    }
    emit(data: unknown): void {
      this.onmessage?.({ data: JSON.stringify(data) })
    }
    raw(data: string): void {
      this.onmessage?.({ data })
    }
  }

  const Host = defineComponent({
    setup() {
      return useEventStream(2)
    },
    render: () => null,
  })

  it('解析事件并按上限丢弃旧事件', async () => {
    vi.stubGlobal('EventSource', FakeEventSource)
    const wrapper = mount(Host)
    const source = FakeEventSource.latest as unknown as FakeEventSource

    for (let i = 0; i < 5; i += 1) {
      source.emit({ subject: 'platform.alert', trace_id: `t${i}`, payload: { warning_id: `w${i}` }, ts: 'now' })
    }
    await nextTick()

    const vm = wrapper.vm as unknown as { events: { payload: { warning_id: string } }[] }
    expect(vm.events).toHaveLength(2)
    expect(vm.events[0].payload.warning_id).toBe('w4')

    wrapper.unmount()
    expect(source.closed).toBe(true)
    vi.unstubAllGlobals()
  })

  it('非法 JSON 载荷不崩，记录错误', async () => {
    vi.stubGlobal('EventSource', FakeEventSource)
    const wrapper = mount(Host)
    const source = FakeEventSource.latest as unknown as FakeEventSource

    source.raw('not-json')
    await nextTick()

    const vm = wrapper.vm as unknown as { events: unknown[]; lastError: string | null }
    expect(vm.events).toHaveLength(0)
    expect(vm.lastError).toContain('解析失败')
    wrapper.unmount()
    vi.unstubAllGlobals()
  })
})
