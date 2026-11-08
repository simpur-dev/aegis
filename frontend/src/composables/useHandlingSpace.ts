/**
 * 处置空间状态（模块级单例，与 `useAlerts` 同一取向：页面与抽屉共用一份）。
 *
 * 数据真源是 `/spaces` 台账；SSE `handling.task.changed` 只负责"快"：
 * - 事件带 `version`，只接受比本地新的那版——乱序到达或重连补发的旧帧不会把界面改回去；
 * - 断线重连后拉一次列表 + 详情对账，断流期间发生的状态变化不靠本地脑补；
 * - 写动作把本地看到的 `version` 作为 `expected_version` 交给后端，409 就是"被别人抢先改过"，
 *   界面明说并重新对账（FR-4.3）。
 */

import { computed, ref, watch, type Ref } from 'vue'

import {
  SpaceApiError,
  spaceApi,
  type HandlingTaskDto,
  type SpaceApiClient,
  type SpaceDetailDto,
  type SpaceSummaryDto,
} from '@/api/spaces'
import type { StreamEvent } from '@/composables/useEventStream'

const SYNC_DEBOUNCE_MS = 400
const LIST_LIMIT = 20
const TASK_SUBJECT = 'handling.task.changed'

const spaces = ref<SpaceSummaryDto[]>([])
const currentSpaceId = ref('')
const detail = ref<SpaceDetailDto | null>(null)
const syncing = ref(false)
const error = ref('')
const versionConflict = ref('')
const persistent = ref(false)
const lastSyncAt = ref(0)

const tasks = computed<HandlingTaskDto[]>(() => detail.value?.tasks ?? [])
const participants = computed<string[]>(() => detail.value?.participants ?? [])
const statusCounts = computed<Record<string, number>>(() => detail.value?.status_counts ?? {})

let attached: Ref<StreamEvent[]> | null = null
let stops: Array<() => void> = []
let syncTimer: number | undefined
let client: SpaceApiClient = spaceApi

/** 只接受版本更新的帧；同时重算状态计数，避免界面上"总数"与列表对不上。 */
function applyTask(incoming: HandlingTaskDto): void {
  if (detail.value === null) return
  const rows = [...detail.value.tasks]
  const index = rows.findIndex((row) => row.task_id === incoming.task_id)
  if (index >= 0 && rows[index]!.version >= incoming.version) return
  if (index >= 0) rows.splice(index, 1, incoming)
  else rows.unshift(incoming)
  const counts: Record<string, number> = {}
  for (const row of rows) counts[row.status] = (counts[row.status] ?? 0) + 1
  detail.value = {
    ...detail.value,
    tasks: rows,
    tasks_total: rows.length,
    status_counts: counts,
    participants: [...new Set(rows.map((row) => row.assignee).filter((id): id is string => id !== null))].sort(),
  }
}

function readableFailure(err: unknown): string {
  if (!(err instanceof SpaceApiError)) return err instanceof Error ? err.message : '处置空间对账失败'
  if (err.status === 401) return '先在告警卡选择席位，再执行处置'
  if (err.status === 403) return err.message || '当前席位没有这个处置权限'
  if (err.status === 404) return '空间或任务不存在（可能已被关闭）'
  return err.message
}

async function syncDetail(): Promise<void> {
  const spaceId = currentSpaceId.value
  if (spaceId === '') {
    detail.value = null
    return
  }
  const body = await client.detail(spaceId)
  // 对账期间用户可能切了空间：迟到的旧响应不能改写到新空间上。
  if (currentSpaceId.value !== spaceId) return
  detail.value = body
  persistent.value = body.persistent
}

async function syncAll(): Promise<void> {
  if (syncing.value) return
  syncing.value = true
  error.value = ''
  try {
    const body = await client.list({ open_only: true, limit: LIST_LIMIT })
    spaces.value = body.items
    persistent.value = body.persistent
    if (currentSpaceId.value === '' || !body.items.some((row) => row.space_id === currentSpaceId.value)) {
      currentSpaceId.value = body.items[0]?.space_id ?? ''
    }
    await syncDetail()
    lastSyncAt.value = Date.now()
  } catch (err) {
    error.value = readableFailure(err)
  } finally {
    syncing.value = false
  }
}

function scheduleSync(): void {
  if (syncTimer !== undefined) window.clearTimeout(syncTimer)
  syncTimer = window.setTimeout(() => void syncAll(), SYNC_DEBOUNCE_MS)
}

function onEvents(events: StreamEvent[]): void {
  let hitOtherSpace = false
  for (const event of events) {
    if (event.subject !== TASK_SUBJECT) continue
    const payload = event.payload ?? {}
    const spaceId = typeof payload.space_id === 'string' ? payload.space_id : ''
    const version = Number(payload.version ?? 0)
    if (spaceId === '' || !Number.isFinite(version) || version < 1) continue
    if (spaceId === currentSpaceId.value) {
      applyTask({
        task_id: String(payload.task_id ?? ''),
        space_id: spaceId,
        stu_id: String(payload.stu_id ?? ''),
        status: String(payload.status ?? ''),
        assignee: typeof payload.assignee === 'string' ? payload.assignee : null,
        version,
        updated_at: typeof payload.updated_at === 'string' ? payload.updated_at : event.ts,
      })
    } else {
      hitOtherSpace = true
    }
  }
  if (hitOtherSpace) scheduleSync()
}

async function runAction(run: () => Promise<{ task: HandlingTaskDto }>): Promise<string> {
  try {
    const body = await run()
    applyTask(body.task)
    versionConflict.value = ''
    return ''
  } catch (err) {
    if (err instanceof SpaceApiError && err.status === 409) {
      versionConflict.value = '这条任务已被他人处置，已按台账重新对账'
      void syncAll()
      return versionConflict.value
    }
    return readableFailure(err)
  }
}

/** 测试之间要能拿到干净状态：清掉 watch 句柄与去抖计时器，避免跨用例串状态。 */
export function resetHandlingSpace(nextClient: SpaceApiClient = spaceApi): void {
  for (const stop of stops) stop()
  stops = []
  attached = null
  if (syncTimer !== undefined) window.clearTimeout(syncTimer)
  syncTimer = undefined
  client = nextClient
  spaces.value = []
  currentSpaceId.value = ''
  detail.value = null
  syncing.value = false
  error.value = ''
  versionConflict.value = ''
  persistent.value = false
  lastSyncAt.value = 0
}

export function useHandlingSpace() {
  function attachStream(handle: { events: Ref<StreamEvent[]>; connected: Ref<boolean> }): void {
    if (attached === handle.events) return
    for (const stop of stops) stop()
    attached = handle.events
    // immediate：挂上时流里已有的历史事件也要能补上（版本闸保证旧帧不覆盖新态）
    stops = [
      watch(handle.events, onEvents, { immediate: true }),
      watch(handle.connected, (now, prev) => {
        if (now && !prev) void syncAll()
      }),
    ]
  }

  function select(spaceId: string): void {
    currentSpaceId.value = spaceId
    void syncDetail().catch((err: unknown) => {
      error.value = readableFailure(err)
    })
  }

  function claim(task: HandlingTaskDto): Promise<string> {
    return runAction(() => client.claim(currentSpaceId.value, task.task_id, task.version))
  }

  function ack(task: HandlingTaskDto): Promise<string> {
    return runAction(() => client.ack(currentSpaceId.value, task.task_id, task.version))
  }

  function transfer(task: HandlingTaskDto, assignee: string): Promise<string> {
    return runAction(() => client.transfer(currentSpaceId.value, task.task_id, assignee, task.version))
  }

  return {
    spaces,
    currentSpaceId,
    detail,
    tasks,
    participants,
    statusCounts,
    syncing,
    error,
    versionConflict,
    persistent,
    lastSyncAt,
    attachStream,
    refresh: () => void syncAll(),
    select,
    claim,
    ack,
    transfer,
  }
}
