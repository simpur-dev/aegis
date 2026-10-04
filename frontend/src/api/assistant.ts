/**
 * 语义交互契约层（后端 `backend/src/aegis/api/assistant_api.py` 的前端视图）。
 *
 * 与 `@/api/workflow.ts`、`@/api/map.ts` 同一隔离边界：自带 axios 实例与错误包装，
 * **不**从 `@/api/client` 导入符号，其他 agent 改动通用客户端时不波及这里。
 *
 * 后端事实（决定本模块能声明什么，不臆造）：
 * - 路由四条形：`GET /capabilities`（assistant_api.py:70）、`POST /chat`（:75）、
 *   `POST /confirm`（:104）、`GET /sessions/{session_id}`（:110）；
 * - 出口开关是配置面的：`AEGIS_ASSISTANT_ENABLED=false` 时整段路由**不挂载**
 *   （app.py:538-541），此时四条都读成 404；语义腿没装配时是 503 +
 *   `detail.code = "E_ASSISTANT_UNAVAILABLE"`（assistant_api.py:57-63）。
 *   两者都得原样外显成"这个出口不存在/这条腿没接上"，不许渲染成"没有回复"；
 * - 请求体 `extra="forbid"`（assistant_api.py:33）：多一个字段就是 422，
 *   所以提交前用 `toChatBody` 收敛白名单，snake_case 一律不转；
 * - `session_id` 口径 `^[A-Za-z0-9_-]{4,64}$`（assistant_api.py:29）：由 `meta`/`proposal`
 *   帧回给前端，前端不自己造（造出来的大概率不合模式，白吃一次 422）；
 * - `/chat` 是 **POST + SSE**，帧形如 `data: {json}\n\n`，另有注释帧 `: keep-alive\n\n`
 *   （assistant_api.py:145）。EventSource 只能发 GET，所以这里必须用
 *   `fetch` + `response.body.getReader()` 自己按空行切帧；
 * - 一帧的字典是 `{type, ...data}` 平铺的（services/assistant.py:157 `as_dict`），
 *   帧类型集合见 services/assistant.py:283-330 与 :480-512。
 */

import axios, { AxiosError, type AxiosInstance, type AxiosRequestConfig } from 'axios'

import { describeValidationDetail } from '@/utils/validationDetail'

const http = axios.create({ baseURL: '/', timeout: 20_000 })

/**
 * 对话/确认这两个请求体的字段在界面上的叫法，供 422 的字段名配中文。
 *
 * 后端全是 `extra="forbid"` + 正则/长度约束（assistant_api.py:32-47），撞一次 422 的概率不低
 * （超长文本、会话号写错、区划代码不是 6 位以上大写字母数字），而机器名对值班员没有信息量。
 */
export const ASSISTANT_FIELD_LABELS: Record<string, string> = {
  message: '对话内容',
  session_id: '会话号',
  reporter: '上报人名义',
  region_code: '区划代码',
  hazard_hint: '灾种提示',
  action_id: '动作号',
  actor: '确认人名义',
}

export const ASSISTANT_ENDPOINTS = {
  capabilities: '/api/v1/assistant/capabilities',
  chat: '/api/v1/assistant/chat',
  confirm: '/api/v1/assistant/confirm',
  session: (sessionId: string) => `/api/v1/assistant/sessions/${sessionId}`,
} as const

export class AssistantApiError extends Error {
  readonly status: number
  readonly detail: unknown

  constructor(status: number, message: string, detail?: unknown) {
    super(message)
    this.name = 'AssistantApiError'
    this.status = status
    this.detail = detail
  }

  /** 后端把机器可读码放在 `detail.code`（HTTPException 的 detail 是字典时才成立）。 */
  get code(): string {
    const record = (this.detail ?? {}) as Record<string, unknown>
    const nested = (record.detail ?? record) as Record<string, unknown>
    return typeof nested?.code === 'string' ? nested.code : ''
  }
}

/** 语义腿没装配（503 + E_ASSISTANT_UNAVAILABLE）：上层按"这条腿没接上"渲染。 */
export function isAssistantUnavailable(error: unknown): boolean {
  return error instanceof AssistantApiError && (error.status === 503 || error.code === 'E_ASSISTANT_UNAVAILABLE')
}

/**
 * 出口整体不存在（配置关掉了助手，路由没挂）：和"未装配"是两件事，不许合并成一个文案。
 * 只用于能力面与对话这两条路由——`sessions/{id}` 的 404 是"会话不存在或已过期"
 * （assistant_api.py:115），那是业务事实而不是装配事实，上层按 `status === 404` 自己判。
 */
export function isAssistantDisabled(error: unknown): boolean {
  return error instanceof AssistantApiError && error.status === 404 && error.code === ''
}

/**
 * detail → 界面上能读的一句原因。认不出形状时返回空串，由调用方回落（这里不编造原因）。
 *
 * 两条出口都要走它：`/chat` 是 fetch（自己读 body），能力面/确认/会话是 axios。
 * 以前只有 fetch 那条翻了原因，axios 那条直接把 `error.message`（"Request failed with status code 422"）
 * 交给界面——同一个错误在同一个页面上分成两种写法，英文那句对值班员等于没说。
 */
function readableReason(detail: unknown): string {
  const record = (detail ?? {}) as Record<string, unknown>
  const nested = (record.detail ?? record) as Record<string, unknown>
  if (typeof nested?.message === 'string') return nested.message
  return describeValidationDetail(detail, ASSISTANT_FIELD_LABELS)
}

function withReason(prefix: string, reason: string): string {
  return reason === '' ? prefix : `${prefix}：${reason}`
}

async function dispatch<T>(instance: AxiosInstance, config: AxiosRequestConfig): Promise<T> {
  try {
    const response = await instance.request<T>(config)
    return response.data
  } catch (error) {
    if (error instanceof AxiosError) {
      const status = error.response?.status ?? 0
      const detail = error.response?.data
      // 没有响应体（超时、连接被拒）时保留 axios 自己的话——那句里带的是"请求没成功"这一事实
      if (detail === undefined) throw new AssistantApiError(status, `助手接口调用失败（HTTP ${status}）：${error.message}`, undefined)
      throw new AssistantApiError(status, withReason(`助手接口调用失败（HTTP ${status}）`, readableReason(detail)), detail)
    }
    throw error
  }
}

// ---------- 能力面（assistant_api.py:70-73 → services/assistant.py:247-264） ----------

export interface ActionSpecDto {
  action: string
  title: string
  requires_confirmation: boolean
  needs: string[]
  available: boolean
  missing: string[]
  /** 词表自己的示例句：页面上的建议条就预填它。缺字段时是空串，建议条据此不渲染成可点项。 */
  example: string
}

export interface CapabilitiesDto {
  llm_configured: boolean
  semantic_parser: boolean
  wired_actions: Record<string, boolean>
  actions: ActionSpecDto[]
  session_ttl_seconds: number
  max_pending_actions: number
}

/**
 * 能力面缺字段时的兜底：后端少给一个键，页面读的是"读到了但这项没有"，
 * 而不是渲染层崩成白屏（与 `map.ts` 的 normalizeAnchors 同一取向：形状在这里判，语义在上层判）。
 */
export function capabilitiesShape(raw: unknown): CapabilitiesDto {
  const record = (raw ?? {}) as Record<string, unknown>
  const actions = Array.isArray(record.actions) ? record.actions : []
  const wired = (record.wired_actions ?? {}) as Record<string, unknown>
  return {
    llm_configured: record.llm_configured === true,
    semantic_parser: record.semantic_parser === true,
    wired_actions: Object.fromEntries(Object.entries(wired).map(([key, value]) => [key, value === true])),
    actions: actions.map((item) => actionSpec(item)),
    session_ttl_seconds: Number(record.session_ttl_seconds ?? 0),
    max_pending_actions: Number(record.max_pending_actions ?? 0),
  }
}

function actionSpec(raw: unknown): ActionSpecDto {
  const record = (raw ?? {}) as Record<string, unknown>
  return {
    action: String(record.action ?? ''),
    title: String(record.title ?? record.action ?? ''),
    requires_confirmation: record.requires_confirmation === true,
    needs: Array.isArray(record.needs) ? record.needs.map(String) : [],
    available: record.available === true,
    missing: Array.isArray(record.missing) ? record.missing.map(String) : [],
    example: typeof record.example === 'string' ? record.example : '',
  }
}

// ---------- 会话与确认 ----------

export interface SessionStateDto {
  session_id: string
  turns: number
  pending_actions: PendingActionDto[]
  history: Array<{ role: string; text: string }>
}

export interface PendingActionDto {
  action_id: string
  action: string
  summary: string
  args: Record<string, unknown>
  status: string
  expires_in_seconds: number
}

export interface ConfirmRequestDto {
  session_id: string
  action_id: string
  actor?: string
}

/** `status` 的四个取值就是后端 confirm() 的四条返回分支（services/assistant.py:332-362）。 */
export type ConfirmStatus = 'executed' | 'rejected' | 'expired' | 'failed'

export interface ConfirmResultDto {
  status: ConfirmStatus
  action_id: string
  action?: string
  actor?: string | null
  result?: Record<string, unknown>
  reason?: string
  error?: string
}

// ---------- 对话请求体（assistant_api.py:32-39，extra="forbid"） ----------

export interface ChatRequestDto {
  message: string
  session_id?: string
  reporter?: string
  region_code?: string
  hazard_hint?: string
}

/**
 * 只发后端声明过的 5 个键：多一个键就是 422，空字符串也会被 pydantic 的
 * `min_length` 判死，所以可选键要么有值要么不带。
 */
export function toChatBody(request: ChatRequestDto): ChatRequestDto {
  const body: ChatRequestDto = { message: request.message }
  if (request.session_id) body.session_id = request.session_id
  if (request.reporter) body.reporter = request.reporter
  if (request.region_code) body.region_code = request.region_code
  if (request.hazard_hint) body.hazard_hint = request.hazard_hint
  return body
}

/**
 * 对话请求体的口径，逐条抄自后端 `ChatRequest`（assistant_api.py:35-39）。
 *
 * 抄来的东西会被校验：`api/assistant.spec.ts` 里的跨端门禁直接读
 * `assistant_api.py` 源码比对这里的数字与正则——后端改了上限而这里没跟上就是红灯，
 * 免得两份口径各自演化（那是"前端放过去、后端 422"这类来回的源头）。
 */
export const CHAT_LIMITS = {
  message: { min: 1, max: 2_000 },
  reporter: { min: 2, max: 64 },
  hazard_hint: { max: 64 },
  region_code: { pattern: /^[0-9A-Z]{6,24}$/ },
} as const

/**
 * 发出前按后端口径本地挡一次：撞 422 要等一个来回，而这一页对的是值班员的手速；
 * 更糟的是"发出去了、时间线上什么都没有"那种观感。
 *
 * 只挡**人填的**三格。`session_id` 刻意不在这里挡：它是后端 `meta` 帧给的，
 * 万一哪天与后端自己的模式不一致，本地拒绝会把整场对话锁死，
 * 而后端的 422 至少把"哪一格、为什么"原样说出来——那种不一致不该由前端掩盖。
 *
 * 返回空串表示"按这份口径这一条能过"，不保证后端一定收（还有一堆语义判据）。
 */
export function preflightChat(request: ChatRequestDto): string {
  const text = request.message
  if (text.length < CHAT_LIMITS.message.min) {
    return `对话内容为空：后端要求 ${CHAT_LIMITS.message.min}..${CHAT_LIMITS.message.max} 字，这一条没有发出去。`
  }
  if (text.length > CHAT_LIMITS.message.max) {
    return `对话内容 ${text.length} 字，超过后端的 ${CHAT_LIMITS.message.max} 字上限：请缩短后重发。`
  }
  const reporter = request.reporter
  if (reporter !== undefined && reporter !== '') {
    if (reporter.length < CHAT_LIMITS.reporter.min || reporter.length > CHAT_LIMITS.reporter.max) {
      return `上报人名义「${reporter}」不合口径：后端要求 ${CHAT_LIMITS.reporter.min}..${CHAT_LIMITS.reporter.max} 字，这一条没有发出去。`
    }
  }
  const region = request.region_code
  if (region !== undefined && region !== '' && !CHAT_LIMITS.region_code.pattern.test(region)) {
    return `区划代码「${region}」不合口径：后端要求 6..24 位大写字母或数字，这一条没有发出去。`
  }
  const hint = request.hazard_hint
  if (hint !== undefined && hint.length > CHAT_LIMITS.hazard_hint.max) {
    return `灾种提示 ${hint.length} 字，超过后端的 ${CHAT_LIMITS.hazard_hint.max} 字上限。`
  }
  return ''
}

// ---------- SSE 帧 ----------

export interface MetaFrame {
  type: 'meta'
  session_id: string
  llm_configured: boolean
  actions: string[]
}

export interface IntentFrame {
  type: 'intent'
  action: string
  args: Record<string, unknown>
  confidence: number
  decided_by: string
  note: string
  requires_confirmation: boolean
}

/** 越权指令帧：后端明确"被拒 + 留痕"，前端必须原样摊开，不许降级成一次查询结果。 */
export interface RejectedFrame {
  type: 'rejected'
  instruction: string
  reason: string
  session_id: string
}

export interface StatusFrame {
  type: 'status'
  text: string
}

export interface ProposalFrame {
  type: 'proposal'
  action_id: string
  action: string
  summary: string
  args: Record<string, unknown>
  status: string
  expires_in_seconds: number
  session_id: string
}

/** `result` 帧是 `{action, ...payload}` 平铺的（services/assistant.py:511），payload 形状随动作而变。 */
export interface ResultFrame {
  type: 'result'
  action: string
  [key: string]: unknown
}

export interface AnswerFrame {
  type: 'answer'
  text: string
}

export interface ErrorFrame {
  type: 'error'
  message: string
}

export interface DoneFrame {
  type: 'done'
  session_id: string
  latency_ms: number
  rejected_count: number
}

export type AssistantFrame =
  | MetaFrame
  | IntentFrame
  | RejectedFrame
  | StatusFrame
  | ProposalFrame
  | ResultFrame
  | AnswerFrame
  | ErrorFrame
  | DoneFrame

const FRAME_TYPES = new Set([
  'meta',
  'intent',
  'rejected',
  'status',
  'proposal',
  'result',
  'answer',
  'error',
  'done',
])

function malformed(message: string): ErrorFrame {
  return { type: 'error', message }
}

/**
 * 一帧 → 可渲染帧。解析失败与"后端加了前端不认识的帧型"都返回 error 帧而不是抛：
 * 对话半途抛错，用户读到的是"一直转圈"，而现场真正要的是那句"第 N 帧读不懂"。
 */
export function decodeFrame(payload: string): AssistantFrame {
  let parsed: unknown
  try {
    parsed = JSON.parse(payload)
  } catch {
    return malformed(`SSE 帧不是合法 JSON：${payload.slice(0, 120)}`)
  }
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return malformed('SSE 帧不是对象')
  }
  const record = parsed as Record<string, unknown>
  const type = typeof record.type === 'string' ? record.type : ''
  if (!type) return malformed('SSE 帧缺少 type 字段')
  if (!FRAME_TYPES.has(type)) return malformed(`未识别的帧类型：${type}`)
  return parsed as AssistantFrame
}

/**
 * 按空行切帧：`rest` 是尚未收完的尾巴，留给下一次 reader 读取拼接。
 * 一帧被 TCP 分片切成两半是常态而不是异常，这里切错的后果是整段对话少一帧。
 * 注释帧（`: keep-alive`）与没有 `data:` 行的块直接跳过——它们不是业务事件。
 */
export function splitFrames(buffer: string): { frames: AssistantFrame[]; rest: string } {
  const parts = buffer.split(/\r?\n\r?\n/)
  const rest = parts.pop() ?? ''
  const frames: AssistantFrame[] = []
  for (const block of parts) {
    const lines = block.split(/\r?\n/)
    const data = lines.filter((line) => line.startsWith('data:')).map((line) => line.slice(5).replace(/^ /, ''))
    if (data.length === 0) continue
    frames.push(decodeFrame(data.join('\n')))
  }
  return { frames, rest }
}

/** 流末收摊：后端正常会以空行结尾，但连接被掐时尾巴里可能还压着完整的一帧。 */
function drainTrailing(buffer: string): AssistantFrame[] {
  const trimmed = buffer.trim()
  if (!trimmed) return []
  return splitFrames(`${trimmed}\n\n`).frames
}

// ---------- 流式对话（POST + SSE，不能用 EventSource） ----------

export interface StreamBodyLike {
  getReader(): {
    read(): Promise<{ done: boolean; value?: Uint8Array }>
    cancel?: (reason?: unknown) => Promise<void>
  }
}

export interface ChatResponseLike {
  ok: boolean
  status: number
  body: StreamBodyLike | null
  json(): Promise<unknown>
  text(): Promise<string>
}

export type ChatFetch = (input: string, init?: RequestInit) => Promise<ChatResponseLike>

export interface StreamChatOptions {
  /** 测试注入口：桩 fetch 不必是真的 Response。 */
  fetchImpl?: ChatFetch
  signal?: AbortSignal
}

/** 非 2xx 的响应体翻成带码错误：503 的 body 是 `{detail:{code,message}}`，422 的是数组，两种都要能读。 */
async function toApiError(response: ChatResponseLike): Promise<AssistantApiError> {
  let detail: unknown
  try {
    detail = await response.json()
  } catch {
    detail = await response.text().catch(() => '')
  }
  // 422 的 detail 是 FastAPI 的校验数组：以前这里只落到"HTTP 422"一句话，
  // 把"哪一格、为什么"整段丢了——贴一段长文本进来被拒，用户完全不知道在说什么。
  const reason = readableReason(detail)
  return new AssistantApiError(response.status, withReason(`对话请求失败（HTTP ${response.status}）`, reason), detail)
}

/**
 * 发一轮对话并按帧产出结果。帧序列是过程事实（`meta → intent → status → result → answer → done`），
 * 后端刻意不做 token 级伪流式（assistant_api.py:1-8），所以这里也不要把 answer 帧拆成"打字机"。
 */
export async function* streamChat(
  request: ChatRequestDto,
  options: StreamChatOptions = {},
): AsyncGenerator<AssistantFrame, void, undefined> {
  const doFetch: ChatFetch = options.fetchImpl ?? (globalThis.fetch as unknown as ChatFetch)
  if (typeof doFetch !== 'function') {
    throw new AssistantApiError(0, '当前环境不支持 fetch，无法读取流式对话')
  }
  const response = await doFetch(ASSISTANT_ENDPOINTS.chat, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify(toChatBody(request)),
    signal: options.signal,
  }).catch((error: unknown) => {
    throw new AssistantApiError(0, `对话请求未发出：${error instanceof Error ? error.message : String(error)}`)
  })

  if (!response.ok) throw await toApiError(response)
  if (!response.body) throw new AssistantApiError(response.status, '响应没有可读流（浏览器未提供 response.body）')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const split = splitFrames(buffer)
      buffer = split.rest
      for (const frame of split.frames) yield frame
    }
    buffer += decoder.decode()
    for (const frame of drainTrailing(buffer)) yield frame
  } finally {
    await reader.cancel?.().catch(() => undefined)
  }
}

// ---------- 客户端工厂（生产用默认实例，测试注入桩适配器） ----------

export function createAssistantApiClient(instance: AxiosInstance) {
  const request = <T>(config: AxiosRequestConfig) => dispatch<T>(instance, config)

  return {
    /** 装配事实：LLM 是否配了、哪些动作缺依赖。页面据此显示"未配置"，而不是装可用。 */
    capabilities: async () =>
      capabilitiesShape(await request<unknown>({ url: ASSISTANT_ENDPOINTS.capabilities, method: 'get' })),

    /** 人工确认执行：一次性、带期、限本会话（services/assistant.py:332）。 */
    confirm: (body: ConfirmRequestDto) =>
      request<ConfirmResultDto>({ url: ASSISTANT_ENDPOINTS.confirm, method: 'post', data: body }),

    /** 会话回读：404 就是"会话不存在或已过期"（assistant_api.py:115）。 */
    session: (sessionId: string) => request<SessionStateDto>({ url: ASSISTANT_ENDPOINTS.session(sessionId), method: 'get' }),

    chat: (body: ChatRequestDto, options: StreamChatOptions = {}) => streamChat(body, options),
  }
}

export type AssistantApiClient = ReturnType<typeof createAssistantApiClient>

export const assistantApi = createAssistantApiClient(http)

export default assistantApi
