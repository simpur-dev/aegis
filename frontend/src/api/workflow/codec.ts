/**
 * 工作流响应编解码（ADR-0010）：所有 `unknown` 载荷在这里收成领域类型。
 *
 * 解析失败必须带路径（`nodes[3].config.sla_ms`），不允许 `as WorkflowDocument` 直通；
 * 位置类下标保留原形，方便把错误直接指回画布上的第几个节点。
 */
import {
  isInstanceStatus,
  type EdgeDef,
  type JsonValue,
  type NodeDef,
  type NodeState,
  type NodeType,
  type OnFailure,
  type Position,
  type RetryPolicy,
  type WorkflowDef,
} from '@/utils/graph'
import {
  MAX_DOCUMENT_EDGES,
  MAX_DOCUMENT_NODES,
  MAX_STICKY_NOTES,
  WORKFLOW_ID_PATTERN,
  DOCUMENT_SCHEMA_VERSION,
  type RenameLineageResult,
  type StickyNote,
  type StickyNoteColor,
  type Viewport,
  type WorkflowDocument,
  type WorkflowDraft,
  type WorkflowLayout,
  type WorkflowVersionSummary,
  copyLayout,
  copyNodeDef,
} from '@/utils/graph/document'
import { describeValidationDetail } from '@/utils/validationDetail'

import {
  WORKFLOW_ERROR_LABELS,
  WorkflowError,
  type DecisionResult,
  type DefinitionStatusResult,
  type DefinitionSummary,
  type DefinitionWriteResult,
  type DefinitionsResponse,
  type DraftPutInput,
  type InsertNodeResult,
  type InstanceDetail,
  type InstanceNodeRun,
  type InstancesResponse,
  type LabelCapture,
  type NodeConfigResult,
  type NodeInput,
  type NodeTypeEntry,
  type NodeTypesResponse,
  type WorkflowErrorCode,
} from './contract'

/**
 * 画布上这些格子的叫法，用来给 422 里的字段名配中文。
 * 只给单层字段配：`nodes.0.config.limit` 这种路径保持原形——下标正是定位线索。
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

/** 把后端 detail 还原成人能读的一句话（旧端点字符串 / FastAPI 422 数组 / 新统一错误体）。 */
export function describeDetail(detail: unknown): string {
  return describeValidationDetail(detail, WORKFLOW_FIELD_LABELS)
}

const NODE_STATES: readonly NodeState[] = [
  'pending',
  'ready',
  'running',
  'succeeded',
  'failed',
  'skipped',
  'bypassed',
  'degraded',
  'awaiting_human',
  'cancelled',
  'timeout',
]
const NODE_CATEGORIES = ['io', 'logic', 'intelligence', 'human'] as const
const ON_FAILURES: readonly OnFailure[] = ['retry', 'degrade', 'escalate', 'skip', 'abort']
const STICKY_COLORS: readonly StickyNoteColor[] = ['amber', 'blue', 'green', 'violet']
const DEFINITION_STATUSES = ['active', 'archived'] as const
const ERROR_CODES: readonly WorkflowErrorCode[] = [
  'validation_failed',
  'not_found',
  'revision_conflict',
  'name_conflict',
  'no_changes',
  'archived',
  'preview_limit',
  'invalid_schema_version',
  'document_too_large',
  'internal',
  'network_error',
  'aborted',
  'invalid_response',
]
const MAX_COORD = 1_000_000

function fail(path: string, message: string): never {
  throw new WorkflowError('invalid_response', 0, `响应格式不符（${path}）：${message}`, { path })
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function objectAt(value: unknown, path: string): Record<string, unknown> {
  if (!isRecord(value)) fail(path, '应为对象')
  return value
}

function arrayAt(value: unknown, path: string, max = Number.POSITIVE_INFINITY): unknown[] {
  if (!Array.isArray(value)) fail(path, '应为数组')
  if (value.length > max) fail(path, `不得超过 ${max} 项（实际 ${value.length}）`)
  return value
}

function stringAt(
  value: unknown,
  path: string,
  options: { min?: number; max?: number; nonEmpty?: boolean } = {},
): string {
  if (typeof value !== 'string') fail(path, '应为字符串')
  const length = [...value].length
  const min = options.min ?? (options.nonEmpty === true ? 1 : 0)
  if (length < min) fail(path, `长度不得小于 ${min}`)
  if (options.max !== undefined && length > options.max) fail(path, `长度不得超过 ${options.max}`)
  return value
}

function nullableStringAt(value: unknown, path: string): string | null {
  if (value === null) return null
  return stringAt(value, path)
}

function integerAt(
  value: unknown,
  path: string,
  min = Number.NEGATIVE_INFINITY,
  max = Number.POSITIVE_INFINITY,
): number {
  if (typeof value !== 'number' || !Number.isInteger(value)) fail(path, '应为整数')
  if (value < min || value > max) fail(path, `应在 ${min}-${max} 之间（实际 ${value}）`)
  return value
}

function numberAt(value: unknown, path: string, min = -MAX_COORD, max = MAX_COORD): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) fail(path, '应为有限数字')
  if (value < min || value > max) fail(path, `应在 ${min}-${max} 之间（实际 ${value}）`)
  return value
}

function booleanAt(value: unknown, path: string): boolean {
  if (typeof value !== 'boolean') fail(path, '应为布尔值')
  return value
}

function enumAt<T extends string>(value: unknown, path: string, allowed: readonly T[]): T {
  if (typeof value !== 'string' || !(allowed as readonly string[]).includes(value)) {
    fail(path, `取值应为 ${allowed.join(' | ')} 之一（实际 ${String(value)}）`)
  }
  return value as T
}

function stringArrayAt(value: unknown, path: string): string[] {
  return arrayAt(value, path).map((item, index) => stringAt(item, `${path}[${index}]`))
}

/** `Record<string, number>`（节点参数下限）：键必须非空，值必须是有限数。 */
function numberRecordAt(value: unknown, path: string): Record<string, number> {
  const record = objectAt(value, path)
  const out: Record<string, number> = {}
  for (const [key, raw] of Object.entries(record)) {
    if (key === '') fail(path, '键不得为空')
    if (typeof raw !== 'number' || !Number.isFinite(raw)) fail(`${path}.${key}`, '应为有限数字')
    out[key] = raw
  }
  return out
}

export function decodeJsonValue(value: unknown, path: string): JsonValue {
  if (value === null || typeof value === 'boolean' || typeof value === 'string') return value
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) fail(path, '数字必须有限')
    return value
  }
  if (Array.isArray(value)) return value.map((item, index) => decodeJsonValue(item, `${path}[${index}]`))
  if (isRecord(value)) {
    const decoded: Record<string, JsonValue> = {}
    for (const [key, item] of Object.entries(value)) {
      if (key === '__proto__') fail(`${path}.__proto__`, '不允许原型污染键')
      decoded[key] = decodeJsonValue(item, `${path}.${key}`)
    }
    return decoded
  }
  fail(path, '应为 JSON 值')
}

function jsonRecordAt(value: unknown, path: string): Record<string, JsonValue> {
  const record = objectAt(value, path)
  const decoded: Record<string, JsonValue> = {}
  for (const [key, item] of Object.entries(record)) {
    if (key === '__proto__') fail(`${path}.__proto__`, '不允许原型污染键')
    decoded[key] = decodeJsonValue(item, `${path}.${key}`)
  }
  return decoded
}

function retryAt(value: unknown, path: string): RetryPolicy {
  const record = objectAt(value, path)
  return {
    max_attempts: integerAt(record.max_attempts, `${path}.max_attempts`, 0, 5),
    backoff_ms: integerAt(record.backoff_ms, `${path}.backoff_ms`, 0, 60_000),
  }
}

/** 未知节点类型保留原串：服务端可能比本构建新，打开它应只读降级而不是整页报错。 */
function nodeTypeAt(value: unknown, path: string): NodeType {
  const raw = stringAt(value, path, { min: 2, max: 32 })
  if (!/^[a-z][a-z0-9_]{1,31}$/.test(raw)) fail(path, `节点类型非法: ${raw}`)
  return raw as NodeType
}

export function decodeNodeDef(value: unknown, path: string): NodeDef {
  const record = objectAt(value, path)
  const nodeId = stringAt(record.node_id, `${path}.node_id`, { min: 1, max: 32 })
  if (!/^[a-z][a-z0-9_-]{0,31}$/.test(nodeId)) fail(`${path}.node_id`, `节点 ID 非法: ${nodeId}`)
  const sla = integerAt(record.sla_ms, `${path}.sla_ms`, 100, 3_600_000)
  const timeout = integerAt(record.timeout_ms, `${path}.timeout_ms`, 100, 3_600_000)
  if (timeout > sla) fail(`${path}.timeout_ms`, `不得大于 sla_ms (${sla})`)
  return {
    node_id: nodeId,
    type: nodeTypeAt(record.type, `${path}.type`),
    name: stringAt(record.name, `${path}.name`, { max: 64 }),
    config: jsonRecordAt(record.config, `${path}.config`),
    sla_ms: sla,
    timeout_ms: timeout,
    on_failure: enumAt(record.on_failure, `${path}.on_failure`, ON_FAILURES),
    retry: retryAt(record.retry, `${path}.retry`),
  }
}

export function decodeEdgeDef(value: unknown, path: string): EdgeDef {
  const record = objectAt(value, path)
  return {
    source: stringAt(record.source, `${path}.source`, { nonEmpty: true }),
    target: stringAt(record.target, `${path}.target`, { nonEmpty: true }),
    condition: stringAt(record.condition, `${path}.condition`, { max: 64 }),
  }
}

function schemaVersionAt(value: unknown, path: string, required: boolean): number {
  if (value === undefined && !required) return DOCUMENT_SCHEMA_VERSION
  const version = integerAt(value, path, 1)
  if (version !== DOCUMENT_SCHEMA_VERSION) {
    throw new WorkflowError('invalid_schema_version', 422, `不支持的文档 schema_version: ${version}`, { path, version })
  }
  return version
}

export function decodeWorkflowDef(value: unknown, path = 'definition'): WorkflowDef {
  const record = objectAt(value, path)
  const workflowId = stringAt(record.workflow_id, `${path}.workflow_id`, { min: 1, max: 32 })
  if (!WORKFLOW_ID_PATTERN.test(workflowId)) fail(`${path}.workflow_id`, `形状不合法: ${workflowId}`)
  const lineageRaw = record.lineage_id
  const lineage =
    lineageRaw === undefined || lineageRaw === ''
      ? workflowId
      : stringAt(lineageRaw, `${path}.lineage_id`, { min: 12, max: 32 })
  if (!WORKFLOW_ID_PATTERN.test(lineage)) fail(`${path}.lineage_id`, `形状不合法: ${lineage}`)
  const status = enumAt(record.status, `${path}.status`, DEFINITION_STATUSES)
  return {
    workflow_id: workflowId,
    lineage_id: lineage,
    schema_version: schemaVersionAt(record.schema_version, `${path}.schema_version`, false),
    name: stringAt(record.name, `${path}.name`, { min: 1, max: 64 }),
    description: stringAt(record.description, `${path}.description`, { max: 512 }),
    version: integerAt(record.version, `${path}.version`, 1),
    nodes: arrayAt(record.nodes, `${path}.nodes`, MAX_DOCUMENT_NODES).map((node, index) =>
      decodeNodeDef(node, `${path}.nodes[${index}]`),
    ),
    edges: arrayAt(record.edges, `${path}.edges`, MAX_DOCUMENT_EDGES).map((edge, index) =>
      decodeEdgeDef(edge, `${path}.edges[${index}]`),
    ),
    created_by: stringAt(record.created_by, `${path}.created_by`, { min: 1, max: 64 }),
    created_at: stringAt(record.created_at, `${path}.created_at`, { min: 1 }),
    status,
    content_hash: stringAt(record.content_hash, `${path}.content_hash`, { max: 80 }),
  }
}

function positionAt(value: unknown, path: string): Position {
  const record = objectAt(value, path)
  return {
    x: numberAt(record.x, `${path}.x`),
    y: numberAt(record.y, `${path}.y`),
  }
}

function viewportAt(value: unknown, path: string): Viewport {
  const record = objectAt(value, path)
  return {
    x: numberAt(record.x, `${path}.x`),
    y: numberAt(record.y, `${path}.y`),
    zoom: numberAt(record.zoom, `${path}.zoom`, 0.1, 4),
  }
}

function stickyNoteAt(value: unknown, path: string): StickyNote {
  const record = objectAt(value, path)
  const noteId = stringAt(record.note_id, `${path}.note_id`, { min: 1, max: 32 })
  if (!/^[a-z][a-z0-9_-]{0,31}$/.test(noteId)) fail(`${path}.note_id`, `形状不合法: ${noteId}`)
  return {
    note_id: noteId,
    x: numberAt(record.x, `${path}.x`),
    y: numberAt(record.y, `${path}.y`),
    width: numberAt(record.width, `${path}.width`, 120, 800),
    height: numberAt(record.height, `${path}.height`, 80, 800),
    text: stringAt(record.text, `${path}.text`, { max: 4_000 }),
    color: enumAt(record.color, `${path}.color`, STICKY_COLORS),
  }
}

function layoutAt(value: unknown, path: string, nodeIds: ReadonlySet<string>): WorkflowLayout {
  const record = objectAt(value, path)
  const rawPositions = objectAt(record.positions, `${path}.positions`)
  const positions: Record<string, Position> = {}
  for (const [nodeId, raw] of Object.entries(rawPositions)) {
    if (nodeId === '') fail(`${path}.positions`, 'node_id 不能为空')
    if ([...nodeId].length > 32) fail(`${path}.positions.${nodeId}`, 'node_id 不得超过 32 字符')
    if (!nodeIds.has(nodeId)) continue
    positions[nodeId] = positionAt(raw, `${path}.positions.${nodeId}`)
  }
  const notes = arrayAt(record.notes, `${path}.notes`, MAX_STICKY_NOTES).map((note, index) =>
    stickyNoteAt(note, `${path}.notes[${index}]`),
  )
  const noteIds = new Set<string>()
  for (const note of notes) {
    if (noteIds.has(note.note_id)) fail(`${path}.notes`, `备注 note_id 重复: ${note.note_id}`)
    noteIds.add(note.note_id)
  }
  return {
    positions,
    viewport:
      record.viewport === null || record.viewport === undefined
        ? null
        : viewportAt(record.viewport, `${path}.viewport`),
    notes,
  }
}

export function decodeWorkflowDocument(value: unknown, path = 'document'): WorkflowDocument {
  const record = objectAt(value, path)
  const lineage = stringAt(record.lineage_id, `${path}.lineage_id`, { min: 12, max: 32 })
  if (!WORKFLOW_ID_PATTERN.test(lineage)) fail(`${path}.lineage_id`, `形状不合法: ${lineage}`)
  const nodes = arrayAt(record.nodes, `${path}.nodes`, MAX_DOCUMENT_NODES).map((node, index) =>
    decodeNodeDef(node, `${path}.nodes[${index}]`),
  )
  if (nodes.length === 0) fail(`${path}.nodes`, '至少需要一个节点')
  const edges = arrayAt(record.edges, `${path}.edges`, MAX_DOCUMENT_EDGES).map((edge, index) =>
    decodeEdgeDef(edge, `${path}.edges[${index}]`),
  )
  const ids = new Set(nodes.map((node) => node.node_id))
  return {
    schema_version: schemaVersionAt(record.schema_version, `${path}.schema_version`, true),
    lineage_id: lineage,
    name: stringAt(record.name, `${path}.name`, { min: 1, max: 64 }),
    description: stringAt(record.description, `${path}.description`, { max: 512 }),
    nodes,
    edges,
    layout: layoutAt(record.layout, `${path}.layout`, ids),
  }
}

export function decodeWorkflowDraft(value: unknown, path = 'draft'): WorkflowDraft {
  const record = objectAt(value, path)
  const lineage = stringAt(record.lineage_id, `${path}.lineage_id`, { min: 12, max: 32 })
  if (!WORKFLOW_ID_PATTERN.test(lineage)) fail(`${path}.lineage_id`, `形状不合法: ${lineage}`)
  const document = decodeWorkflowDocument(record.document, `${path}.document`)
  if (document.lineage_id !== lineage)
    fail(`${path}.document.lineage_id`, `与草稿 lineage_id 不一致: ${document.lineage_id}`)
  const baseRaw = record.base_workflow_id
  const base =
    baseRaw === null || baseRaw === undefined
      ? null
      : stringAt(baseRaw, `${path}.base_workflow_id`, { min: 12, max: 32 })
  if (base !== null && !WORKFLOW_ID_PATTERN.test(base)) fail(`${path}.base_workflow_id`, `形状不合法: ${base}`)
  return {
    lineage_id: lineage,
    base_workflow_id: base,
    revision: integerAt(record.revision, `${path}.revision`, 1),
    document,
    updated_at: stringAt(record.updated_at, `${path}.updated_at`, { min: 1 }),
  }
}

export function decodeVersionSummary(value: unknown, path = 'version'): WorkflowVersionSummary {
  const record = objectAt(value, path)
  const workflowId = stringAt(record.workflow_id, `${path}.workflow_id`, { min: 12, max: 32 })
  if (!WORKFLOW_ID_PATTERN.test(workflowId)) fail(`${path}.workflow_id`, `形状不合法: ${workflowId}`)
  const lineage = stringAt(record.lineage_id, `${path}.lineage_id`, { min: 12, max: 32 })
  if (!WORKFLOW_ID_PATTERN.test(lineage)) fail(`${path}.lineage_id`, `形状不合法: ${lineage}`)
  return {
    workflow_id: workflowId,
    lineage_id: lineage,
    version: integerAt(record.version, `${path}.version`, 1),
    name: stringAt(record.name, `${path}.name`, { min: 1, max: 64 }),
    description: stringAt(record.description, `${path}.description`, { max: 512 }),
    status: enumAt(record.status, `${path}.status`, DEFINITION_STATUSES),
    node_count: integerAt(record.node_count, `${path}.node_count`, 0),
    edge_count: integerAt(record.edge_count, `${path}.edge_count`, 0),
    created_at: stringAt(record.created_at, `${path}.created_at`, { min: 1 }),
    content_hash: stringAt(record.content_hash, `${path}.content_hash`, { max: 80 }),
    is_current: booleanAt(record.is_current, `${path}.is_current`),
  }
}

export function decodeVersionList(value: unknown): WorkflowVersionSummary[] {
  const record = objectAt(value, 'versions')
  const items = arrayAt(record.items, 'versions.items').map((item, index) =>
    decodeVersionSummary(item, `versions.items[${index}]`),
  )
  if (integerAt(record.count, 'versions.count', 0) !== items.length) fail('versions.count', '与 items 长度不一致')
  return items
}

export function decodeRenameLineageResult(value: unknown, path = 'rename'): RenameLineageResult {
  const record = objectAt(value, path)
  return {
    workflow_id: stringAt(record.workflow_id, `${path}.workflow_id`, { min: 12, max: 32 }),
    lineage_id: stringAt(record.lineage_id, `${path}.lineage_id`, { min: 12, max: 32 }),
    name: stringAt(record.name, `${path}.name`, { min: 1, max: 64 }),
    version: integerAt(record.version, `${path}.version`, 1),
    draft:
      record.draft === null || record.draft === undefined ? null : decodeWorkflowDraft(record.draft, `${path}.draft`),
  }
}

export function decodeNodeTypesResponse(value: unknown): NodeTypesResponse {
  const record = objectAt(value, 'node-types')
  const items = arrayAt(record.items, 'node-types.items').map((item, index): NodeTypeEntry => {
    const path = `node-types.items[${index}]`
    const entry = objectAt(item, path)
    return {
      type: stringAt(entry.type, `${path}.type`, { min: 2, max: 32 }),
      description: stringAt(entry.description, `${path}.description`),
      category: enumAt(entry.category, `${path}.category`, NODE_CATEGORIES),
      required_config: stringArrayAt(entry.required_config, `${path}.required_config`),
      optional_config: stringArrayAt(entry.optional_config, `${path}.optional_config`),
      min_config: numberRecordAt(entry.min_config, `${path}.min_config`),
      outputs: stringArrayAt(entry.outputs, `${path}.outputs`),
    }
  })
  if (integerAt(record.count, 'node-types.count', 0) !== items.length) fail('node-types.count', '与 items 长度不一致')
  return { count: items.length, items }
}

export function decodeDefinitionsResponse(value: unknown): DefinitionsResponse {
  const record = objectAt(value, 'definitions')
  const items = arrayAt(record.items, 'definitions.items').map((item, index): DefinitionSummary => {
    const path = `definitions.items[${index}]`
    const row = objectAt(item, path)
    return {
      workflow_id: stringAt(row.workflow_id, `${path}.workflow_id`, { min: 12, max: 32 }),
      lineage_id: stringAt(row.lineage_id, `${path}.lineage_id`, { min: 12, max: 32 }),
      name: stringAt(row.name, `${path}.name`, { min: 1, max: 64 }),
      version: integerAt(row.version, `${path}.version`, 1),
      status: enumAt(row.status, `${path}.status`, DEFINITION_STATUSES),
      description: stringAt(row.description, `${path}.description`, { max: 512 }),
      node_count: integerAt(row.node_count, `${path}.node_count`, 0),
      edge_count: integerAt(row.edge_count, `${path}.edge_count`, 0),
      created_at: stringAt(row.created_at, `${path}.created_at`, { min: 1 }),
      content_hash: stringAt(row.content_hash, `${path}.content_hash`, { max: 80 }),
    }
  })
  if (integerAt(record.count, 'definitions.count', 0) !== items.length) fail('definitions.count', '与 items 长度不一致')
  return { count: items.length, items }
}

export function decodeDefinitionWriteResult(value: unknown, path = 'definition-write'): DefinitionWriteResult {
  const record = objectAt(value, path)
  return {
    workflow_id: stringAt(record.workflow_id, `${path}.workflow_id`, { min: 12, max: 32 }),
    name: stringAt(record.name, `${path}.name`, { min: 1, max: 64 }),
    version: integerAt(record.version, `${path}.version`, 1),
  }
}

export function decodeDefinitionStatusResult(value: unknown, path = 'definition-status'): DefinitionStatusResult {
  const record = objectAt(value, path)
  return {
    workflow_id: stringAt(record.workflow_id, `${path}.workflow_id`, { min: 12, max: 32 }),
    status: enumAt(record.status, `${path}.status`, DEFINITION_STATUSES),
  }
}

function instanceNodeRunAt(value: unknown, path: string): InstanceNodeRun {
  const record = objectAt(value, path)
  const state = stringAt(record.state, `${path}.state`, { min: 1, max: 32 })
  if (!(NODE_STATES as readonly string[]).includes(state)) fail(`${path}.state`, `未知节点状态: ${state}`)
  const latency = record.schedule_latency_ms
  const duration = record.duration_ms
  return {
    node_id: stringAt(record.node_id, `${path}.node_id`, { min: 1, max: 32 }),
    type: stringAt(record.type, `${path}.type`, { min: 2, max: 32 }),
    state: state as NodeState,
    attempts: integerAt(record.attempts, `${path}.attempts`, 0),
    schedule_latency_ms: latency === null ? null : numberAt(latency, `${path}.schedule_latency_ms`, 0),
    duration_ms: duration === null ? null : numberAt(duration, `${path}.duration_ms`, 0),
    output: jsonRecordAt(record.output, `${path}.output`),
    error: nullableStringAt(record.error, `${path}.error`),
    notes: stringArrayAt(record.notes, `${path}.notes`),
  }
}

export function decodeInstanceDetail(value: unknown, path = 'instance'): InstanceDetail {
  const record = objectAt(value, path)
  const status = stringAt(record.status, `${path}.status`, { min: 1, max: 32 })
  if (!isInstanceStatus(status)) fail(`${path}.status`, `未知实例状态: ${status}`)
  return {
    instance_id: stringAt(record.instance_id, `${path}.instance_id`, { min: 1, max: 64 }),
    workflow_id: stringAt(record.workflow_id, `${path}.workflow_id`, { min: 12, max: 32 }),
    workflow_version: integerAt(record.workflow_version, `${path}.workflow_version`, 1),
    trace_id: stringAt(record.trace_id, `${path}.trace_id`, { min: 1, max: 64 }),
    status,
    error: nullableStringAt(record.error, `${path}.error`),
    payload: record.payload === undefined ? {} : jsonRecordAt(record.payload, `${path}.payload`),
    nodes: arrayAt(record.nodes, `${path}.nodes`).map((run, index) =>
      instanceNodeRunAt(run, `${path}.nodes[${index}]`),
    ),
  }
}

export function decodeInstancesResponse(value: unknown): InstancesResponse {
  const record = objectAt(value, 'instances')
  const items = arrayAt(record.items, 'instances.items').map((item, index) =>
    decodeInstanceDetail(item, `instances.items[${index}]`),
  )
  if (integerAt(record.count, 'instances.count', 0) !== items.length) fail('instances.count', '与 items 长度不一致')
  return { count: items.length, items }
}

function labelCaptureAt(value: unknown, path: string): LabelCapture {
  const record = objectAt(value, path)
  const capture: LabelCapture = { status: stringAt(record.status, `${path}.status`, { min: 1, max: 32 }) }
  if (record.原因 !== undefined) capture.原因 = stringAt(record.原因, `${path}.原因`)
  if (record.case_id !== undefined) capture.case_id = stringAt(record.case_id, `${path}.case_id`)
  if (record.真值 !== undefined) capture.真值 = stringAt(record.真值, `${path}.真值`)
  if (record.样本 !== undefined) capture.样本 = jsonRecordAt(record.样本, `${path}.样本`)
  return capture
}

export function decodeDecisionResult(value: unknown): DecisionResult {
  const detail = decodeInstanceDetail(value)
  const record = objectAt(value, 'decision')
  return record.标注沉淀 === undefined
    ? detail
    : { ...detail, 标注沉淀: labelCaptureAt(record.标注沉淀, 'decision.标注沉淀') }
}

export function decodeNodeConfigResult(value: unknown): NodeConfigResult {
  const record = objectAt(value, 'node-config')
  return {
    node_id: stringAt(record.node_id, 'node-config.node_id', { min: 1, max: 32 }),
    config: jsonRecordAt(record.config, 'node-config.config'),
  }
}

export function decodeInsertNodeResult(value: unknown): InsertNodeResult {
  const record = objectAt(value, 'insert-node')
  return {
    instance_id: stringAt(record.instance_id, 'insert-node.instance_id', { min: 1, max: 64 }),
    node_id: stringAt(record.node_id, 'insert-node.node_id', { min: 1, max: 32 }),
    after: stringAt(record.after, 'insert-node.after', { min: 1, max: 32 }),
  }
}

// ---------- 请求体编码 ----------

/** NodeDef → NodeInput：字段与后端一一对应，不丢任何可写字段。 */
export function toNodeInput(node: NodeDef): NodeInput {
  return {
    node_id: node.node_id,
    type: node.type,
    name: node.name.trim(),
    // 与 copyNodeDef 同一份手抄逻辑：调用方可能把 Pinia 里的响应式代理递进来，
    // structuredClone 会在代理上直接抛 DataCloneError。
    config: copyNodeDef(node).config,
    sla_ms: node.sla_ms,
    timeout_ms: node.timeout_ms,
    on_failure: node.on_failure,
    retry: { ...node.retry },
  }
}

export function toNodeInputs(nodes: readonly NodeDef[]): NodeInput[] {
  return nodes.map(toNodeInput)
}

export function encodeDocument(document: WorkflowDocument): WorkflowDocument {
  return {
    ...document,
    nodes: document.nodes.map(copyNodeDef),
    edges: document.edges.map((edge) => ({ ...edge })),
    layout: copyLayout(document.layout),
  }
}

export function encodeDraftPut(payload: DraftPutInput): DraftPutInput {
  return { base_revision: payload.base_revision, document: encodeDocument(payload.document) }
}

// ---------- 错误体 ----------

export interface DecodedError {
  code: WorkflowErrorCode
  message: string
  details: Record<string, unknown>
}

function isErrorCode(value: string): value is WorkflowErrorCode {
  return (ERROR_CODES as readonly string[]).includes(value)
}

function codeForStatus(status: number): WorkflowErrorCode {
  if (status === 404) return 'not_found'
  if (status === 409) return 'name_conflict'
  if (status === 400 || status === 422) return 'validation_failed'
  return 'internal'
}

/**
 * 归一化后端错误体：
 * - 新端点：`{detail: {code, message, details}}`
 * - 旧端点：`detail` 为字符串（WorkflowValidationError）或 FastAPI 422 数组
 * - 其它：按 HTTP 状态给码，原样附上载荷。
 */
export function decodeWorkflowError(payload: unknown, status: number): DecodedError {
  const fallbackCode = codeForStatus(status)
  if (isRecord(payload)) {
    const detail = payload.detail
    if (isRecord(detail)) {
      const rawCode = typeof detail.code === 'string' ? detail.code : ''
      const code = rawCode !== '' && isErrorCode(rawCode) ? rawCode : fallbackCode
      const message =
        typeof detail.message === 'string' && detail.message.trim() !== ''
          ? detail.message
          : WORKFLOW_ERROR_LABELS[code]
      const details = isRecord(detail.details) ? detail.details : {}
      const fieldErrors = details.errors
      if (Array.isArray(fieldErrors) && fieldErrors.length > 0) {
        const described = describeValidationDetail(fieldErrors, WORKFLOW_FIELD_LABELS)
        if (described !== '') return { code, message: `${message}：${described}`, details }
      }
      return { code, message, details }
    }
    if (detail !== undefined) {
      const described = describeDetail(detail)
      if (described !== '') return { code: fallbackCode, message: described, details: { detail } }
    }
  }
  const described = describeDetail(payload)
  return {
    code: fallbackCode,
    message: described !== '' ? described : WORKFLOW_ERROR_LABELS[fallbackCode],
    details: { detail: payload },
  }
}
