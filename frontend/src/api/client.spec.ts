import { mount } from '@vue/test-utils'
import axios, { AxiosError } from 'axios'
import { defineComponent } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { createApiClient } from './client'
import { authRejected, clearServiceToken, setServiceToken } from './serviceToken'
import { HAZARD_LABELS, RISK_LABELS } from './types'
import { useEventStream, type StreamResponse } from '@/composables/useEventStream'

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
/**
 * `useEventStream` 的替身 fetch：一帧一帧往里推，没有就先等——与真 SSE 的"读一条等一条"同形。
 *
 * 为什么不再是 `EventSource` 的桩：这条流改成 `fetch` 了（要能带服务令牌头），
 * 桩的形态因此跟着换；`latest` / `forceStatus` 保留下来，测试仍然能逐条指着"第几条流"说话。
 */
class FakeStream {
  static latest: FakeStream | null = null
  /** 下一条流以这个状态码回应（200 = 正常）。用来演"开了令牌但没填对"的现场。 */
  static forceStatus = 200

  readonly headers: Record<string, string>
  aborted = false
  private readonly status: number
  private pending: string[] = []
  private waiting: ((chunk: { done: boolean; value?: Uint8Array }) => void) | null = null

  constructor(init?: { headers?: Record<string, string>; signal?: AbortSignal }) {
    this.headers = { ...(init?.headers ?? {}) }
    this.status = FakeStream.forceStatus
    init?.signal?.addEventListener('abort', () => {
      this.aborted = true
      this.pending = []
      this.waiting?.({ done: true })
      this.waiting = null
    })
    FakeStream.latest = this
  }

  /** 推一段原始文本：可以多帧，也可以是半帧（半帧用来验"跨 chunk 的帧不被吞"）。 */
  push(text: string): void {
    this.pending.push(text)
    this.drain()
  }

  emit(event: unknown): void {
    this.push(`data: ${JSON.stringify(event)}\n\n`)
  }

  private drain(): void {
    while (this.waiting !== null && this.pending.length > 0) {
      const next = this.pending.shift() as string
      const settle = this.waiting
      this.waiting = null
      settle({ done: false, value: new TextEncoder().encode(next) })
    }
  }

  private read(): Promise<{ done: boolean; value?: Uint8Array }> {
    if (this.pending.length > 0) {
      const next = this.pending.shift() as string
      return Promise.resolve({ done: false, value: new TextEncoder().encode(next) })
    }
    if (this.aborted) return Promise.resolve({ done: true })
    return new Promise((resolve) => {
      this.waiting = resolve
    })
  }

  get response(): StreamResponse {
    if (this.status !== 200) return { ok: false, status: this.status, body: null }
    return { ok: true, status: this.status, body: { getReader: () => ({ read: () => this.read() }) } }
  }
}

let seenPaths: string[] = []

function fetchStream(path: string, init?: { headers?: Record<string, string>; signal?: AbortSignal }): Promise<StreamResponse> {
  seenPaths.push(path)
  return Promise.resolve(new FakeStream(init).response)
}

const Host = defineComponent({
  setup() {
    return useEventStream(2, fetchStream)
  },
  render: () => null,
})

/**
 * 微任务冲刷：读一条等一条的循环里，每一帧要好几步 await 才落到 `events`。
 * 步数给足——掐着算会测出"事件没进来"这种假红，而那是最难查的一类红。
 */
async function flush(): Promise<void> {
  for (let i = 0; i < 50; i += 1) await Promise.resolve()
}

/** 卸载放进 finally：一条用例断言失败时不许把共享的那条流留给后面的用例（否则一处红变成处处红）。 */
async function withStream(run: (ctx: { vm: Record<string, unknown>; source: FakeStream }) => Promise<void>): Promise<void> {
  const wrapper = mount(Host)
  await flush()
  try {
    await run({ vm: wrapper.vm as unknown as Record<string, unknown>, source: FakeStream.latest as FakeStream })
  } finally {
    wrapper.unmount()
    await flush()
  }
}

describe('useEventStream', () => {
  beforeEach(() => {
    seenPaths = []
    FakeStream.latest = null
    FakeStream.forceStatus = 200
    clearServiceToken()
    authRejected.value = false
  })

  it('解析事件并按上限丢弃旧事件', () =>
    withStream(async ({ vm, source }) => {
      for (let i = 0; i < 5; i += 1) source.emit({ subject: 'platform.alert', trace_id: `t${i}`, payload: { warning_id: `w${i}` }, ts: 'now' })
      await flush()

      const events = vm.events as { payload: { warning_id: string } }[]
      expect(events).toHaveLength(2)
      expect(events[0].payload.warning_id).toBe('w4')
    }))

  it('跨 chunk 的半帧不吞事件，也不把半帧当载荷解析', () =>
    withStream(async ({ vm, source }) => {
      source.push('data: {"subject":"platform.alert","tra')
      await flush()
      expect(vm.events).toHaveLength(0)

      source.push('ce_id":"t9","payload":{"warning_id":"w9"},"ts":"now"}\n\n')
      await flush()
      expect(vm.events).toHaveLength(1)
      expect(vm.lastError, '半帧不该被当成一条坏载荷').toBeNull()
    }))

  it('非法 JSON 载荷不崩，记录错误', () =>
    withStream(async ({ vm, source }) => {
      source.push('data: not-json\n\n')
      await flush()
      expect(vm.events).toHaveLength(0)
      expect(vm.lastError).toContain('解析失败')
    }))

  /**
   * 全应用只该有一条流：顶栏徽标是一份，页面里再要实时事件是第二份——浏览器对同一来源
   * 的并发连接有限（HTTP/1.1 是 6 条），多开一条就在挤占取数请求的位置。
   */
  it('两个使用者共用一条流；最后一个卸载才关，之后再挂是全新一条', async () => {
    const first = mount(Host)
    const second = mount(Host)
    await flush()
    const source = FakeStream.latest as FakeStream
    expect(seenPaths, '两条流就违背了引用计数共享的初衷').toHaveLength(1)
    const eventsOf = (wrapper: typeof first): unknown[] => (wrapper.vm as unknown as { events: unknown[] }).events

    try {
      source.emit({ subject: 'platform.alert', trace_id: 't1', payload: { warning_id: 'w1' }, ts: 'now' })
      await flush()
      expect(eventsOf(first)).toHaveLength(1)
      expect(eventsOf(second)).toHaveLength(1)

      first.unmount()
      expect(source.aborted, '还有一个使用者在，不该关流').toBe(false)
      source.emit({ subject: 'platform.alert', trace_id: 't2', payload: { warning_id: 'w2' }, ts: 'now' })
      await flush()
      expect(eventsOf(second)).toHaveLength(2)
    } finally {
      second.unmount()
      await flush()
    }
    expect(source.aborted, '最后一个使用者走了还不关流，就是漏了一条长连接').toBe(true)

    const third = mount(Host)
    await flush()
    try {
      expect(FakeStream.latest, '重新挂载应当另起一条流').not.toBe(source)
      expect((third.vm as unknown as { events: unknown[] }).events, '新一条流不许带着上一条的历史').toHaveLength(0)
    } finally {
      third.unmount()
      await flush()
    }
  })

  /**
   * 真机杀后端进程量到的：`/healthz` 已经不可达，页头的 SSE 徽标还写"事件流已连接"——
   * 浏览器不会因为上游死了而报错，而原先的保活是注释帧，JS 根本收不到，前端连
   * "多久没动静"的依据都没有。现在后端发真心跳，前端据此判死并重连。
   */
  it('心跳帧只当"流还活着"的证据，不进时间线', () =>
    withStream(async ({ vm, source }) => {
      source.emit({ type: 'heartbeat', ts: '2026-10-04T15:00:00+00:00' })
      await flush()
      expect(vm.events).toHaveLength(0)
      expect(vm.connected).toBe(true)
    }))

  it('心跳静默就判死并另起一条流，不再抱着僵尸流说"已连接"', async () => {
    vi.useFakeTimers()
    const wrapper = mount(Host)
    try {
      await vi.advanceTimersByTimeAsync(0)
      const first = FakeStream.latest as FakeStream
      const vm = wrapper.vm as unknown as { connected: boolean; stalled: boolean }
      expect(vm.connected).toBe(true)

      // 20 秒还没到判死线（15s 心跳的两刻多钟 = 37.5s）：这时掐流就是反应过度
      await vi.advanceTimersByTimeAsync(20_000)
      expect(vm.stalled, '没到静默线就判死，会把正常抖动的流掐掉').toBe(false)
      expect(first.aborted).toBe(false)

      // 过了线：这条僵尸流必须被掐掉并另起一条。新一条连上之后 stalled 归位是设计，
      // 所以这里断言的是"判死并另起"这个动作本身，不是那一瞬间的旗标。
      await vi.advanceTimersByTimeAsync(20_000)
      expect(first.aborted, '两刻多钟没心跳还不掐流，就是抱着僵尸流说"已连接"').toBe(true)
      expect(FakeStream.latest).not.toBe(first)
    } finally {
      wrapper.unmount()
      vi.useRealTimers()
    }
  })

  it('一路有心跳就不判死（空转一小时也是连着）', async () => {
    vi.useFakeTimers()
    const wrapper = mount(Host)
    try {
      await vi.advanceTimersByTimeAsync(0)
      const source = FakeStream.latest as FakeStream
      const vm = wrapper.vm as unknown as { connected: boolean; stalled: boolean }
      for (let minute = 0; minute < 6; minute += 1) {
        await vi.advanceTimersByTimeAsync(15_000)
        source.emit({ type: 'heartbeat', ts: 'tick' })
        await vi.advanceTimersByTimeAsync(0)
      }
      expect(vm.stalled).toBe(false)
      expect(vm.connected).toBe(true)
    } finally {
      wrapper.unmount()
      vi.useRealTimers()
    }
  })

  /** §4.8.1：这条流以前用 EventSource，带不了请求头——开了服务令牌的部署里它只能永久裸奔。 */
  it('带上服务令牌发起，路径仍是那一条事件流', async () => {
    setServiceToken('s3cr3t')
    const wrapper = mount(Host)
    await flush()
    try {
      const source = FakeStream.latest as FakeStream
      expect(seenPaths).toEqual(['/api/v1/events/stream'])
      expect(source.headers['X-Aegis-Token']).toBe('s3cr3t')
      expect(source.headers['Accept'], 'SSE 要显式声明接的是什么').toBe('text/event-stream')
    } finally {
      wrapper.unmount()
      await flush()
    }
  })

  it('令牌不对时说出 401 是令牌问题，不谎称后端挂了', async () => {
    FakeStream.forceStatus = 401
    const wrapper = mount(Host)
    await flush()
    try {
      const vm = wrapper.vm as unknown as { connected: boolean; lastError: string | null }
      expect(vm.connected).toBe(false)
      expect(vm.lastError).toContain('401')
      expect(vm.lastError, '401 的处置动作是填令牌，不是等后端恢复').toContain('令牌')
    } finally {
      wrapper.unmount()
      await flush()
    }
  })

  it('未带令牌时不硬塞一个空头（后端把空凭据与无凭据一视同仁）', () =>
    withStream(async ({ source }) => {
      expect(source.headers['X-Aegis-Token']).toBeUndefined()
    }))
})

