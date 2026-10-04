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
import { describeValidationDetail } from '@/utils/validationDetail'

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
 * 画布上这些格子的叫法，用来给 422 里的字段名配中文。
 *
 * 只给**单层**字段配：`nodes.0.config.limit` 这种路径保持原形——下标（第几个节点）
 * 正是要看的信息，翻译反而会把定位线索绕晕。
 */
export const WORKFLOW_FIELD_LABELS: Record<string, string> = {
  name: '流程名称',
  description: '流程说明',
  nodes: '节点清单',
  edges: '连线清单',
  workflow_id: '流程号',
  choice: '决策选项',
  by: '决策人',
  comment: '决策备注',
  after: '插入位置节点号',
}

/**
 * 把后端的 detail 还原成人能读的一句话。
 *
 * 两种形状都得处理：`WorkflowValidationError` 的 400 发的是字符串，而 FastAPI 的 422 发的是
 * 数组 `[{loc, msg, type}]`。此前这里对所有东西都 `String(detail)`，于是 422 在界面上显示成
 * 一行 `HTTP 422 [object Object]`——恰好是值班员最需要知道"哪个字段不对"的那一次。
 *
 * 拼装逻辑已收到 `@/utils/validationDetail`（上报页、助手页同一份）：
 * 三个出口各写一遍时，同一个错误在三个页面上长成三种样子，改一处漏两处。
 */
export function describeDetail(detail: unknown): string {
  return describeValidationDetail(detail, WORKFLOW_FIELD_LABELS)
}

async function dispatch<T>(instance: AxiosInstance, config: AxiosRequestConfig): Promise<T> {
  try {
    const response = await instance.request<T>(config)
    return response.data
  } catch (error) {
    if (error instanceof AxiosError) {
      const status = error.response?.status ?? 0
      const detail = error.response?.data
      // 原因在构造时就还原进 message：`detail` 里那份留给上层做分支判断，
      // 但任何直接渲染 `error.message` 的地方都不该再拿到 axios 那句英文。
      if (detail === undefined) throw new WorkflowApiError(status, `接口调用失败（HTTP ${status}）：${error.message}`, undefined)
      const reason = describeDetail(detail)
      throw new WorkflowApiError(status, reason === '' ? `接口调用失败（HTTP ${status}）：${error.message}` : `接口调用失败（HTTP ${status}）：${reason}`, detail)
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
  /** 后端这格有默认值（`by: str = Field(default="unknown")`，workflow_api.py:95），
   *  所以"没填"应当是不发这个键，而不是前端替人填一个角色名。 */
  by?: string
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

/** 归档与取消归档的回执：都是"这一版现在的状态"，不产生新版本。 */
export interface DefinitionStatusResult {
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
      request<DefinitionStatusResult>({
        url: `${BASE}/definitions/${encodeURIComponent(workflowId)}/archive`,
        method: 'POST',
      }),

    /** 归档的撤销。同名已有更新版本时后端回 400，理由里会指名该撤哪一版。 */
    restoreDefinition: (workflowId: string) =>
      request<DefinitionStatusResult>({
        url: `${BASE}/definitions/${encodeURIComponent(workflowId)}/restore`,
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
