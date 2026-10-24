/**
 * 工作流传输契约（ADR-0010）：gateway 接口、传输 DTO 与类型化错误。
 *
 * 本文件不依赖 axios / Vue / Pinia：store 与组件只认这里的形状，
 * 具体 HTTP 实现与响应解析分别落在 `client.ts` / `codec.ts`。
 */
import type { EdgeDef, JsonValue, NodeDef, NodeState, OnFailure, RetryPolicy, WorkflowDef } from '@/utils/graph'
import type {
  RenameLineageResult,
  WorkflowDocument,
  WorkflowDraft,
  WorkflowVersionSummary,
} from '@/utils/graph/document'

/** 后端 `WorkflowErrorCode` + 前端自产的两类传输错误。 */
export type WorkflowErrorCode =
  | 'validation_failed'
  | 'not_found'
  | 'revision_conflict'
  | 'name_conflict'
  | 'no_changes'
  | 'archived'
  | 'preview_limit'
  | 'invalid_schema_version'
  | 'document_too_large'
  | 'internal'
  | 'network_error'
  | 'aborted'
  | 'invalid_response'

export const WORKFLOW_ERROR_LABELS: Record<WorkflowErrorCode, string> = {
  validation_failed: '内容校验未通过',
  not_found: '目标不存在',
  revision_conflict: '草稿已被其他会话修改',
  name_conflict: '工作流名称已存在',
  no_changes: '内容与当前版本一致',
  archived: '该工作流已归档',
  preview_limit: '试运行数量已达上限',
  invalid_schema_version: '文档版本不受支持',
  document_too_large: '文档超出大小上限',
  internal: '服务端内部错误',
  network_error: '网络请求失败',
  aborted: '请求已取消',
  invalid_response: '服务端响应格式不符',
}

export class WorkflowError extends Error {
  readonly code: WorkflowErrorCode
  readonly status: number
  readonly details: Record<string, unknown>

  constructor(code: WorkflowErrorCode, status: number, message: string, details: Record<string, unknown> = {}) {
    super(message)
    this.name = 'WorkflowError'
    this.code = code
    this.status = status
    this.details = details
  }
}

export function isWorkflowValidationError(error: unknown): boolean {
  return (
    error instanceof WorkflowError &&
    (error.code === 'validation_failed' ||
      error.code === 'invalid_schema_version' ||
      error.code === 'document_too_large')
  )
}

// ---------- 请求体（workflow_api.py） ----------

export interface NodeInput {
  node_id: string
  type: string
  name: string
  config: Record<string, JsonValue>
  sla_ms: number
  timeout_ms: number
  on_failure: OnFailure
  retry: RetryPolicy
}

export type EdgeInput = EdgeDef

export interface DefinitionInput {
  name: string
  description: string
  nodes: NodeInput[]
  edges: EdgeInput[]
}

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
  choice: string
  by?: string
  comment: string
  level?: number
}

export interface InsertNodeInput {
  after: string
  node: NodeInput
}

export interface DraftPutInput {
  base_revision: number
  document: WorkflowDocument
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

export interface DefinitionSummary {
  workflow_id: string
  lineage_id: string
  name: string
  version: number
  status: string
  description: string
  node_count: number
  edge_count: number
  created_at: string
  content_hash: string
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
  payload: Record<string, JsonValue>
  nodes: InstanceNodeRun[]
}

export interface InstancesResponse {
  count: number
  items: InstanceDetail[]
}

export type StartResult = InstanceDetail

/** D9 数据飞轮的去向回执：只在签字那一次响应里给，不进实例快照。 */
export interface LabelCapture {
  status: string
  原因?: string
  case_id?: string
  真值?: string
  样本?: Record<string, JsonValue>
}

export interface DecisionResult extends InstanceDetail {
  标注沉淀?: LabelCapture
}

export interface NodeConfigResult {
  node_id: string
  config: Record<string, JsonValue>
}

export interface InsertNodeResult {
  instance_id: string
  node_id: string
  after: string
}

/**
 * 工作流 gateway。生产实现见 `client.ts`；测试注入桩实现。
 *
 * `signal` 一律放在最后：store 在切换文档/卸载页面时用它取消在途请求，
 * 组件层不需要知道 HTTP 细节。
 */
export interface WorkflowGateway {
  nodeTypes(signal?: AbortSignal): Promise<NodeTypesResponse>

  definitions(signal?: AbortSignal): Promise<DefinitionsResponse>
  definition(workflowId: string, signal?: AbortSignal): Promise<WorkflowDef>
  createDefinition(payload: DefinitionInput, signal?: AbortSignal): Promise<DefinitionWriteResult>
  reviseDefinition(workflowId: string, payload: ReviseInput, signal?: AbortSignal): Promise<DefinitionWriteResult>
  archiveDefinition(workflowId: string, signal?: AbortSignal): Promise<DefinitionStatusResult>
  restoreDefinition(workflowId: string, signal?: AbortSignal): Promise<DefinitionStatusResult>

  startInstance(payload: StartInput, signal?: AbortSignal): Promise<StartResult>
  instances(signal?: AbortSignal): Promise<InstancesResponse>
  instance(instanceId: string, signal?: AbortSignal): Promise<InstanceDetail>
  submitDecision(
    instanceId: string,
    nodeId: string,
    payload: DecisionInput,
    signal?: AbortSignal,
  ): Promise<DecisionResult>
  patchNodeConfig(
    instanceId: string,
    nodeId: string,
    config: Record<string, JsonValue>,
    signal?: AbortSignal,
  ): Promise<NodeConfigResult>
  insertNode(instanceId: string, payload: InsertNodeInput, signal?: AbortSignal): Promise<InsertNodeResult>
  bypassNode(instanceId: string, nodeId: string, reason?: string, signal?: AbortSignal): Promise<StartResult>
  abortInstance(instanceId: string, reason?: string, signal?: AbortSignal): Promise<StartResult>

  createLineage(name: string, signal?: AbortSignal): Promise<WorkflowDraft>
  listVersions(lineageId: string, signal?: AbortSignal): Promise<WorkflowVersionSummary[]>
  getVersionDocument(workflowId: string, signal?: AbortSignal): Promise<WorkflowDocument>
  getDraft(lineageId: string, signal?: AbortSignal): Promise<WorkflowDraft>
  putDraft(lineageId: string, payload: DraftPutInput, signal?: AbortSignal): Promise<WorkflowDraft>
  publish(lineageId: string, baseRevision: number, signal?: AbortSignal): Promise<WorkflowVersionSummary>
  revertToDraft(lineageId: string, workflowId: string, signal?: AbortSignal): Promise<WorkflowDraft>
  renameLineage(lineageId: string, name: string, signal?: AbortSignal): Promise<RenameLineageResult>
}

export type { NodeDef, WorkflowDef, WorkflowDocument, WorkflowDraft, WorkflowVersionSummary, RenameLineageResult }
