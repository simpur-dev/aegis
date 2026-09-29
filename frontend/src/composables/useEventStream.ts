import { onBeforeUnmount, ref } from 'vue'

export interface StreamEvent {
  subject: string
  trace_id: string
  payload: Record<string, unknown>
  ts: string
}

/**
 * 订阅平台 SSE 事件流（预警发布与反馈状态）。
 * 自动重连：指数退避封顶 10s，事件历史保留最近 200 条，避免长时间断线内存增长。
 */
export function useEventStream(limit = 200) {
  const events = ref<StreamEvent[]>([])
  const connected = ref(false)
  const lastError = ref<string | null>(null)

  let source: EventSource | null = null
  let attempts = 0
  let closedByUser = false

  function connect(): void {
    if (source || closedByUser || typeof EventSource === 'undefined') return
    source = new EventSource('/api/v1/events/stream')
    source.onopen = () => {
      connected.value = true
      attempts = 0
      lastError.value = null
    }
    source.onmessage = (message: MessageEvent<string>) => {
      try {
        const parsed = JSON.parse(message.data) as StreamEvent
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

  function disconnect(): void {
    closedByUser = true
    source?.close()
    source = null
    connected.value = false
  }

  onBeforeUnmount(disconnect)
  connect()

  return { events, connected, lastError, disconnect }
}
