/**
 * 工作流文档：草稿/预览/发布的统一载荷（ADR-0009 §1，后端镜像 documents.py）。
 *
 * `WorkflowDef` 是引擎输入，只描述图身份；文档额外带 lineage、名称与版式。
 * 版式不进 `WorkflowDef`——拖动节点不该改变引擎眼中的模板，但版式又要随版本回溯。
 * 本模块只做纯数据：不依赖 Vue/Pinia/axios/UI。
 */
import {
  findCyclePath,
  layerPositions,
  type EdgeDef,
  type JsonValue,
  type NodeDef,
  type Position,
  type StickyNote,
  type WorkflowDef,
} from '../graph'

export type { StickyNote, StickyNoteColor } from '../graph'

export const DOCUMENT_SCHEMA_VERSION = 1
export const MAX_DOCUMENT_NODES = 200
export const MAX_DOCUMENT_EDGES = 500
export const MAX_STICKY_NOTES = 32
export const MAX_START_VARIABLES = 32
export const MAX_NODE_CONFIG_BYTES = 64 * 1024
export const MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
export const MAX_VARIABLE_REFS_PER_NODE = 32
export const MAX_VARIABLE_PATH_DEPTH = 8

export const WORKFLOW_ID_PATTERN = /^wf_[0-9a-f]{12}$/
export const DOCUMENT_NODE_ID_PATTERN = /^[a-z][a-z0-9_-]{0,31}$/
export const START_VARIABLE_NAME_PATTERN = /^[A-Za-z_][A-Za-z0-9_]{0,31}$/

const PROTOTYPE_SEGMENTS = new Set(['__proto__', 'prototype', 'constructor'])

export type StartVariableType = 'string' | 'number' | 'boolean' | 'json'

export interface Viewport {
  x: number
  y: number
  zoom: number
}

export interface WorkflowLayout {
  positions: Record<string, Position>
  viewport: Viewport | null
  notes: StickyNote[]
}

export interface StartVariable {
  name: string
  label: string
  type: StartVariableType
  required: boolean
  default: JsonValue
}

export interface WorkflowDocument {
  schema_version: number
  lineage_id: string
  name: string
  description: string
  nodes: NodeDef[]
  edges: EdgeDef[]
  layout: WorkflowLayout
}

/** 草稿：线内唯一的可变对象；revision 是 CAS 令牌。 */
export interface WorkflowDraft {
  lineage_id: string
  base_workflow_id: string | null
  revision: number
  document: WorkflowDocument
  updated_at: string
}

export interface WorkflowVersionSummary {
  workflow_id: string
  lineage_id: string
  version: number
  name: string
  description: string
  status: 'active' | 'archived'
  node_count: number
  edge_count: number
  created_at: string
  content_hash: string
  is_current: boolean
}

export interface RenameLineageResult {
  workflow_id: string
  lineage_id: string
  name: string
  version: number
  draft: WorkflowDraft | null
}

export function emptyLayout(): WorkflowLayout {
  return { positions: {}, viewport: null, notes: [] }
}

/**
 * 手抄一份 JSON，而不是 `structuredClone`：调用方拿到的常常是 Vue 的响应式代理，
 * 代理对象过不了 structuredClone（DataCloneError），而 config 本来就只有 JSON 值。
 */
function cloneJsonValue(value: JsonValue): JsonValue {
  if (Array.isArray(value)) return value.map((item) => cloneJsonValue(item))
  if (value !== null && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, cloneJsonValue(item)]))
  }
  return value
}

export function copyNodeDef(node: NodeDef): NodeDef {
  return { ...node, config: cloneJsonValue(node.config) as Record<string, JsonValue>, retry: { ...node.retry } }
}

export function copyEdgeDef(edge: EdgeDef): EdgeDef {
  return { ...edge }
}

/** 旧定义投影成文档：没保存过 layout 的定义得到空版式，读取不报错。 */
export function documentFromDefinition(
  definition: WorkflowDef,
  layout: WorkflowLayout = emptyLayout(),
): WorkflowDocument {
  return {
    schema_version: DOCUMENT_SCHEMA_VERSION,
    lineage_id:
      definition.lineage_id !== undefined && definition.lineage_id !== ''
        ? definition.lineage_id
        : definition.workflow_id,
    name: definition.name,
    description: definition.description,
    nodes: definition.nodes.map(copyNodeDef),
    edges: definition.edges.map(copyEdgeDef),
    layout: copyLayout(layout),
  }
}

export interface DefinitionIdentity {
  workflow_id?: string
  version?: number
  created_by?: string
  created_at?: string
  status?: 'active' | 'archived'
  content_hash?: string
}

/** 文档投影成画布域对象（`current`）：身份字段缺失时取 lineage 与平台默认值。 */
export function definitionFromDocument(document: WorkflowDocument, identity: DefinitionIdentity = {}): WorkflowDef {
  return {
    workflow_id: identity.workflow_id ?? document.lineage_id,
    lineage_id: document.lineage_id,
    schema_version: document.schema_version,
    name: document.name,
    description: document.description,
    version: identity.version ?? 1,
    nodes: document.nodes.map(copyNodeDef),
    edges: document.edges.map(copyEdgeDef),
    created_by: identity.created_by ?? 'platform.web',
    created_at: identity.created_at,
    status: identity.status ?? 'active',
    content_hash: identity.content_hash,
  }
}

export function copyLayout(layout: WorkflowLayout): WorkflowLayout {
  return {
    positions: Object.fromEntries(
      Object.entries(layout.positions).map(([nodeId, position]) => [nodeId, { ...position }]),
    ),
    viewport: layout.viewport === null ? null : { ...layout.viewport },
    notes: layout.notes.map((note) => ({ ...note })),
  }
}

/** 剪掉已删除节点残留的坐标；后端也在校验前做同一件事。 */
export function prunePositions(
  positions: Readonly<Record<string, Position>>,
  nodeIds: ReadonlySet<string>,
): Record<string, Position> {
  const pruned: Record<string, Position> = {}
  for (const [nodeId, position] of Object.entries(positions)) {
    if (nodeIds.has(nodeId)) pruned[nodeId] = { x: position.x, y: position.y }
  }
  return pruned
}

/** 分层自动布局：与画布 `defToGraph` 同一口径（同层纵向居中、层间横推）。 */
export function autoLayoutDocument(document: WorkflowDocument): WorkflowDocument {
  const ids = document.nodes.map((node) => node.node_id)
  if (ids.length === 0) return document
  let positions: Record<string, Position>
  try {
    positions = layerPositions(ids, document.edges)
  } catch {
    // 有环的草稿也允许编辑：布局退回网格，环路错误由 validateDocument 报
    positions = Object.fromEntries(
      ids.map((id, index) => [id, { x: (index % 4) * 280, y: Math.floor(index / 4) * 132 }]),
    )
  }
  return { ...document, layout: { ...document.layout, positions } }
}

/** 打开文档时的版式整理：残留坐标剪掉；完全没有坐标的旧定义自动布局。 */
export function ensureDocumentLayout(document: WorkflowDocument): WorkflowDocument {
  const ids = new Set(document.nodes.map((node) => node.node_id))
  const positions = prunePositions(document.layout.positions, ids)
  const pruned: WorkflowDocument = {
    ...document,
    layout: { ...document.layout, positions },
  }
  if (Object.keys(positions).length === 0 && ids.size > 0) return autoLayoutDocument(pruned)
  return pruned
}

/** 键序归一的序列化：脏标记比较只看内容，不看对象键的插入顺序。 */
export function canonicalDocumentJson(document: WorkflowDocument): string {
  return JSON.stringify(canonicalize(document))
}

function canonicalize(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalize)
  if (value !== null && typeof value === 'object') {
    const source = value as Record<string, unknown>
    const sorted: Record<string, unknown> = {}
    for (const key of Object.keys(source).sort()) {
      const item = source[key]
      if (item !== undefined) sorted[key] = canonicalize(item)
    }
    return sorted
  }
  return value
}

export function documentsEqual(left: WorkflowDocument | null, right: WorkflowDocument | null): boolean {
  if (left === null || right === null) return left === right
  return canonicalDocumentJson(left) === canonicalDocumentJson(right)
}

export function documentByteLength(document: WorkflowDocument): number {
  return new TextEncoder().encode(JSON.stringify(document)).length
}

function jsonByteLength(value: unknown): number {
  return new TextEncoder().encode(JSON.stringify(value) ?? '').length
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function startVariableErrors(nodes: readonly NodeDef[], errors: string[]): void {
  const starts = nodes.filter((node) => node.type === 'start')
  if (starts.length > 1) errors.push('start 节点至多一个')
  const names = new Set<string>()
  for (const start of starts) {
    const raw = start.config.variables
    if (raw === undefined) continue
    if (!Array.isArray(raw)) {
      errors.push(`start 节点 ${start.node_id} 的 variables 必须是数组`)
      continue
    }
    if (raw.length > MAX_START_VARIABLES) {
      errors.push(`start 变量不得超过 ${MAX_START_VARIABLES} 项（实际 ${raw.length}）`)
    }
    raw.forEach((item, index) => {
      const path = `start.variables[${index}]`
      if (!isRecord(item)) {
        errors.push(`${path} 必须是对象`)
        return
      }
      const name = item.name
      if (typeof name !== 'string' || !START_VARIABLE_NAME_PATTERN.test(name)) {
        errors.push(`${path}.name 不合法`)
        return
      }
      if (PROTOTYPE_SEGMENTS.has(name)) {
        errors.push(`${path}.name 不允许使用 ${name}`)
      }
      if (names.has(name)) errors.push(`start 变量名重复: ${name}`)
      names.add(name)
      const type = item.type ?? 'string'
      if (type !== 'string' && type !== 'number' && type !== 'boolean' && type !== 'json') {
        errors.push(`${path}.type 不合法: ${String(type)}`)
        return
      }
      const fallback = item.default
      if (fallback !== undefined && fallback !== null) {
        if (type === 'string' && typeof fallback !== 'string') errors.push(`变量 ${name} 的 default 必须是字符串`)
        if (type === 'number' && (typeof fallback !== 'number' || !Number.isFinite(fallback))) {
          errors.push(`变量 ${name} 的 default 必须是数字`)
        }
        if (type === 'boolean' && typeof fallback !== 'boolean') errors.push(`变量 ${name} 的 default 必须是布尔值`)
      }
    })
  }
}

function reachableFrom(start: string, edges: readonly EdgeDef[]): Set<string> {
  const adjacency = new Map<string, string[]>()
  for (const edge of edges) {
    const targets = adjacency.get(edge.source)
    if (targets === undefined) adjacency.set(edge.source, [edge.target])
    else targets.push(edge.target)
  }
  const seen = new Set([start])
  const stack = [start]
  while (stack.length > 0) {
    const node = stack.pop()
    if (node === undefined) break
    for (const child of adjacency.get(node) ?? []) {
      if (!seen.has(child)) {
        seen.add(child)
        stack.push(child)
      }
    }
  }
  return seen
}

/**
 * 与后端 `WorkflowDocument._document_is_valid` 同源的本地体检。
 *
 * 方向刻意与 codec 相反：这里允许本构建不认识的节点类型（只校验类型串形状），
 * 好让"服务端比前端新"时仍然能打开、浏览一份文档；能不能保存由 `validateGraph` 再拦。
 */
export function validateDocument(document: WorkflowDocument): string[] {
  const errors: string[] = []
  if (document.schema_version !== DOCUMENT_SCHEMA_VERSION) {
    return [`不支持的文档 schema_version: ${String(document.schema_version)}`]
  }
  if (!WORKFLOW_ID_PATTERN.test(document.lineage_id)) errors.push(`lineage_id 形状不合法: ${document.lineage_id}`)
  if (document.name.length < 1 || document.name.length > 64) errors.push('工作流名称长度须在 1-64 字符之间')
  if (document.description.length > 512) errors.push('工作流描述不得超过 512 字符')
  if (document.nodes.length < 1) errors.push('工作流至少需要一个节点')
  if (document.nodes.length > MAX_DOCUMENT_NODES) errors.push(`节点数不得超过 ${MAX_DOCUMENT_NODES}`)
  if (document.edges.length > MAX_DOCUMENT_EDGES) errors.push(`连线数不得超过 ${MAX_DOCUMENT_EDGES}`)

  const ids = document.nodes.map((node) => node.node_id)
  const known = new Set(ids)
  if (known.size !== ids.length) errors.push('节点 ID 重复')

  for (const node of document.nodes) {
    if (!DOCUMENT_NODE_ID_PATTERN.test(node.node_id)) errors.push(`节点 ID 非法: ${node.node_id}`)
    if (!/^[a-z][a-z0-9_]{1,31}$/.test(node.type)) errors.push(`节点类型非法: ${node.type}`)
    if (node.timeout_ms > node.sla_ms) errors.push(`节点 [${node.node_id}] timeout_ms 不得大于 sla_ms`)
    const size = jsonByteLength(node.config)
    if (size > MAX_NODE_CONFIG_BYTES)
      errors.push(`节点 ${node.node_id} 的 config 超过 ${MAX_NODE_CONFIG_BYTES} 字节（实际 ${size}）`)
  }

  for (const edge of document.edges) {
    if (!known.has(edge.source)) errors.push(`边的源节点不存在: ${edge.source}`)
    if (!known.has(edge.target)) errors.push(`边的目标节点不存在: ${edge.target}`)
    if (edge.source === edge.target) errors.push(`不允许自环: ${edge.source}`)
  }

  const ends = document.nodes.filter((node) => node.type === 'end')
  if (ends.length > 1) errors.push('end 节点至多一个')
  for (const node of document.nodes.filter((item) => item.type === 'start')) {
    if (document.edges.some((edge) => edge.target === node.node_id))
      errors.push(`start 节点 ${node.node_id} 不允许有入边`)
  }
  for (const node of ends) {
    if (document.edges.some((edge) => edge.source === node.node_id))
      errors.push(`end 节点 ${node.node_id} 不允许有出边`)
  }

  const start = document.nodes.find((node) => node.type === 'start')
  if (start !== undefined) {
    const roots = document.nodes.filter((node) => !document.edges.some((edge) => edge.target === node.node_id))
    if (roots.length !== 1 || roots[0]?.node_id !== start.node_id) {
      errors.push(
        `存在 start 时它必须是唯一根节点，当前根节点: ${roots
          .map((node) => node.node_id)
          .sort()
          .join('、')}`,
      )
    }
    const reachable = reachableFrom(start.node_id, document.edges)
    const unreachable = [...known].filter((id) => !reachable.has(id)).sort()
    if (unreachable.length > 0) errors.push(`这些节点从 ${start.node_id} 不可达: ${unreachable.join('、')}`)
  }

  startVariableErrors(document.nodes, errors)

  if (errors.length === 0) {
    const cycle = findCyclePath(ids, document.edges)
    if (cycle !== null) errors.push(`工作流图存在环: ${cycle.join(' → ')}`)
  }

  if (errors.length === 0) {
    const size = documentByteLength(document)
    if (size > MAX_DOCUMENT_BYTES) errors.push(`文档序列化后超过 ${MAX_DOCUMENT_BYTES} 字节（实际 ${size}）`)
  }
  return errors
}
