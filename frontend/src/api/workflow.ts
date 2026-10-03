/**
 * 工作流 HTTP 契约（后端 backend/src/aegis/api/workflow_api.py 的前端视图）。
 *
 * 刻意不从 @/api/client 导入任何符号：本模块自带 axios 实例与错误包装，
 * 这样其他 agent 改动通用客户端时不会波及这里（并行开发的隔离边界）。
 *
 * 字段全部保持后端 snake_case：后端 model_config = ConfigDict(extra="forbid")，
 * 多传字段（例如 NodeDef.retry）会被直接 422，因此提交前用 toNodeInput 收敛白名单。
 */

import axios, { AxiosError, type AxiosInstance, type AxiosRequestConfig } from 'axios'

import type { EdgeDef, JsonValue, NodeDef, NodeState, OnFailure, RetryPolicy, WorkflowDef } from '@/utils/graph'

const http = axios.create({ baseURL: '/', timeout: 20_000 })

export class WorkflowApiError extends Error {
  readonly status: number
  readonly detail: unknown

  constructor(status: number, message: string, detail?: unknown) {
    super(message)
    this.name = 'WorkflowApiError'
    this.status = status
    this.detail = detail
  }
}

/** 后端把 WorkflowValidationError 统一映射为 400（workflow_api.py:144-145 等）。 */
export function isWorkflowValidationError(error: unknown): boolean {
  return error instanceof WorkflowApiError && error.status === 400
}

/**
 * 把后端的 detail 还原成人能读的一句话。
 *
 * 两种形状都得处理：`WorkflowValidationError` 的 400 发的是字符串，而 FastAPI 的 422 发的是
 * 数组 `[{loc, msg, type}]`。此前这里对所有东西都 `String(detail)`，于是 422 在界面上显示成
 * 一行 `HTTP 422 [object Object]`——恰好是值班员最需要知道"哪个字段不对"的那一次。
 */
export function describeDetail(detail: unknown): string {
  if (typeof detail === 'string') return detail
  if (typeof detail === 'number' || typeof detail === 'boolean') return String(detail)
  if (Array.isArray(detail)) {
    return detail
      .map((item) => describeDetail(item))
      .filter((text) => text !== '')
      .join('；')
  }
  if (detail !== null && typeof detail === 'object') {
    const record = detail as Record<string, unknown>
    if ('detail' in record) return describeDetail(record.detail)
    const message = typeof record.msg === 'string' ? record.msg : ''
    // `loc` 首段恒为 body/query/path，对人没信息量；下标要留着（第几个节点写错了正是要看的东西）
    const location = Array.isArray(record.loc) ? record.loc.map(String).filter((seg) => seg !== 'body').join('.') : ''
    if (message !== '') return location === '' ? message : `${location}：${message}`
    return Object.entries(record)
      .map(([key, value]) => `${key}=${describeDetail(value)}`)
      .join(' ')
  }
  return ''
}

async function dispatch<T>(instance: AxiosInstance, config: AxiosRequestConfig): Promise<T> {
  try {
    const response = await instance.request<T>(config)
    return response.data
  } catch (error) {
    if (error instanceof AxiosError) {
      throw new WorkflowApiError(error.response?.status ?? 0, error.message, error.response?.data)
    }
    throw error
  }
}

// ---------- 请求体（workflow_api.py:22-86） ----------

export interface NodeInput {
  node_id: string
  type: string
  name: string
  config: Record<string, JsonValue>
  sla_ms: number
  timeout_ms: number
  on_failure: OnFailure
  /** 与后端 `NodeInput` 同形：读接口会带出 retry，写接口也必须收得下，
   *  否则"打开→原样保存"会把节点的重试策略静默抹回默认值。 */
  retry: RetryPolicy
}

export interface DefinitionInput {
  name: string
  description: string
  nodes: NodeInput[]
  edges: EdgeInput[]
}

export type EdgeInput = EdgeDef

export interface ReviseInput {
  nodes?: NodeInput[]
  edges?: EdgeInput[]
  description?: string
}

export interface StartInput {
  workflow_id?: string
  workflow_name?: string
  payload?: Record<string, JsonValue>
  trace_id?: string
}

export interface DecisionInput {
  /** 必须命中节点 options（engine.py:518-522 会先做小写比对）。 */
  choice: string
  by: string
  comment: string
}

export interface InsertNodeInput {
  after: string
  node: NodeInput
}

/** NodeDef → NodeInput：字段与后端一一对应，不再丢弃任何可写字段。 */
export function toNodeInput(node: NodeDef): NodeInput {
  return {
    node_id: node.node_id,
    type: node.type,
    name: node.name,
    config: node.config,
    sla_ms: node.sla_ms,
    timeout_ms: node.timeout_ms,
    on_failure: node.on_failure,
    retry: node.retry,
  }
}

export function toNodeInputs(nodes: readonly NodeDef[]): NodeInput[] {
  return nodes.map(toNodeInput)
}

// ---------- 响应体 ----------

export interface NodeTypeEntry {
  type: string
  description: string
  required_config: string[]
  optional_config: string[]
}

export interface NodeTypesResponse {
  count: number
  items: NodeTypeEntry[]
}

/** 注意：定义列表不返回 nodes/edges（workflow_api.py:113-130），图上结构只能由本地画布持有。 */
export interface DefinitionSummary {
  workflow_id: string
  name: string
  version: number
  status: string
  description: string
  node_count: number
  edge_count: number
}

export interface DefinitionsResponse {
  count: number
  items: DefinitionSummary[]
}

export interface DefinitionWriteResult {
  workflow_id: string
  name: string
  version: number
}

export interface ArchiveResult {
  workflow_id: string
  status: string
}

export interface InstanceNodeRun {
  node_id: string
  type: string
  state: NodeState
  attempts: number
  schedule_latency_ms: number | null
  duration_ms: number | null
  output: Record<string, JsonValue>
  error: string | null
  notes: string[]
}

export interface InstanceDetail {
  instance_id: string
  workflow_id: string
  workflow_version: number
  trace_id: string
  status: string
  error: string | null
  nodes: InstanceNodeRun[]
}

export interface InstancesResponse {
  count: number
  items: InstanceDetail[]
}

/** POST /instances 在实例详情上多带了 trace_id，但 InstanceDetail 本就含同名字段，故同构。 */
export type StartResult = InstanceDetail

export interface NodeConfigResult {
  node_id: string
  config: Record<string, JsonValue>
}

export interface InsertNodeResult {
  instance_id: string
  node_id: string
  after: string
}

const BASE = '/api/v1/workflow'

/**
 * 工作流客户端工厂：生产使用模块级默认实例，测试注入桩适配器实例（见 __tests__/api.spec.ts）。
 */
export function createWorkflowClient(instance: AxiosInstance) {
  const request = <T>(config: AxiosRequestConfig) => dispatch<T>(instance, config)

  return {
    /** 节点面板数据源：16 类节点的描述与配置白名单。 */
    nodeTypes: () => request<NodeTypesResponse>({ url: `${BASE}/node-types` }),

    definitions: () => request<DefinitionsResponse>({ url: `${BASE}/definitions` }),
    /** 一份定义的完整内容（节点、连线、参数）：列表只有计数，改图必须靠这条把它取回来。 */
    definition: (workflowId: string) => request<WorkflowDef>({ url: `${BASE}/definitions/${workflowId}` }),

    createDefinition: (payload: DefinitionInput) =>
      request<DefinitionWriteResult>({ url: `${BASE}/definitions`, method: 'POST', data: payload }),

    /** 修订产生新版本（workflow_api.py:148-163）；在途实例仍绑定旧版本快照。 */
    reviseDefinition: (workflowId: string, payload: ReviseInput) =>
      request<DefinitionWriteResult>({
        url: `${BASE}/definitions/${encodeURIComponent(workflowId)}/revise`,
        method: 'POST',
        data: payload,
      }),

    archiveDefinition: (workflowId: string) =>
      request<ArchiveResult>({
        url: `${BASE}/definitions/${encodeURIComponent(workflowId)}/archive`,
        method: 'POST',
      }),

    startInstance: (payload: StartInput) =>
      request<StartResult>({ url: `${BASE}/instances`, method: 'POST', data: payload }),

    instances: () => request<InstancesResponse>({ url: `${BASE}/instances` }),

    instance: (instanceId: string) =>
      request<InstanceDetail>({ url: `${BASE}/instances/${encodeURIComponent(instanceId)}` }),

    /** 人工核签：choice 必须落在节点登记的 options 内。 */
    submitDecision: (instanceId: string, nodeId: string, payload: DecisionInput) =>
      request<StartResult>({
        url: `${BASE}/instances/${encodeURIComponent(instanceId)}/nodes/${encodeURIComponent(nodeId)}/decision`,
        method: 'POST',
        data: payload,
      }),

    /** 在途改参：仅 pending/ready 节点可改（engine.py:539-540）。 */
    patchNodeConfig: (instanceId: string, nodeId: string, config: Record<string, JsonValue>) =>
      request<NodeConfigResult>({
        url: `${BASE}/instances/${encodeURIComponent(instanceId)}/nodes/${encodeURIComponent(nodeId)}`,
        method: 'PATCH',
        data: { config },
      }),

    insertNode: (instanceId: string, payload: InsertNodeInput) =>
      request<InsertNodeResult>({
        url: `${BASE}/instances/${encodeURIComponent(instanceId)}/nodes`,
        method: 'POST',
        data: payload,
      }),

    /** reason 是查询参数而非请求体（workflow_api.py:241-247）。 */
    bypassNode: (instanceId: string, nodeId: string, reason = '') =>
      request<StartResult>({
        url: `${BASE}/instances/${encodeURIComponent(instanceId)}/nodes/${encodeURIComponent(nodeId)}/bypass`,
        method: 'POST',
        params: { reason },
      }),

    abortInstance: (instanceId: string, reason = '') =>
      request<StartResult>({
        url: `${BASE}/instances/${encodeURIComponent(instanceId)}/abort`,
        method: 'POST',
        params: { reason },
      }),
  }
}

export type WorkflowClient = ReturnType<typeof createWorkflowClient>

export const workflowApi = createWorkflowClient(http)

export default workflowApi
