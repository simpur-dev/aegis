/**
 * api/assistant.ts 单测：重点是「POST + SSE 自己切帧」这条路，而不是 axios 那三条。
 *
 * 手法与 `src/api/client.spec.ts` 一致（那里桩 EventSource，这里桩 fetch）：
 * `/chat` 是 POST 带 body 的 SSE，EventSource 做不到，所以替身是 `response.body.getReader()`。
 * 真正会咬人的三件事都在下面：一帧被 TCP 分片切成两半、UTF-8 中文被切成半个字、
 * 以及后端 503 的字典 detail——这三件任一处理错，现场读到的都是"一直转圈"。
 */

import type { AxiosRequestConfig } from 'axios'
import axios, { AxiosError } from 'axios'
import { describe, expect, it, vi } from 'vitest'

import type { AssistantFrame, ChatResponseLike } from './assistant'
import {
  ASSISTANT_ENDPOINTS,
  AssistantApiError,
  createAssistantApiClient,
  decodeFrame,
  isAssistantDisabled,
  isAssistantUnavailable,
  splitFrames,
  streamChat,
  toChatBody,
} from './assistant'

// ---------- 桩 ----------

function clientWith(handler: (config: AxiosRequestConfig) => { status: number; data: unknown }) {
  const seen: AxiosRequestConfig[] = []
  const instance = axios.create({
    baseURL: '/',
    adapter: async (config) => {
      seen.push(config)
      const result = handler(config)
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
  return { client: createAssistantApiClient(instance), seen }
}

const encoder = new TextEncoder()

/** 把若干段文本当作 TCP 分片喂给读者：分片边界由用例决定，正是这些用例要考的地方。 */
function streamResponse(chunks: string[], init: { ok?: boolean; status?: number; json?: unknown } = {}): ChatResponseLike {
  let index = 0
  return {
    ok: init.ok ?? true,
    status: init.status ?? 200,
    body: {
      getReader: () => ({
        read: async () => {
          if (index >= chunks.length) return { done: true, value: undefined }
          return { done: false, value: encoder.encode(chunks[index++] as string) }
        },
      }),
    },
    json: async () => {
      if (init.json === undefined) throw new Error('no json body')
      return init.json
    },
    text: async () => (typeof init.json === 'string' ? init.json : ''),
  }
}

/** 没有可读 body 的响应（连接被半路关掉的一种真实形态）。 */
function emptyBodyResponse(overrides: Partial<ChatResponseLike> = {}): ChatResponseLike {
  return { ok: true, status: 200, body: null, json: async () => ({}), text: async () => '', ...overrides }
}

async function collect(frames: AsyncGenerator<AssistantFrame>): Promise<AssistantFrame[]> {
  const seen: AssistantFrame[] = []
  for await (const frame of frames) seen.push(frame)
  return seen
}

const frame = (type: string, extra: Record<string, unknown> = {}): string => `data: ${JSON.stringify({ type, ...extra })}\n\n`

// ---------- 请求体白名单（后端 extra="forbid"） ----------

describe('toChatBody：只发后端声明过的键', () => {
  it('空字符串的可选键一律不带：带了就是 422', () => {
    expect(toChatBody({ message: '在线智能体有几个', session_id: '', reporter: '', region_code: '', hazard_hint: '' })).toEqual({
      message: '在线智能体有几个',
    })
  })

  it('有值的可选键按 snake_case 原名带出去，不改名', () => {
    expect(
      toChatBody({ message: '查预警', session_id: 'sess_1234', reporter: '值班员', region_code: '540121', hazard_hint: '泥石流' }),
    ).toEqual({ message: '查预警', session_id: 'sess_1234', reporter: '值班员', region_code: '540121', hazard_hint: '泥石流' })
  })
})

// ---------- 切帧 ----------

describe('splitFrames：注释帧跳过，半截帧留着', () => {
  it('keep-alive 注释帧不是业务事件，不产出帧也不报错', () => {
    const { frames, rest } = splitFrames(': keep-alive\n\n')
    expect(frames).toEqual([])
    expect(rest).toBe('')
  })

  it('一帧被切成两半时不产出帧，尾巴留给下一次读取', () => {
    const whole = frame('answer', { text: '共 3 条预警' })
    const first = splitFrames(whole.slice(0, 18))
    expect(first.frames).toEqual([])
    expect(first.rest.length).toBeGreaterThan(0)
    const second = splitFrames(first.rest + whole.slice(18))
    expect(second.frames).toEqual([{ type: 'answer', text: '共 3 条预警' }])
    expect(second.rest).toBe('')
  })

  it('一个块里多行 data: 是同一帧（SSE 的拼接口径），不拆成两帧', () => {
    const { frames } = splitFrames('data: {"type":"answer",\ndata: "text":"甲"}\n\n')
    expect(frames).toEqual([{ type: 'answer', text: '甲' }])
  })

  it('非 JSON 帧变成 error 帧而不是抛：对话半途崩掉没人知道为什么', () => {
    const { frames } = splitFrames('data: {不是 JSON\n\n')
    expect(frames).toHaveLength(1)
    expect(frames[0]).toMatchObject({ type: 'error' })
    expect((frames[0] as { message: string }).message).toContain('不是合法 JSON')
  })

  it.each([
    ['data: null\n\n', '不是对象'],
    ['data: {}\n\n', '缺少 type'],
    ['data: {"type":"token"}\n\n', '未识别的帧类型'],
  ])('%s 判为畸形帧：%s', (raw, hint) => {
    expect(splitFrames(raw).frames[0]).toMatchObject({ type: 'error', message: expect.stringContaining(hint) })
  })

  it('未识别的帧型也只报一次错就继续：后端加帧型要看得见，但不许把整页打挂', () => {
    const { frames } = splitFrames(frame('token', { text: '假流式' }) + frame('answer', { text: '好' }))
    expect(frames.map((item) => item.type)).toEqual(['error', 'answer'])
  })

  it('data: 后面没有空格也认（SSE 只规定冒号后的单个空格可省）', () => {
    expect(splitFrames('data:{"type":"done","session_id":"s1","latency_ms":12,"rejected_count":0}\n\n').frames).toHaveLength(1)
  })

  it('decodeFrame 保留帧里的全部字段，result 帧的 payload 不被裁掉', () => {
    const decoded = decodeFrame('data: {"type":"result","action":"query.warnings","count":2,"items":[{"warning_id":"w1"}]}'.slice(6))
    expect(decoded).toMatchObject({ type: 'result', action: 'query.warnings', count: 2 })
  })
})

// ---------- 流式对话 ----------

describe('streamChat：分片读取', () => {
  it('按后端真实帧序列产出帧，注释帧不占位', async () => {
    const frames = await collect(
      streamChat(
        { message: '在线智能体有几个', reporter: '值班员' },
        {
          fetchImpl: async () =>
            streamResponse([
              frame('meta', { session_id: 'sess_0001', llm_configured: false, actions: ['query.agents'] }),
              ': keep-alive\n\n',
              frame('intent', { action: 'query.agents', args: {}, confidence: 0.9, decided_by: 'rule', note: '', requires_confirmation: false }),
              frame('status', { text: '执行 query.agents' }) + frame('result', { action: 'query.agents', online: 3 }),
              frame('answer', { text: '3 个在线' }),
              frame('done', { session_id: 'sess_0001', latency_ms: 7, rejected_count: 0 }),
            ]),
        },
      ),
    )
    expect(frames.map((item) => item.type)).toEqual(['meta', 'intent', 'status', 'result', 'answer', 'done'])
    expect(frames[4]).toEqual({ type: 'answer', text: '3 个在线' })
  })

  it('一帧跨两次读取：拼接后才成帧，不许丢也不许半截解析', async () => {
    const whole = frame('answer', { text: '沟道泥位抬升 1.2 米' })
    const frames = await collect(
      streamChat({ message: '查预警' }, { fetchImpl: async () => streamResponse([whole.slice(0, 12), whole.slice(12)]) }),
    )
    expect(frames).toEqual([{ type: 'answer', text: '沟道泥位抬升 1.2 米' }])
  })

  it('中文按字节被切开也不产生 error 帧：后端是 ensure_ascii=False 的原文', async () => {
    const bytes = encoder.encode(frame('answer', { text: '泥石流迹象，已生成待确认动作' }))
    const frames = await collect(
      streamChat({ message: '帮我上报' }, {
        fetchImpl: async () => {
          let index = 0
          return {
            ok: true,
            status: 200,
            body: {
              getReader: () => ({
                read: async () => {
                  if (index >= bytes.length) return { done: true, value: undefined }
                  // 每次 3 字节：必然把 UTF-8 多字节字符切成两半
                  return { done: false, value: bytes.subarray(index, (index += 3)) }
                },
              }),
            },
            json: async () => ({}),
            text: async () => '',
          } as ChatResponseLike
        },
      }),
    )
    expect(frames).toEqual([{ type: 'answer', text: '泥石流迹象，已生成待确认动作' }])
  })

  it('收尾没有空行时，尾巴里那一帧仍然交出来', async () => {
    const frames = await collect(
      streamChat({ message: '帮助' }, { fetchImpl: async () => streamResponse(['data: {"type":"done","session_id":"s1","latency_ms":1,"rejected_count":0}']) }),
    )
    expect(frames).toEqual([{ type: 'done', session_id: 's1', latency_ms: 1, rejected_count: 0 }])
  })

  it('畸形帧只让那一帧变 error，后续帧照常产出', async () => {
    const frames = await collect(
      streamChat({ message: '帮助' }, { fetchImpl: async () => streamResponse(['data: 💥\n\n', frame('answer', { text: '还在' })]) }),
    )
    expect(frames.map((item) => item.type)).toEqual(['error', 'answer'])
  })

  it('POST 到 /chat，body 是白名单收敛后的 JSON', async () => {
    let seen: { input?: string; init?: RequestInit } = {}
    await collect(
      streamChat({ message: '查站点', session_id: '', reporter: '值班员', region_code: '' }, {
        fetchImpl: async (input, init) => {
          seen = { input, init }
          return streamResponse([frame('done', { session_id: 's1', latency_ms: 1, rejected_count: 0 })])
        },
      }),
    )
    expect(seen.input).toBe(ASSISTANT_ENDPOINTS.chat)
    expect(seen.init?.method).toBe('POST')
    expect(seen.init?.headers).toMatchObject({ 'Content-Type': 'application/json' })
    expect(JSON.parse(String(seen.init?.body))).toEqual({ message: '查站点', reporter: '值班员' })
  })

  it('503 的字典 detail 翻成带码错误，消息用后端原文', async () => {
    const error = await collect(
      streamChat({ message: '查站点' }, {
        fetchImpl: async () =>
          emptyBodyResponse({
            ok: false,
            status: 503,
            json: async () => ({ detail: { code: 'E_ASSISTANT_UNAVAILABLE', message: '语义交互服务未装配（assistant）' } }),
          }),
      }),
    ).catch((reason: unknown) => reason)
    expect(error).toBeInstanceOf(AssistantApiError)
    expect((error as AssistantApiError).status).toBe(503)
    expect((error as AssistantApiError).code).toBe('E_ASSISTANT_UNAVAILABLE')
    expect((error as Error).message).toBe('语义交互服务未装配（assistant）')
    expect(isAssistantUnavailable(error)).toBe(true)
  })

  it('422 这类没有 message 字段的错误体也不裸奔：给出状态码说明', async () => {
    const error = await collect(
      streamChat({ message: '' }, {
        fetchImpl: async () =>
          emptyBodyResponse({ ok: false, status: 422, json: async () => ({ detail: [{ loc: ['body', 'message'], msg: 'String should have at least 1 characters' }] }) }),
      }),
    ).catch((reason: unknown) => reason)
    expect((error as AssistantApiError).status).toBe(422)
    expect((error as Error).message).toContain('HTTP 422')
  })

  it('fetch 本身失败（后端不可达）状态码记 0', async () => {
    const error = await collect(
      streamChat({ message: '查站点' }, {
        fetchImpl: async () => {
          throw new TypeError('Failed to fetch')
        },
      }),
    ).catch((reason: unknown) => reason)
    expect((error as AssistantApiError).status).toBe(0)
    expect((error as Error).message).toContain('Failed to fetch')
  })

  it('没有可读流时明说，而不是产出一个空回复', async () => {
    await expect(collect(streamChat({ message: '查站点' }, { fetchImpl: async () => emptyBodyResponse() }))).rejects.toThrow(/可读流/)
  })

  it('环境没有 fetch 时给的是可显示的错，不是 ReferenceError', async () => {
    vi.stubGlobal('fetch', undefined)
    try {
      await expect(collect(streamChat({ message: '查站点' }))).rejects.toThrow(/不支持 fetch/)
    } finally {
      vi.unstubAllGlobals()
    }
  })
})

// ---------- 另外三条 HTTP 路由 ----------

describe('能力面 / 确认 / 会话', () => {
  const capabilities = {
    llm_configured: false,
    semantic_parser: true,
    wired_actions: { list_warnings: true },
    actions: [{ action: 'run.drill', title: '发起一次演练', requires_confirmation: true, needs: ['run_drill'], available: true, missing: [] }],
    session_ttl_seconds: 900,
    max_pending_actions: 8,
  }

  it('能力面打到真实路由，键原样保留', async () => {
    const { client, seen } = clientWith(() => ({ status: 200, data: capabilities }))
    const result = await client.capabilities()
    expect(seen[0]?.url).toBe(ASSISTANT_ENDPOINTS.capabilities)
    expect(result.llm_configured).toBe(false)
    expect(result.actions[0]?.requires_confirmation).toBe(true)
  })

  it('后端少给键时兜底成"这项没有"，而不是把 undefined 交给渲染层', async () => {
    const { client } = clientWith(() => ({ status: 200, data: { llm_configured: true, actions: null } }))
    expect(await client.capabilities()).toEqual({
      llm_configured: true,
      semantic_parser: false,
      wired_actions: {},
      actions: [],
      session_ttl_seconds: 0,
      max_pending_actions: 0,
    })
  })

  it('确认只发三个键：后端 extra="forbid"，多一个就 422', async () => {
    const { client, seen } = clientWith(() => ({ status: 200, data: { status: 'executed', action_id: 'act_abc123', action: 'run.drill' } }))
    await client.confirm({ session_id: 'sess_0001', action_id: 'act_abc123' })
    expect(JSON.parse(String(seen[0]?.data))).toEqual({ session_id: 'sess_0001', action_id: 'act_abc123' })
    expect(seen[0]?.method).toBe('post')
  })

  it('会话回读按 id 拼路径，404 保留状态码给上层区分"过期"', async () => {
    const { client, seen } = clientWith((config) =>
      String(config.url).includes('missing') ? { status: 404, data: { detail: '会话不存在或已过期' } } : { status: 200, data: { session_id: 'sess_0001', turns: 2, pending_actions: [], history: [] } },
    )
    const state = await client.session('sess_0001')
    expect(seen[0]?.url).toBe(ASSISTANT_ENDPOINTS.session('sess_0001'))
    expect(state.turns).toBe(2)
    const error = await client.session('sess_missing').catch((reason: unknown) => reason)
    expect((error as AssistantApiError).status).toBe(404)
  })

  it('路由未挂载（配置关掉助手）是 404 而不是 503：两句话不许合成一句', async () => {
    const { client } = clientWith(() => ({ status: 404, data: { detail: 'Not Found' } }))
    const error = await client.capabilities().catch((reason: unknown) => reason)
    expect(isAssistantDisabled(error)).toBe(true)
    expect(isAssistantUnavailable(error)).toBe(false)
  })

  it('503 未装配按装配事实处理', async () => {
    const { client } = clientWith(() => ({ status: 503, data: { detail: { code: 'E_ASSISTANT_UNAVAILABLE', message: '语义交互服务未装配（assistant）' } } }))
    const error = await client.capabilities().catch((reason: unknown) => reason)
    expect(isAssistantUnavailable(error)).toBe(true)
    expect(isAssistantDisabled(error)).toBe(false)
  })
})
