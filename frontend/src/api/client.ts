import axios, { AxiosError, type AxiosInstance, type AxiosRequestConfig } from 'axios'

import type {
  AgentInfo,
  DrillResponse,
  LatencyReport,
  TaskUnit,
  TelemetryReading,
  WarningRecord,
} from './types'

export class ApiError extends Error {
  readonly status: number
  readonly detail: unknown

  constructor(status: number, message: string, detail?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

const http = axios.create({ baseURL: '/', timeout: 20_000 })

/**
 * 时延账本必须自带单位声明——这是指标页能正确渲染的前提，不是可选项。
 *
 * 缺声明就在边界上判成"读失败"：渲染层拿不到单位会抛，整页打空；
 * 猜一个单位则正是那条链上出过的 1000 倍误读（`ingest_end_to_end_seconds` 的 0.019 秒
 * 曾被键名说成毫秒）。两种都比"这页现在读不到数"这句话更坏。
 */
export function requireLatencyUnits(report: LatencyReport): LatencyReport {
  for (const [name, stats] of Object.entries(report?.metrics ?? {})) {
    if (stats.unit !== 'ms' && stats.unit !== 's') {
      throw new ApiError(502, `时延账本没有声明单位：${name}（拿到 ${String(stats.unit)}，只接受 ms / s）`)
    }
  }
  return report
}

async function dispatch<T>(
  instance: AxiosInstance,
  config: AxiosRequestConfig,
): Promise<T> {
  try {
    const response = await instance.request<T>(config)
    return response.data
  } catch (error) {
    if (error instanceof AxiosError) {
      throw new ApiError(error.response?.status ?? 0, error.message, error.response?.data)
    }
    throw error
  }
}

export interface TelemetryQuery {
  station_id?: string
  metric?: string
  region_code?: string
  limit?: number
}

/**
 * ApiClient 工厂：生产用默认实例，测试可注入带桩适配器（避免依赖真实后端）。
 */
export function createApiClient(instance: AxiosInstance) {
  const request = <T>(config: AxiosRequestConfig) => dispatch<T>(instance, config)

  return {
    health: () => request<{ status: string; version: string; contract: string }>({ url: '/healthz' }),
    ready: () =>
      request<{ status: string; bus: string; agents_online: number; store: Record<string, number> }>({
        url: '/readyz',
      }),

    telemetry: (query: TelemetryQuery = {}) =>
      request<{ count: number; items: TelemetryReading[] }>({ url: '/api/v1/telemetry', params: query }),

    warnings: (params: { limit?: number; region_code?: string } = {}) =>
      request<{ count: number; items: WarningRecord[] }>({ url: '/api/v1/warnings', params }),

    warning: (warningId: string) => request<WarningRecord>({ url: `/api/v1/warnings/${warningId}` }),

    task: (taskUnitId: string) => request<TaskUnit>({ url: `/api/v1/tasks/${taskUnitId}` }),

    events: (limit = 20) =>
      request<{ items: DrillResponse['chains'] }>({ url: '/api/v1/events', params: { limit } }),

    agents: () =>
      request<{ online: number; items: AgentInfo[]; gateway_counters: Record<string, number> }>({
        url: '/api/v1/agents',
      }),

    collaboration: () =>
      request<{ success_rate: number | null; transactions: Record<string, unknown>[] }>({
        url: '/api/v1/collaboration',
      }),

    latency: () => request<LatencyReport>({ url: '/api/v1/metrics/latency' }).then(requireLatencyUnits),

    drill: (payload: { scenario: 'surge' | 'normal'; ticks?: number; region_code?: string }) =>
      request<DrillResponse>({ url: '/api/v1/drill/run', method: 'POST', data: payload, timeout: 60_000 }),
  }
}

export type ApiClient = ReturnType<typeof createApiClient>

export const api = createApiClient(http)

export default api
