import { onBeforeUnmount, ref } from 'vue'
import type { Ref } from 'vue'

export interface StreamEvent {
  subject: string
  trace_id: string
  payload: Record<string, unknown>
  ts: string
}

/** 后端每 15 秒发一帧心跳（`app.py` 的 keep-alive）；这里按两刻多钟判"没动静"。 */
const HEARTBEAT_MS = 15_000
const SILENCE_TIMEOUT_MS = HEARTBEAT_MS * 2.5
const WATCHDOG_MS = 5_000

export interface StreamHandle {
  events: Ref<StreamEvent[]>
  connected: Ref<boolean>
  stalled: Ref<boolean>
  lastError: Ref<string | null>
  disconnect: () => void
}

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
 * 自动重连：指数退避封顶 10s；事件历史保留最近 `limit` 条（**以第一条流的口径为准**），
 * 避免长时间断线内存增长。
 *
 * 还要盯"静默"：浏览器不会因为上游进程死了而报错——真机把后端杀掉之后，`/healthz`
 * 立刻不可达、取数一直失败，而这条流仍然报"已连接"（原先的保活是注释帧，JS 永远收不到，
 * 前端连"多久没动静"的依据都没有）。所以后端改成发一帧真心跳，这里据此判死并主动重连。
 */
export function useEventStream(limit = 200): StreamHandle {
  consumers += 1
  if (shared === null) shared = createStream(limit)
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

function createStream(limit: number): StreamHandle {
  const events = ref<StreamEvent[]>([])
  const connected = ref(false)
  const stalled = ref(false)
  const lastError = ref<string | null>(null)

  let source: EventSource | null = null
  let attempts = 0
  let closedByUser = false
  let lastBeatAt = 0
  let watchdog: ReturnType<typeof setInterval> | null = null

  function connect(): void {
    if (source || closedByUser || typeof EventSource === 'undefined') return
    source = new EventSource('/api/v1/events/stream')
    source.onopen = () => {
      connected.value = true
      stalled.value = false
      attempts = 0
      lastError.value = null
      lastBeatAt = Date.now()
    }
    source.onmessage = (message: MessageEvent<string>) => {
      lastBeatAt = Date.now()
      stalled.value = false
      try {
        const parsed = JSON.parse(message.data) as StreamEvent & { type?: string }
        // 心跳是流的证据，不是业务事件：进时间线会挤掉真发生过的链路
        if (parsed.type === 'heartbeat') return
        events.value = [parsed, ...events.value].slice(0, limit)
      } catch {
        lastError.value = '事件解析失败（非 JSON 载荷）'
      }
    }
    source.onerror = () => {
      connected.value = false
      source?.close()
      source = null
      if (closedByUser) return
      attempts += 1
      const delay = Math.min(1000 * 2 ** (attempts - 1), 10_000)
      window.setTimeout(connect, delay)
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
      source?.close()
      source = null
      attempts = 0
      connect()
    }, WATCHDOG_MS)
  }

  function disconnect(): void {
    closedByUser = true
    if (watchdog !== null) {
      clearInterval(watchdog)
      watchdog = null
    }
    source?.close()
    source = null
    connected.value = false
  }

  // 关流交给 useEventStream 的引用计数：这里再挂一个 onBeforeUnmount 的话，
  // 第一个使用者卸载就把共享流掐了，后面的人拿到一条已关的流（实测三条用例因此红）。
  connect()
  watch()

  return { events, connected, stalled, lastError, disconnect }
}
