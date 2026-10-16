/**
 * 推演沙盒契约层（后端 `POST /api/v1/simulation/run`，`backend/src/aegis/api/app.py:962-986`）。
 *
 * 隔离边界同 `@/api/reports.ts` / `@/api/workflow.ts`：自带 axios 实例与错误包装，
 * **不**从 `@/api/client` 导入符号；凭据由 `installServiceToken` 统一携带（§4.8.1：
 * 开了服务令牌的部署里，不带凭据的这一页会整页 401）。
 *
 * 后端事实（决定这里能声明什么）：
 * - 入参 `SimulationRequest`（app.py:68-77）逐格有 `ge/le`，越界是 422，不是前端裁一刀再发；
 * - `playbook` 是 `^(转移|封路|不行动)$` 的字面白名单，同义改写（"组织转移"）会被拒收；
 * - 概率矩阵/时间线/预案对比/角色档都由确定性模型算出，`note` 那句"未实测初值"是后端给的，
 *   前端**原样显示**、不改写也不删——删掉它就把演示口径说成了现场达标；
 * - `roles` 那一格（`services/simulation.py:SimulationReport.as_dict`）说的是"这一份报告
 *   实际用了哪一档角色、应问几次、实问几次、几次回落"。缺它就无法分辨"四角色真选了"
 *   与"角色只是四个字符串"，所以它是渲染必需的键，缺了按读失败处理而不是画空表。
 */

import axios, { AxiosError, type AxiosInstance, type AxiosRequestConfig } from 'axios'

import { describeValidationDetail } from '@/utils/validationDetail'
import { installServiceToken } from '@/api/serviceToken'

const http = axios.create({ baseURL: '/', timeout: 60_000 })
installServiceToken(http)

export const SIMULATION_ENDPOINT = '/api/v1/simulation/run'

export class SimulationApiError extends Error {
  readonly status: number
  readonly detail: unknown

  constructor(status: number, message: string, detail?: unknown) {
    super(message)
    this.name = 'SimulationApiError'
    this.status = status
    this.detail = detail
  }
}

export const SIMULATION_FIELD_LABELS: Record<string, string> = {
  rain_base_mm: '基准降雨量',
  rain_spread_mm: '降雨浮动量',
  scenarios: '情景条数',
  rounds: '推演轮数',
  playbook: '预案档位',
  token_budget: '令牌预算',
  seed: '随机种子',
}

/** 422 拼成一行中文原因（字段名用页面上的叫法），其余状态直接给 message。 */
export function describeSimulationError(error: unknown): string {
  if (!(error instanceof SimulationApiError)) return error instanceof Error ? error.message : String(error)
  if (error.status !== 422) return error.message
  const text = describeValidationDetail(error.detail, SIMULATION_FIELD_LABELS)
  return text === '' ? error.message : text
}

async function dispatch<T>(instance: AxiosInstance, config: AxiosRequestConfig): Promise<T> {
  try {
    const response = await instance.request<T>(config)
    return response.data
  } catch (error) {
    if (error instanceof AxiosError) {
      const status = error.response?.status ?? 0
      const detail = error.response?.data
      const prefix = `推演接口调用失败（HTTP ${status}）`
      if (detail === undefined) throw new SimulationApiError(status, `${prefix}：${error.message}`, undefined)
      const reason = describeValidationDetail(detail, SIMULATION_FIELD_LABELS)
      throw new SimulationApiError(status, reason === '' ? `${prefix}：${error.message}` : `${prefix}：${reason}`, detail)
    }
    throw error
  }
}

// ---------- 请求体（app.py:68-77 逐格抄来） ----------

/** 取值域：逐格抄自 `SimulationRequest` 的 `ge/le`（`api/simulation.spec.ts` 现读 app.py 比对）。 */
export const SIMULATION_LIMITS = {
  rain_base_mm: { min: 0, max: 500 },
  rain_spread_mm: { min: 0, max: 200 },
  scenarios: { min: 1, max: 64 },
  rounds: { min: 1, max: 24 },
  token_budget: { min: 1, max: 10_000_000 },
} as const

/** 后端默认值（app.py:71-77 各格的 `Field default`）：表单初值与缺格回落都取它，前端不自估任何数。 */
export const SIMULATION_DEFAULTS: SimulationBody = {
  rain_base_mm: 40,
  rain_spread_mm: 25,
  scenarios: 8,
  rounds: 5,
  playbook: '转移',
  token_budget: 200_000,
  seed: 20_261_011,
}

export const PLAYBOOK_ACTIONS = ['转移', '封路', '不行动'] as const
export type PlaybookAction = (typeof PLAYBOOK_ACTIONS)[number]

export interface SimulationBody {
  rain_base_mm: number
  rain_spread_mm: number
  scenarios: number
  rounds: number
  playbook: string
  token_budget: number
  seed: number
}

/** 表单草稿：`a-input-number` 清空给 `null`，收敛只发生在 `toSimulationBody` 一处。 */
export type SimulationDraft = Partial<Record<keyof SimulationBody, number | string | null>>

/** 缺格回落到后端默认值（不是前端自估的数）；越界不裁，交给 `preflightSimulation` 拦。 */
export function toSimulationBody(draft: SimulationDraft): SimulationBody {
  const body: SimulationBody = { ...SIMULATION_DEFAULTS }
  for (const key of ['rain_base_mm', 'rain_spread_mm', 'scenarios', 'rounds', 'token_budget', 'seed'] as const) {
    const raw = draft[key]
    if (typeof raw === 'number' && Number.isFinite(raw)) body[key] = raw
  }
  const playbook = typeof draft.playbook === 'string' ? draft.playbook.trim() : ''
  if (playbook !== '') body.playbook = playbook
  return body
}

/**
 * 发出前按后端口径本地挡一次（上报表单同一手法）。
 *
 * 这一页最先要拦的是**预案档位写成同义句**：`转移` 才是白名单，"组织群众转移"会吃 422，
 * 而表单里是个下拉框，出现野值只可能是被人改了控件——拦下来比让后端拒一次更省一次往返。
 * 返回空串表示可以发。
 */
export function preflightSimulation(body: SimulationBody): string {
  for (const key of ['rain_base_mm', 'rain_spread_mm', 'scenarios', 'rounds', 'token_budget'] as const) {
    const range = SIMULATION_LIMITS[key]
    const value = body[key]
    if (!Number.isFinite(value)) return `${SIMULATION_FIELD_LABELS[key]} 不是一个数：这一份推演没有发出去。`
    if (value < range.min || value > range.max) {
      return `${SIMULATION_FIELD_LABELS[key]} ${value} 超出后端的 ${range.min}..${range.max}：这一份推演没有发出去。`
    }
  }
  if (!Number.isInteger(body.seed)) return `${SIMULATION_FIELD_LABELS.seed} 必须是整数：这一份推演没有发出去。`
  if (!(PLAYBOOK_ACTIONS as readonly string[]).includes(body.playbook)) {
    return `预案档位「${body.playbook}」不在白名单里（只接受 ${PLAYBOOK_ACTIONS.join(' / ')}）：这一份推演没有发出去。`
  }
  return ''
}

// ---------- 响应体 ----------

export interface SimulationRole {
  key: string
  title: string
  concern: string
}

export interface SimulationRolesCell {
  policy: string
  roles: SimulationRole[]
  rounds: number
  asked: number
  calls: number
  fallbacks: number
  distinct_actions: string[]
  note?: string
}

export interface SimulationTimelineRow {
  round: number
  rain_mm: number
  mudlevel_cm: number
  actions: Record<string, string>
  applied_playbook: string
  mitigation: number
  trigger_probability: number
}

export interface SimulationReport {
  mode: string
  note: string
  cost: { requested_scenarios: number; effective_scenarios: number; downscaled: boolean }
  probability_matrix: { rain_bucket_mm: string; scenarios: number; mean_secondary_rate: number; max_secondary_rate: number }[]
  timeline: SimulationTimelineRow[]
  playbook_comparison: { playbook: string; mitigation: number; mean_secondary_rate: number; exposure_proxy: number }[]
  roles: SimulationRolesCell
}

/**
 * 报告进渲染层前的边界校验，理由与 `requireLatencyUnits()` 同源：
 * 渲染层拿不到 `roles`/`actions` 会直接抛，整页打空；把缺键写成"显示成空表"更坏——
 * 那正是"多角色推演"退化成四个字符串却看不出来的形状。所以缺就在边界上判读失败。
 *
 * `note` 也在必需键里：那句"概率/缓解系数为未实测初值"是这页能被当成演示而非达标依据的唯一凭据。
 */
const REQUIRED_KEYS: (keyof SimulationReport)[] = ['cost', 'probability_matrix', 'timeline', 'playbook_comparison', 'roles', 'mode', 'note']

export function requireSimulationReport(raw: unknown): SimulationReport {
  const data = (raw ?? {}) as Partial<SimulationReport>
  const missing = REQUIRED_KEYS.filter((key) => data[key] === undefined)
  if (missing.length > 0) {
    throw new SimulationApiError(502, `推演报告缺键：${missing.join(' / ')}（出口形状与页面契约不一致，这页拒绝猜）`, raw)
  }
  const report = data as SimulationReport
  if (report.timeline.length === 0) throw new SimulationApiError(502, '推演报告的时间线是空的：没有任何一轮角色票', raw)
  const withoutVotes = report.timeline.filter((row) => row.actions === undefined || Object.keys(row.actions).length === 0)
  if (withoutVotes.length > 0) {
    throw new SimulationApiError(502, `时间线有 ${withoutVotes.length} 轮没带角色票（applied_playbook 无从解释）`, raw)
  }
  if (report.roles.roles.length === 0) throw new SimulationApiError(502, '角色档没列出在场角色：这一份报告说不清是谁选的', raw)
  return report
}

/** 工厂：生产用默认实例，测试注入桩适配器（全程离线，同 `api/reports.ts` 的手法）。 */
export function createSimulationApiClient(instance: AxiosInstance) {
  return {
    run(body: SimulationBody): Promise<SimulationReport> {
      const config: AxiosRequestConfig = { url: SIMULATION_ENDPOINT, method: 'POST', data: body, timeout: 60_000 }
      return dispatch<unknown>(instance, config).then(requireSimulationReport)
    },
  }
}

export const simulationClient = createSimulationApiClient(http)

export function runSimulation(body: SimulationBody): Promise<SimulationReport> {
  return simulationClient.run(body)
}

/** 角色 key → 页面显示名：优先用后端给的名字（它是角色画像的真源）。 */
export function roleLabel(role: SimulationRole): string {
  return role.title === '' ? role.key : role.title
}
