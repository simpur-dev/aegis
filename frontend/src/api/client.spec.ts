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

  /**
   * 八张页的读接口都走这份客户端，此前它们出错时界面直接打 axios 那句英文：
   * 真机巡检时 `/warnings` 是"加载预警失败：Request failed with status code 422"、
   * `/map` 是"数据读取失败：Request failed…"、`/metrics` 是"读不到账本：Request failed…"。
   * 三张页三种前缀，后面挂的都是同一句工程师话。
   */
  it('422 的数组原因翻成一句中文界面话，字段名带查询参数标签', async () => {
    const client = clientWith(() => ({
      status: 422,
      data: { detail: [{ loc: ['query', 'limit'], msg: 'Input should be greater than 0', type: 'greater_than' }] },
    }))
    const error = (await client.telemetry({ limit: 0 }).catch((e: unknown) => e)) as Error
    expect(error.message).toBe('接口调用失败（HTTP 422）：条数上限（limit）：Input should be greater than 0')
  })

  it('字符串 detail（404 的业务事实）原样带出，不裹成 [object Object]', async () => {
    const client = clientWith(() => ({ status: 404, data: { detail: '预警不存在' } }))
    const error = (await client.warning('wrn_missing').catch((e: unknown) => e)) as Error
    expect(error.message).toBe('接口调用失败（HTTP 404）：预警不存在')
  })

  it('认不出形状的响应体不编原因：留住 axios 那句话', async () => {
    const client = clientWith(() => ({ status: 500, data: {} }))
    const error = (await client.health().catch((e: unknown) => e)) as Error & { detail: unknown }
    expect(error.message).toBe('接口调用失败（HTTP 500）：request failed')
    expect(error.detail).toEqual({})
  })

  /**
   * 账本没声明单位就在边界上判成读失败。
   *
   * 放过去只有两种下游结果：渲染层抛错把整页打空，或者猜一个单位把 0.019 秒显示成
   * 0.019 毫秒——后者正是这条链已经出过一次的那个数。宁可是"这页读不到数"这句话。
   */
  it('时延账本缺单位声明时判为读失败，并点名是哪条指标', async () => {
    const client = clientWith(() => ({
      status: 200,
      data: {
        sla_thresholds: {},
        metrics: { ingest_end_to_end_seconds: { count: 3, p50: 0.01, p95: 0.02, p99: 0.02, max: 0.02, mean: 0.012 } },
        collaboration: { transactions: 0, success_rate: null, target: 0.9, pass: false },
        violations: {},
        gateway_counters: {},
        store: {},
      },
    }))
    const error = await client.latency().catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(Error)
    expect((error as Error).message).toContain('ingest_end_to_end_seconds')
    expect((error as Error).message).toContain('没有声明单位')
  })

  it('单位声明合法（ms / s）时原样放行，不改一个数', async () => {
    const metrics = {
      ingest_end_to_end_seconds: { count: 3, unit: 's', p50: 0.01, p95: 0.02, p99: 0.02, max: 0.02, mean: 0.012, budget: 300 },
      warning_generation_ms: { count: 2, unit: 'ms', p50: 4000, p95: 5000, p99: 5000, max: 5000, mean: 4500 },
    }
    const client = clientWith(() => ({
      status: 200,
      data: {
        sla_thresholds: {},
        metrics,
        collaboration: { transactions: 0, success_rate: null, target: 0.9, pass: false },
        violations: {},
        gateway_counters: {},
        store: {},
      },
    }))
    const report = await client.latency()
    expect(report.metrics.ingest_end_to_end_seconds.p95).toBe(0.02)
    expect(report.metrics.warning_generation_ms.unit).toBe('ms')
  })

  it('未知的第三种单位也拦下（不能默认它是毫秒）', async () => {
    const client = clientWith(() => ({
      status: 200,
      data: {
        sla_thresholds: {},
        metrics: { collab_txn: { count: 1, unit: 'min', p50: 1, p95: 1, p99: 1, max: 1, mean: 1 } },
        collaboration: { transactions: 0, success_rate: null, target: 0.9, pass: false },
        violations: {},
        gateway_counters: {},
        store: {},
      },
    }))
    const error = await client.latency().catch((caught: unknown) => caught)
    expect((error as Error).message).toContain('只接受 ms / s')
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

  /**
   * 真机杀后端进程量到的：`/healthz` 已经不可达，页头的 SSE 徽标还写"事件流已连接"——
   * 浏览器不会因为上游死了而报错，而原先的保活是注释帧，JS 根本收不到，前端连
   * "多久没动静"的依据都没有。现在后端发真心跳，前端据此判死并重连。
   */
  it('心跳帧只当"流还活着"的证据，不进时间线', async () => {
    vi.stubGlobal('EventSource', FakeEventSource)
    const wrapper = mount(Host)
    const source = FakeEventSource.latest as unknown as FakeEventSource
    source.onopen?.()
    source.emit({ type: 'heartbeat', ts: '2026-10-04T15:00:00+00:00' })
    await nextTick()
    const vm = wrapper.vm as unknown as { events: unknown[]; connected: boolean }
    expect(vm.events).toHaveLength(0)
    expect(vm.connected).toBe(true)
    wrapper.unmount()
    vi.unstubAllGlobals()
  })

  it('心跳静默就判死并另起一条流，不再抱着僵尸流说"已连接"', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('EventSource', FakeEventSource)
    try {
      const wrapper = mount(Host)
      const first = FakeEventSource.latest as unknown as FakeEventSource
      first.onopen?.()
      const vm = wrapper.vm as unknown as { connected: boolean; stalled: boolean }
      expect(vm.connected).toBe(true)

      vi.advanceTimersByTime(40_000)
      expect(vm.stalled, '两刻多钟没心跳，这条流已经是僵尸').toBe(true)
      expect(vm.connected).toBe(false)
      expect(first.closed).toBe(true)
      expect(FakeEventSource.latest).not.toBe(first)
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
      vi.unstubAllGlobals()
    }
  })

  it('一路有心跳就不判死（空转一小时也是连着）', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('EventSource', FakeEventSource)
    try {
      const wrapper = mount(Host)
      const source = FakeEventSource.latest as unknown as FakeEventSource
      source.onopen?.()
      const vm = wrapper.vm as unknown as { connected: boolean; stalled: boolean }
      for (let minute = 0; minute < 6; minute += 1) {
        vi.advanceTimersByTime(15_000)
        source.emit({ type: 'heartbeat', ts: 'tick' })
      }
      expect(vm.stalled).toBe(false)
      expect(vm.connected).toBe(true)
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
      vi.unstubAllGlobals()
    }
  })
})
