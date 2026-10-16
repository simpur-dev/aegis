import { onBeforeUnmount, ref } from 'vue'
import type { Ref } from 'vue'

import { tokenHeaders } from '@/api/serviceToken'

export interface StreamEvent {
  subject: string
  trace_id: string
  payload: Record<string, unknown>
  ts: string
}

/** 后端每 15 秒发一帧心跳（`api/routers/stream.py` 的 keep-alive）；这里按两刻多钟判"没动静"。 */
const HEARTBEAT_MS = 15_000
const SILENCE_TIMEOUT_MS = HEARTBEAT_MS * 2.5
const WATCHDOG_MS = 5_000
const STREAM_PATH = '/api/v1/events/stream'

export interface StreamHandle {
  events: Ref<StreamEvent[]>
  connected: Ref<boolean>
  stalled: Ref<boolean>
  lastError: Ref<string | null>
  disconnect: () => void
}

/** 只用到 `ok/status/body.getReader()`，因此测试不必造一个真 Response（与 `api/assistant.ts` 同形）。 */
export interface StreamBody {
  read(): Promise<{ done: boolean; value?: Uint8Array }>
  cancel?: (reason?: unknown) => Promise<void>
}

export interface StreamResponse {
  ok: boolean
  status: number
  body: { getReader(): StreamBody } | null
}

export type StreamFetch = (path: string, init?: { headers?: Record<string, string>; signal?: AbortSignal }) => Promise<StreamResponse>

/** 全应用一条流：句柄与"还有几个使用者"。 */
let shared: StreamHandle | null = null
let consumers = 0

/**
 * 订阅平台事件流（预警发布与反馈状态）——**全应用共享一条**。
 *
 * 为什么要点名这件事：这个 composable 原来每被调用一次就 `new EventSource` 一条流。
 * 顶栏的"事件流已连接"徽标是一份，任何页面再要一份实时事件就是第二条连接，
 * 而浏览器对同一来源的并发连接数有限（HTTP/1.1 下是 6 条）——监测页、态势总览、
 * 系统输出浮层各开一条，加上取数请求就会开始排队。所以这里改成引用计数共享：
 * 第一个调用者建流，后来者复用；最后一个卸载时才真关流并清空句柄（下一个使用者拿到干净的状态）。
 *
 * 为什么是 `fetch` 而不是 `EventSource`：`EventSource` 发不了请求头，而 §4.8.1 的服务令牌
 * 就是靠 `X-Aegis-Token` 头认的——用它的代价只能是"事件流对开了令牌的部署永久裸奔"。
 * 换 fetch 之后这一条流与其他取数同一鉴权口径（后端中间件对 `/api/` 之下不分例外）。
 *
 * 自动重连：指数退避封顶 10s；401 也重连——值班员在页脚填对令牌的下一刻，这条流自己就活了。
 * 事件历史保留最近 `limit` 条（**以第一条流的口径为准**），避免长时间断线内存增长。
 *
 * 还要盯"静默"：浏览器不会因为上游进程死了而报错——真机把后端杀掉之后，`/healthz`
 * 立刻不可达、取数一直失败，而这条流仍然报"已连接"（原先的保活是注释帧，JS 永远收不到，
 * 前端连"多久没动静"的依据都没有）。所以后端改成发一帧真心跳，这里据此判死并主动重连。
 */
export function useEventStream(limit = 200, fetchImpl?: StreamFetch): StreamHandle {
  consumers += 1
  if (shared === null) shared = createStream(limit, fetchImpl ?? browserFetch)
  const handle = shared
  onBeforeUnmount(() => {
    consumers -= 1
    if (consumers === 0 && shared !== null) {
      shared.disconnect()
      shared = null
    }
  })
  return handle
}

const browserFetch: StreamFetch = async (path, init) => {
  const response = await fetch(path, { ...init, cache: 'no-store' })
  const reader = response.body?.getReader()
  return {
    ok: response.ok,
    status: response.status,
    body: reader ? { getReader: () => ({ read: () => reader.read() }) } : null,
  }
}

/** SSE 一帧的形状：`data: {...}\n\n`，注释帧（`: keep-alive`）不参与计数。 */
function dataLines(buffer: string): { frames: string[]; rest: string } {
  const parts = buffer.split('\n\n')
  const rest = parts.pop() ?? ''
  const frames: string[] = []
  for (const part of parts) {
    const data = part
      .split('\n')
      .filter((line) => line.startsWith('data:'))
      .map((line) => line.slice(5).trim())
      .join('')
    if (data !== '') frames.push(data)
  }
  return { frames, rest }
}

function createStream(limit: number, fetchImpl: StreamFetch): StreamHandle {
  const events = ref<StreamEvent[]>([])
  const connected = ref(false)
  const stalled = ref(false)
  const lastError = ref<string | null>(null)

  let controller: AbortController | null = null
  let attempts = 0
  let closedByUser = false
  let lastBeatAt = 0
  let watchdog: ReturnType<typeof setInterval> | null = null

  async function readLoop(reader: StreamBody): Promise<void> {
    const decoder = new TextDecoder()
    let buffer = ''
    for (;;) {
      const chunk = await reader.read()
      if (chunk.done) break
      buffer += decoder.decode(chunk.value ?? new Uint8Array(), { stream: true })
      const { frames, rest } = dataLines(buffer)
      buffer = rest
      for (const frame of frames) {
        lastBeatAt = Date.now()
        stalled.value = false
        try {
          const parsed = JSON.parse(frame) as StreamEvent & { type?: string }
          // 心跳是流的证据，不是业务事件：进时间线会挤掉真发生过的链路
          if (parsed.type === 'heartbeat') continue
          events.value = [parsed, ...events.value].slice(0, limit)
        } catch {
          lastError.value = '事件解析失败（非 JSON 载荷）'
        }
      }
    }
  }

  function retryLater(): void {
    if (closedByUser) return
    attempts += 1
    const delay = Math.min(1000 * 2 ** (attempts - 1), 10_000)
    window.setTimeout(() => void connect(), delay)
  }

  async function connect(): Promise<void> {
    if (controller !== null || closedByUser) return
    const local = new AbortController()
    controller = local
    try {
      const response = await fetchImpl(STREAM_PATH, { headers: { Accept: 'text/event-stream', ...tokenHeaders() }, signal: local.signal })
      if (!response.ok) {
        connected.value = false
        // 401 单独说一句：这台开了令牌，页脚还没填对——别让值班员以为是后端挂了
        lastError.value = response.status === 401 ? '事件流被拒（401）：缺少或不对的服务令牌' : `事件流读取失败（HTTP ${response.status}）`
        if (controller === local) controller = null
        retryLater()
        return
      }
      connected.value = true
      stalled.value = false
      attempts = 0
      lastBeatAt = Date.now()
      lastError.value = null
      if (response.body === null) throw new Error('这条响应没有可读主体')
      await readLoop(response.body.getReader())
      // 后端正常收尾（例如进程重启）也要重连：一条不再有新帧的流不算"已连接"
      if (controller === local) {
        connected.value = false
        controller = null
        retryLater()
      }
    } catch (error) {
      if (controller !== local) return // 已被 disconnect/新一轮连接接管，不算故障
      connected.value = false
      controller = null
      if (closedByUser) return
      lastError.value = `事件流中断：${error instanceof Error ? error.message : String(error)}`
      retryLater()
    }
  }

  /** 静默超时：浏览器不报错的那一类死法，只能自己判。判死就重连，别抱着一条僵尸流说"已连接"。 */
  function watch(): void {
    if (watchdog !== null) return
    watchdog = setInterval(() => {
      if (!connected.value || closedByUser) return
      if (Date.now() - lastBeatAt < SILENCE_TIMEOUT_MS) return
      stalled.value = true
      connected.value = false
      const local = controller
      controller = null
      local?.abort()
      attempts = 0
      void connect()
    }, WATCHDOG_MS)
  }

  function disconnect(): void {
    closedByUser = true
    if (watchdog !== null) {
      clearInterval(watchdog)
      watchdog = null
    }
    const local = controller
    controller = null
    local?.abort()
    connected.value = false
  }

  // 关流交给 useEventStream 的引用计数：这里再挂一个 onBeforeUnmount 的话，
  // 第一个使用者卸载就把共享流掐了，后面的人拿到一条已关的流（实测三条用例因此红）。
  void connect()
  watch()

  return { events, connected, stalled, lastError, disconnect }
}
