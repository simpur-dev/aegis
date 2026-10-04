/**
 * 人工上报契约层（后端 `POST /api/v1/reports`，`backend/src/aegis/api/app.py:416-439`）。
 *
 * 隔离边界同 `@/api/workflow.ts` / `@/api/map.ts`：自带 axios 实例与错误包装，
 * **不**从 `@/api/client` 导入符号。
 *
 * 后端事实（决定这里能声明什么）：
 * - 请求体 `ReportIn`（app.py:54-62）：`reporter` 2..64、`region_code` `^[0-9A-Z]{6,24}$`、
 *   `note` 4..2000、`lat` ±90、`lon` ±180，越界就是 422，**不是**前端裁一刀再发；
 * - 解析服务没装配时是 503 + `detail.code = "E_PARSER_UNAVAILABLE"`（app.py:429-430）；
 * - 返回的 `parse` 就是三路融合结果 `ParsedDisaster.as_dict()`
 *   （`services/semantic_parser.py:185-204`）：`legs` 是每一路的原始结论，冲突与降级
 *   各成一份清单（semantic_parser.py:11-13 定死的口径：规则 > 检索佐证 > LLM，
 *   三路结论一律完整外显）。前端把 `decided_by` 与 `legs` 原样摊开，
 *   不替后端做"取哪个"的裁决，也不把降级藏成成功；
 * - 上报之后走的是**和监测事件同一条链路**（container.py:544-550），
 *   所以回执里带 `chain`：找得到 trace_id / warning_id / task_units 才算真的接进去。
 * - 坐标只进证据链不改判据（container.py:531-543），回执里的 `location` 是 `[lon, lat]`。
 */

import axios, { AxiosError, type AxiosInstance, type AxiosRequestConfig } from 'axios'

import type { HazardType, RiskLevel, StageResult } from './types'
import { describeValidationDetail } from '@/utils/validationDetail'

const http = axios.create({ baseURL: '/', timeout: 30_000 })

export const REPORT_ENDPOINT = '/api/v1/reports'

export class ReportApiError extends Error {
  readonly status: number
  readonly detail: unknown

  constructor(status: number, message: string, detail?: unknown) {
    super(message)
    this.name = 'ReportApiError'
    this.status = status
    this.detail = detail
  }

  get code(): string {
    const record = (this.detail ?? {}) as Record<string, unknown>
    const nested = (record.detail ?? record) as Record<string, unknown>
    return typeof nested?.code === 'string' ? nested.code : ''
  }
}

/** 解析腿未装配（503）：上报入口要显式说"这条腿没接上"，不许显示成"提交成功但无回执"。 */
export function isParserUnavailable(error: unknown): boolean {
  return error instanceof ReportApiError && (error.status === 503 || error.code === 'E_PARSER_UNAVAILABLE')
}

/**
 * 422 的 detail 是 FastAPI 的校验错误数组（`{loc, msg, type}`）。
 * 现场要读的是"哪一格不合格"，所以把后端原文拼成一行，不翻译成我们自己的措辞——
 * 翻译一次就多一个可能说错的口径。但**字段名要给中文**：这一页的标签本来就叫
 * "上报人 / 区划代码 / 险情描述"，报错里却写 `reporter`、`region_code`，
 * 值班员得先把中英文对上才知道刚才填的是哪一格。规则原文照抄，字段名用页面上的叫法。
 */
export const REPORT_FIELD_LABELS: Record<string, string> = {
  reporter: '上报人',
  region_code: '区划代码',
  hazard_hint: '灾种提示',
  note: '险情描述',
  lat: '纬度',
  lon: '经度',
  scenario: '演练场景',
  ticks: '演练轮数',
}

export function describeValidationError(error: unknown): string {
  if (!(error instanceof ReportApiError) || error.status !== 422) return ''
  const text = describeValidationDetail(error.detail, REPORT_FIELD_LABELS)
  return text === '' ? error.message : text
}

async function dispatch<T>(instance: AxiosInstance, config: AxiosRequestConfig): Promise<T> {
  try {
    const response = await instance.request<T>(config)
    return response.data
  } catch (error) {
    if (error instanceof AxiosError) {
      throw new ReportApiError(error.response?.status ?? 0, error.message, error.response?.data)
    }
    throw error
  }
}

// ---------- 请求体（app.py:54-62） ----------

export interface ReportInputDto {
  reporter: string
  region_code: string
  note: string
  hazard_hint?: string
  lat?: number
  lon?: number
}

/**
 * 表单草稿：ant-design 的空输入是空串或 `null`（`a-input-number` 清空给 null），
 * 而线上格式只允许"有值才带键"。两种形状之间的收敛只发生在 `toReportBody` 一处。
 */
export interface ReportDraftDto {
  reporter: string
  region_code: string
  note: string
  hazard_hint?: string | null
  lat?: number | null
  lon?: number | null
}

/**
 * 白名单收敛：`lat`/`lon` 要么都带要么都不带——这不是前端加的规矩，是后端的口径
 * （app.py:431 `None if payload.lat is None or payload.lon is None`）：只有一个值时
 * 后端整段按"没定位"处理。表单侧另有提示，免得用户以为坐标发出去了。
 *
 * 文本字段一律先裁首尾空白再发：`"    "`（四个空格）满足 `note` 的 min_length=4，
 * 会一路走到解析腿才被 `ValueError("待解析文本为空")` 拦下——那是个裸 ValueError，
 * 出口就成了 HTTP 500。真机在表单里敲四个空格就是这么撞的（后端现已同步裁剪）。
 */
export function toReportBody(input: ReportDraftDto): ReportInputDto {
  const body: ReportInputDto = {
    reporter: input.reporter.trim(),
    region_code: input.region_code.trim(),
    note: input.note.trim(),
  }
  const hint = input.hazard_hint?.trim() ?? ''
  if (hint !== '') body.hazard_hint = hint
  if (typeof input.lat === 'number' && typeof input.lon === 'number') {
    body.lat = input.lat
    body.lon = input.lon
  }
  return body
}

/**
 * 上报表单的取值域，逐条抄自后端 `ReportIn`（app.py:58-63）。
 *
 * 表单标题里本来就写着"2..64 字，后端校验"——写着上限却不拦住，等于让人敲完
 * 七十个字再吃一次 422。数值不写死在测试里：`api/reports.spec.ts` 现读 app.py 比对。
 */
export const REPORT_LIMITS = {
  reporter: { min: 2, max: 64 },
  region_code: { pattern: /^[0-9A-Z]{6,24}$/ },
  hazard_hint: { max: 64 },
  note: { min: 4, max: 2_000 },
  lat: { min: -90, max: 90 },
  lon: { min: -180, max: 180 },
} as const

/**
 * 发出前按后端口径本地挡一次（助手页同一手法）。
 *
 * 这里最先要拦的是**只有空格的正文**：`"    "` 满足 `min_length=4`，
 * 曾经一路走到解析腿才被裸 `ValueError` 拦下，出口就是 HTTP 500。
 * 传进来的应是 `toReportBody()` 裁过的形状（本函数不再裁）。
 */
export function preflightReport(body: ReportInputDto): string {
  const note = body.note
  if (note.length < REPORT_LIMITS.note.min || note.length > REPORT_LIMITS.note.max) {
    const how = note.length === 0 ? '去掉空格后是空的' : `是 ${note.length} 字`
    return `险情描述${how}：后端要求 ${REPORT_LIMITS.note.min}..${REPORT_LIMITS.note.max} 字，这一条没有发出去。`
  }
  const reporter = body.reporter
  if (reporter.length < REPORT_LIMITS.reporter.min || reporter.length > REPORT_LIMITS.reporter.max) {
    return `上报人「${reporter}」不合口径：后端要求 ${REPORT_LIMITS.reporter.min}..${REPORT_LIMITS.reporter.max} 字，这一条没有发出去。`
  }
  const region = body.region_code
  if (!REPORT_LIMITS.region_code.pattern.test(region)) {
    return `区划代码「${region}」不合口径：后端要求 6..24 位大写字母或数字，这一条没有发出去。`
  }
  const hint = body.hazard_hint ?? ''
  if (hint.length > REPORT_LIMITS.hazard_hint.max) {
    return `灾种提示 ${hint.length} 字，超过后端的 ${REPORT_LIMITS.hazard_hint.max} 字上限：这一条没有发出去。`
  }
  if (typeof body.lat === 'number' && (body.lat < REPORT_LIMITS.lat.min || body.lat > REPORT_LIMITS.lat.max)) {
    return `纬度 ${body.lat} 超出 ${REPORT_LIMITS.lat.min}..${REPORT_LIMITS.lat.max}：这一条没有发出去。`
  }
  if (typeof body.lon === 'number' && (body.lon < REPORT_LIMITS.lon.min || body.lon > REPORT_LIMITS.lon.max)) {
    return `经度 ${body.lon} 超出 ${REPORT_LIMITS.lon.min}..${REPORT_LIMITS.lon.max}：这一条没有发出去。`
  }
  return ''
}

// ---------- 响应体 ----------
/** 三路融合的四条结论键（semantic_parser.py:33-38 与 :576 的 `"none"`）：与后端同名，不改写。 */
export type ParseLeg = 'rule' | 'rule_declared' | 'retrieval' | 'llm' | 'none'

export const LEG_SOURCE_LABELS: Record<ParseLeg, string> = {
  rule: '规则词表（阈值命中）',
  rule_declared: '申报等级（上报人写明）',
  retrieval: '检索佐证',
  llm: 'LLM 建议',
  none: '三路均未产出等级',
}

export function legSourceLabel(leg: string): string {
  return LEG_SOURCE_LABELS[leg as ParseLeg] ?? leg
}

export interface LegFindingDto {
  leg: ParseLeg
  hazard_type: HazardType | null
  risk_level: RiskLevel | null
  confidence: number
  rationale: string
  refs: string[]
  used: boolean
}

export interface MetricReadingDto {
  metric: string
  value: number
  unit: string
}

export interface TriggerHitDto {
  rule_id: string
  hazard_type: string
  region_code: string
  evidence_refs: string[]
  score: number
  observed_at: string
}

export interface EvidenceNoteDto {
  source: string
  hazard_type: string | null
  typical_level: number | null
}

export interface ParseResultDto {
  text: string
  hazard_type: HazardType
  region_code: string
  risk_level: RiskLevel | null
  confidence: number
  decided_by: ParseLeg
  needs_review: boolean
  entities: string[]
  metrics: MetricReadingDto[]
  reported_people: number | null
  trigger_hits: TriggerHitDto[]
  legs: LegFindingDto[]
  conflicts: string[]
  degradations: string[]
  evidence: EvidenceNoteDto[]
}

/** `RiskVerdict.as_payload()`（services/risk_engine.py:30-40）。 */
export interface ReportRiskDto {
  hazard_type: HazardType
  region_code: string
  risk_level: RiskLevel
  confidence: number
  rationale: string
  evidence_refs: string[]
  assessed_at: string
  assessed_by: string
}

/** `ChainResult.as_dict()`（pipeline/chain.py:102-116）。 */
export interface ReportChainDto {
  trace_id: string
  event_id: string
  ok: boolean
  acted: boolean
  stages: StageResult[]
  risk: ReportRiskDto | null
  task_units: string[]
  warning_id: string | null
  errors: string[]
  degradations: string[]
  reference_cases: string[]
  context_docs: string[]
}

export interface ReportEchoDto {
  reporter: string
  region_code: string
  /** `[lon, lat]`（经度在前，container.py:431 的 location 口径）。 */
  location: [number, number] | null
}

/**
 * 低置信度上报开出的人工核签工单（`container._open_report_review`）。
 *
 * 后端事实：`human_review_required` 为真时这里才非空——开单不拦发布，预警照发，
 * 工单判的是"这条结论是否按现状生效"。签核走**既有**的工作流决策接口
 * （`decision_endpoint`），平台不另起一套签字口径。
 */
export interface ReportReviewDto {
  workflow_id: string
  instance_id: string | null
  status: string | null
  /** 停在哪个节点（`awaiting_human`）；为空说明这条流程没等人签。 */
  pending_node: string | null
  options: string[]
  decision_endpoint: string
}

export interface ReportOutcomeDto {
  report: ReportEchoDto
  parse: ParseResultDto
  human_review_required: boolean
  /** 核签流程未注册时后端给 `null`（降级是可见事实，不是异常）。 */
  review: ReportReviewDto | null
  intake_seconds: number
  chain: ReportChainDto
}

// ---------- 客户端工厂 ----------

export function createReportsApiClient(instance: AxiosInstance) {
  const request = <T>(config: AxiosRequestConfig) => dispatch<T>(instance, config)

  return {
    /** 提交一条人工上报：文本进三路融合，之后与监测事件同链路。入参是线上格式（用 `toReportBody` 从表单草稿收敛）。 */
    submit: (input: ReportInputDto) =>
      request<ReportOutcomeDto>({ url: REPORT_ENDPOINT, method: 'post', data: input }),
  }
}

export type ReportsApiClient = ReturnType<typeof createReportsApiClient>

export const reportsApi = createReportsApiClient(http)

export default reportsApi
