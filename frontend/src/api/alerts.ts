/**
 * 告警接口：未确认列表与三种处置。
 *
 * 后端口径（`api/routers/alerts.py`）：接手/稍后需要 `duty` 及以上席位，转派需要 `lead`；
 * 重复接手不报错（`already: true`）；404 = 没有这条，409 = 状态不允许（已合并/已关闭）。
 * 席位从页脚选择（`X-Aegis-Operator` 头），与其它写动作同一个来源。
 */

import axios, { AxiosError } from 'axios'

import { installServiceToken, tokenHeaders } from '@/api/serviceToken'
import { operatorHeaders } from '@/api/operatorSeat'

const http = axios.create({ baseURL: '/', timeout: 20_000 })
installServiceToken(http)

export const ALERT_ENDPOINTS = {
  list: '/api/v1/alerts',
  ack: (alertId: string) => `/api/v1/alerts/${encodeURIComponent(alertId)}/ack`,
  transfer: (alertId: string) => `/api/v1/alerts/${encodeURIComponent(alertId)}/transfer`,
  snooze: (alertId: string) => `/api/v1/alerts/${encodeURIComponent(alertId)}/snooze`,
} as const

export interface AlertDto {
  alert_id: string
  warning_id: string
  level: number
  region_code: string
  status: string
  ack_by: string | null
  ack_at: string | null
  assigned_to: string | null
  snooze_until: string | null
  escalated_at: string | null
  drill: boolean
  created_at: string
  /** LEFT JOIN warnings 得到的展示字段；预警行不在时是空串（界面显示"未取到"，不是省略）。 */
  title?: string
  hazard_type?: string
}

export interface AlertListDto {
  count: number
  items: AlertDto[]
  persistent: boolean
  escalation_minutes: number
}

export class AlertApiError extends Error {
  readonly status: number
  readonly detail: unknown

  constructor(status: number, message: string, detail?: unknown) {
    super(message)
    this.name = 'AlertApiError'
    this.status = status
    this.detail = detail
  }

  get code(): string {
    const record = (this.detail ?? {}) as Record<string, unknown>
    const nested = (record.detail ?? record) as Record<string, unknown>
    return typeof nested?.code === 'string' ? nested.code : ''
  }
}

function readableReason(error: unknown): string {
  if (!(error instanceof AxiosError)) return error instanceof Error ? error.message : String(error)
  const detail = (error.response?.data ?? {}) as Record<string, unknown>
  const nested = (detail.detail ?? detail) as Record<string, unknown>
  if (typeof nested?.message === 'string') return nested.message
  return `告警接口调用失败（HTTP ${error.response?.status ?? '?'}）`
}

async function call<T>(run: () => Promise<{ data: T }>): Promise<T> {
  try {
    return (await run()).data
  } catch (error) {
    if (error instanceof AxiosError) {
      throw new AlertApiError(error.response?.status ?? 0, readableReason(error), error.response?.data)
    }
    throw error
  }
}

export function createAlertApiClient(instance = http) {
  return {
    list: (params: { open_only?: boolean; limit?: number } = {}) =>
      call<AlertListDto>(() => instance.get<AlertListDto>(ALERT_ENDPOINTS.list, { params })),

    ack: (alertId: string) =>
      call<{ alert: AlertDto; already: boolean }>(() =>
        instance.post(ALERT_ENDPOINTS.ack(alertId), undefined, { headers: { ...tokenHeaders(), ...operatorHeaders() } }),
      ),

    transfer: (alertId: string, assignee: string) =>
      call<{ alert: AlertDto }>(() =>
        instance.post(ALERT_ENDPOINTS.transfer(alertId), { assignee }, { headers: { ...tokenHeaders(), ...operatorHeaders() } }),
      ),

    snooze: (alertId: string, minutes: number) =>
      call<{ alert: AlertDto }>(() =>
        instance.post(ALERT_ENDPOINTS.snooze(alertId), { minutes }, { headers: { ...tokenHeaders(), ...operatorHeaders() } }),
      ),
  }
}

export type AlertApiClient = ReturnType<typeof createAlertApiClient>

export const alertApi = createAlertApiClient()
