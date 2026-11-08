/**
 * 处置空间接口（设计文档 §5.1 / FR-4.2–4.4）。
 *
 * 后端口径（`api/routers/spaces.py`）：认领/回执需要 `duty` 及以上席位，移交需要 `lead`；
 * 写动作带 `expected_version`（客户端看到的那版），版本被别人抢先改过就是 409
 * `E_TASK_STATE_CONFLICT`，界面据此提示并重新对账，绝不拿旧视图覆盖新状态。
 * 席位与告警卡、助手写动作同源（`operatorSeat`）。
 */

import axios, { AxiosError } from 'axios'

import { installServiceToken, tokenHeaders } from '@/api/serviceToken'
import { operatorHeaders } from '@/api/operatorSeat'

const http = axios.create({ baseURL: '/', timeout: 20_000 })
installServiceToken(http)

export const SPACE_ENDPOINTS = {
  list: '/api/v1/spaces',
  detail: (spaceId: string) => `/api/v1/spaces/${encodeURIComponent(spaceId)}`,
  claim: (spaceId: string, taskId: string) =>
    `/api/v1/spaces/${encodeURIComponent(spaceId)}/tasks/${encodeURIComponent(taskId)}/claim`,
  ack: (spaceId: string, taskId: string) =>
    `/api/v1/spaces/${encodeURIComponent(spaceId)}/tasks/${encodeURIComponent(taskId)}/ack`,
  transfer: (spaceId: string, taskId: string) =>
    `/api/v1/spaces/${encodeURIComponent(spaceId)}/tasks/${encodeURIComponent(taskId)}/transfer`,
} as const

export interface HandlingSpaceDto {
  space_id: string
  warning_id: string
  status: string
  created_at: string
  closed_at: string | null
}

export interface SpaceSummaryDto extends HandlingSpaceDto {
  tasks_total: number
  tasks_open: number
  tasks_closed: number
}

export interface HandlingTaskDto {
  task_id: string
  space_id: string
  stu_id: string
  assignee: string | null
  status: string
  version: number
  updated_at: string | null
}

export interface SpaceListDto {
  count: number
  items: SpaceSummaryDto[]
  persistent: boolean
}

export interface SpaceDetailDto {
  space: HandlingSpaceDto
  tasks: HandlingTaskDto[]
  participants: string[]
  status_counts: Record<string, number>
  tasks_total: number
  persistent: boolean
}

export class SpaceApiError extends Error {
  readonly status: number
  readonly detail: unknown

  constructor(status: number, message: string, detail?: unknown) {
    super(message)
    this.name = 'SpaceApiError'
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
  return `处置空间接口调用失败（HTTP ${error.response?.status ?? '?'}）`
}

async function call<T>(run: () => Promise<{ data: T }>): Promise<T> {
  try {
    return (await run()).data
  } catch (error) {
    if (error instanceof AxiosError) {
      throw new SpaceApiError(error.response?.status ?? 0, readableReason(error), error.response?.data)
    }
    throw error
  }
}

function writeConfig(): { headers: Record<string, string> } {
  return { headers: { ...tokenHeaders(), ...operatorHeaders() } }
}

export function createSpaceApiClient(instance = http) {
  return {
    list: (params: { open_only?: boolean; limit?: number } = {}) =>
      call<SpaceListDto>(() => instance.get<SpaceListDto>(SPACE_ENDPOINTS.list, { params, headers: tokenHeaders() })),

    detail: (spaceId: string) =>
      call<SpaceDetailDto>(() => instance.get<SpaceDetailDto>(SPACE_ENDPOINTS.detail(spaceId), { headers: tokenHeaders() })),

    claim: (spaceId: string, taskId: string, expectedVersion: number) =>
      call<{ task: HandlingTaskDto; space_id: string }>(() =>
        instance.post(SPACE_ENDPOINTS.claim(spaceId, taskId), { expected_version: expectedVersion }, writeConfig()),
      ),

    ack: (spaceId: string, taskId: string, expectedVersion: number) =>
      call<{ task: HandlingTaskDto; space_id: string }>(() =>
        instance.post(SPACE_ENDPOINTS.ack(spaceId, taskId), { expected_version: expectedVersion }, writeConfig()),
      ),

    transfer: (spaceId: string, taskId: string, assignee: string, expectedVersion: number) =>
      call<{ task: HandlingTaskDto; space_id: string }>(() =>
        instance.post(
          SPACE_ENDPOINTS.transfer(spaceId, taskId),
          { assignee, expected_version: expectedVersion },
          writeConfig(),
        ),
      ),
  }
}

export type SpaceApiClient = ReturnType<typeof createSpaceApiClient>

export const spaceApi = createSpaceApiClient()
