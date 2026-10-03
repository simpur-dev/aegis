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

const http = axios.create({ baseURL: '/', timeout: 20_000 })

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

async function dispatch<T>(instance: AxiosInstance, config: AxiosRequestConfig): Promise<T> {
  try {
    const response = await instance.request<T>(config)
    return response.data
  } catch (error) {
    if (error instanceof AxiosError) {
      throw new AssistantApiError(error.response?.status ?? 0, error.message, error.response?.data)
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
  const record = (detail ?? {}) as Record<string, unknown>
  const nested = (record.detail ?? record) as Record<string, unknown>
  const message = typeof nested?.message === 'string' ? nested.message : `对话请求失败（HTTP ${response.status}）`
  return new AssistantApiError(response.status, message, detail)
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
