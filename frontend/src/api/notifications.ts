/**
 * 站内通知读面：SSE 负责"快"，这里负责断线重连后的对账与「标为已读」。
 * 写入不开放给前端：通知只能由助手 notify.lead/group 经 Notifier 产生。
 */

import axios, { AxiosError } from 'axios'

import { operatorHeaders } from '@/api/operatorSeat'
import { installServiceToken, tokenHeaders } from '@/api/serviceToken'

const http = axios.create({ baseURL: '/', timeout: 20_000 })
installServiceToken(http)

export interface NotificationDto {
  id: number
  recipient: string
  message: string
  kind: string
  sender: string
  space_id: string | null
  created_at: string | null
  read_at: string | null
}

export interface NotificationListDto {
  count: number
  items: NotificationDto[]
}

function headers(): Record<string, string> {
  return { ...tokenHeaders(), ...operatorHeaders() }
}

async function call<T>(run: () => Promise<{ data: T }>): Promise<T> {
  try {
    return (await run()).data
  } catch (error) {
    if (error instanceof AxiosError) {
      const detail = (error.response?.data ?? {}) as Record<string, unknown>
      const nested = (detail.detail ?? detail) as Record<string, unknown>
      throw new Error(typeof nested.message === 'string' ? nested.message : `通知接口调用失败（HTTP ${error.response?.status ?? '?'}）`)
    }
    throw error
  }
}

export const notificationApi = {
  list: (params: { unread_only?: boolean; limit?: number } = {}) =>
    call<NotificationListDto>(() => http.get<NotificationListDto>('/api/v1/notifications', { params, headers: headers() })),

  read: (id: number) =>
    call<{ notification: NotificationDto }>(() =>
      http.post(`/api/v1/notifications/${id}/read`, undefined, { headers: headers() }),
    ),
}
