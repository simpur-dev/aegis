// Ported from Dify web/app/components/workflow/utils/clipboard.ts
// (tag 1.17.1, commit 8387590ace4a094de812b7847fc6a4c3a27cd52b).
// Copyright (c) LangGenius, Inc. Licensed under the modified Apache License 2.0.
// Changes: adapted to AEGIS; see THIRD_PARTY_NOTICES.md.
import {
  layerPositions,
  NODE_SPECS,
  ON_FAILURE_LABELS,
  SLA_MAX_MS,
  SLA_MIN_MS,
  type EdgeDef,
  type JsonValue,
  type NodeDef,
  type NodeType,
  type OnFailure,
  type Position,
} from '../graph'
import type { WorkflowDocument } from './document'

export const WORKFLOW_CLIPBOARD_KIND = 'aegis-workflow-clipboard'
export const WORKFLOW_CLIPBOARD_VERSION = '1'
/** 剪贴板里的文本也是外部输入：解析前先量字节，别让一个 200MB 的粘贴把主线程拖死。 */
export const MAX_CLIPBOARD_BYTES = 2 * 1024 * 1024

const PROTOTYPE_SEGMENTS = new Set(['__proto__', 'prototype', 'constructor'])

/**
 * 上游的节点是画布 Node（position 内嵌）；AEGIS 的 NodeDef 不含版式，
 * 所以粘贴单位是"定义 + 落点"这一对，落点单独走 layout。
 */
export interface ClipboardNode {
  node: Record<string, unknown>
  position: Position
}

export interface ClipboardPayload {
  nodes: ClipboardNode[]
  edges: EdgeDef[]
}

export type ClipboardErrorCode =
  | 'empty'
  | 'too-large'
  | 'invalid-json'
  | 'wrong-kind'
  | 'bad-payload'
  | 'no-valid-nodes'
  | 'read-failed'
  | 'write-failed'

/**
 * 上游对坏输入一律静默返回空结果，粘贴按钮会"什么也没发生"。
 * AEGIS 把每种坏输入折成可展示的 code，让调用方能说清是空剪贴板、格式不对，还是跨版本。
 */
export class ClipboardError extends Error {
  readonly code: ClipboardErrorCode

  constructor(code: ClipboardErrorCode, message: string) {
    super(message)
    this.name = 'ClipboardError'
    this.code = code
  }
}

export interface ClipboardParseResult {
  nodes: ClipboardNode[]
  edges: EdgeDef[]
  sourceVersion: string
  isVersionMismatch: boolean
}

const isPlainObject = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value)

const isSafeKey = (key: string): boolean => !PROTOTYPE_SEGMENTS.has(key)

/** 递归拷贝并丢弃原型段：JSON.parse 会把 `__proto__` 变成自有键，后续一旦被赋值就会污染原型。 */
function sanitizeIncomingValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sanitizeIncomingValue)
  if (!isPlainObject(value)) return value

  const copy: Record<string, unknown> = {}
  for (const [key, child] of Object.entries(value)) {
    if (!isSafeKey(key)) continue
    copy[key] = sanitizeIncomingValue(child)
  }
  return copy
}

function defaultWithoutIncoming(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sanitizeIncomingValue)
  if (!isPlainObject(value)) return value

  const copy: Record<string, unknown> = {}
  for (const [key, child] of Object.entries(value)) {
    if (!isSafeKey(key)) continue
    copy[key] = defaultWithoutIncoming(child)
  }
  return copy
}

/**
 * 按节点类型的默认配置净化剪贴板值：类型不符就回落到默认值，对象按键取并集。
 * 与上游一致；差异是并集里的非默认键也递归过滤原型段。
 */
export function sanitizeClipboardValueByDefault(defaultValue: unknown, incomingValue: unknown): unknown {
  if (defaultValue === undefined) return sanitizeIncomingValue(incomingValue)

  if (Array.isArray(defaultValue)) {
    return Array.isArray(incomingValue)
      ? incomingValue.map(sanitizeIncomingValue)
      : defaultWithoutIncoming(defaultValue)
  }

  if (isPlainObject(defaultValue)) {
    if (!isPlainObject(incomingValue)) return defaultWithoutIncoming(defaultValue)

    const merged: Record<string, unknown> = {}
    const keys = new Set([...Object.keys(defaultValue), ...Object.keys(incomingValue)])

    for (const key of keys) {
      if (!isSafeKey(key)) continue
      const hasDefault = Object.hasOwn(defaultValue, key)
      const hasIncoming = Object.hasOwn(incomingValue, key)

      if (hasDefault && hasIncoming) {
        merged[key] = sanitizeClipboardValueByDefault(defaultValue[key], incomingValue[key])
      } else if (hasIncoming) {
        merged[key] = sanitizeIncomingValue(incomingValue[key])
      } else {
        merged[key] = defaultWithoutIncoming(defaultValue[key])
      }
    }

    return merged
  }

  if (typeof defaultValue === 'number') {
    return typeof incomingValue === 'number' && Number.isFinite(incomingValue) ? incomingValue : defaultValue
  }

  return typeof incomingValue === typeof defaultValue ? sanitizeIncomingValue(incomingValue) : defaultValue
}

/** 兼容性判断只回答"这个值能不能直接用"，不做合并；默认值缺省视为兼容（版本演进）。 */
export function isClipboardValueCompatibleWithDefault(defaultValue: unknown, incomingValue: unknown): boolean {
  if (incomingValue === undefined) return true
  if (defaultValue === undefined) return true

  if (Array.isArray(defaultValue)) return Array.isArray(incomingValue)

  if (isPlainObject(defaultValue)) {
    if (!isPlainObject(incomingValue)) return false

    return Object.entries(defaultValue).every(([key, value]) => {
      if (!isSafeKey(key)) return true
      return isClipboardValueCompatibleWithDefault(value, incomingValue[key])
    })
  }

  if (typeof defaultValue === 'number') {
    return typeof incomingValue === 'number' && Number.isFinite(incomingValue)
  }

  return typeof incomingValue === typeof defaultValue
}

export function isClipboardNodeStructurallyValid(value: unknown): value is ClipboardNode {
  if (!isPlainObject(value)) return false

  const node = value.node
  if (!isPlainObject(node)) return false
  if (typeof node.node_id !== 'string' || node.node_id === '') return false
  if (typeof node.type !== 'string' || node.type === '') return false
  if (!isPlainObject(node.config)) return false

  const position = value.position
  if (!isPlainObject(position)) return false

  return Number.isFinite(position.x) && Number.isFinite(position.y)
}

export interface ClipboardEdgeInput {
  source: string
  target: string
  condition?: unknown
}

export function isClipboardEdgeStructurallyValid(value: unknown): value is ClipboardEdgeInput {
  if (!isPlainObject(value)) return false

  return typeof value.source === 'string' && typeof value.target === 'string'
}

/** 只保留 AEGIS EdgeDef 的三格；上游的 id/targetHandle 在这里没有对应字段。 */
function normalizeEdge(value: ClipboardEdgeInput): EdgeDef {
  return {
    source: value.source,
    target: value.target,
    condition: typeof value.condition === 'string' ? value.condition : '',
  }
}

function normalizeNode(value: ClipboardNode): ClipboardNode {
  return {
    node: sanitizeIncomingValue(value.node) as Record<string, unknown>,
    position: { x: value.position.x, y: value.position.y },
  }
}

/**
 * 解析剪贴板文本。
 *
 * 与上游的三处差异：
 * - 空文本、字节超限、JSON 坏、kind/version 形状不对都抛 `ClipboardError`（上游静默返回空）；
 * - 有效节点为 0 时也报错，否则"粘贴成功但什么也没出现"；
 * - 单条非法节点/边仍按上游做过滤，不因一条脏数据丢整次粘贴。
 */
export function parseWorkflowClipboardText(
  text: string,
  currentVersion: string = WORKFLOW_CLIPBOARD_VERSION,
): ClipboardParseResult {
  if (text === '') throw new ClipboardError('empty', '剪贴板里没有可粘贴的内容')

  const bytes = new TextEncoder().encode(text).length
  if (bytes > MAX_CLIPBOARD_BYTES) {
    throw new ClipboardError('too-large', `剪贴板内容 ${bytes} 字节，超过 ${MAX_CLIPBOARD_BYTES} 字节上限`)
  }

  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch {
    throw new ClipboardError('invalid-json', '剪贴板内容不是合法 JSON')
  }

  if (!isPlainObject(parsed)) throw new ClipboardError('bad-payload', '剪贴板载荷不是对象')
  if (parsed.kind !== WORKFLOW_CLIPBOARD_KIND) {
    throw new ClipboardError('wrong-kind', '剪贴板内容不是 AEGIS 工作流片段')
  }
  if (typeof parsed.version !== 'string') {
    throw new ClipboardError('bad-payload', '剪贴板载荷缺少版本号')
  }
  if (!Array.isArray(parsed.nodes) || !Array.isArray(parsed.edges)) {
    throw new ClipboardError('bad-payload', '剪贴板载荷的 nodes/edges 不是数组')
  }

  const nodes = parsed.nodes.filter(isClipboardNodeStructurallyValid).map(normalizeNode)
  if (nodes.length === 0) throw new ClipboardError('no-valid-nodes', '剪贴板片段里没有结构合法的节点')

  const edges = parsed.edges.filter(isClipboardEdgeStructurallyValid).map(normalizeEdge)

  return {
    nodes,
    edges,
    sourceVersion: parsed.version,
    isVersionMismatch: parsed.version !== currentVersion,
  }
}

/** 写出前先量字节：超限就报错，而不是写进一个浏览器会在读取时拒绝的巨型字符串。 */
export function stringifyWorkflowClipboardData(
  payload: ClipboardPayload,
  currentVersion: string = WORKFLOW_CLIPBOARD_VERSION,
): string {
  const text = JSON.stringify({
    kind: WORKFLOW_CLIPBOARD_KIND,
    version: currentVersion,
    nodes: payload.nodes,
    edges: payload.edges,
  })

  const bytes = new TextEncoder().encode(text).length
  if (bytes > MAX_CLIPBOARD_BYTES) {
    throw new ClipboardError('too-large', `要复制的内容 ${bytes} 字节，超过 ${MAX_CLIPBOARD_BYTES} 字节上限`)
  }

  return text
}

/** 兜底给调用方展示用；非剪贴板异常（navigator 拒绝）也折成可展示文案。 */
export function clipboardErrorMessage(error: unknown): string {
  if (error instanceof ClipboardError) return error.message
  if (error instanceof Error && error.message !== '') return error.message
  // 浏览器抛的 DOMException 在部分环境里不继承 Error，只带 message。
  if (typeof error === 'object' && error !== null && 'message' in error) {
    const message = (error as { message: unknown }).message
    if (typeof message === 'string' && message !== '') return message
  }
  return '剪贴板不可用'
}

/** 仅用于确认对象是否为空壳载荷（C3 建片段时判空）。 */
export function isClipboardPayloadEmpty(payload: ClipboardPayload): boolean {
  return payload.nodes.length === 0 && payload.edges.length === 0
}

/** 结构化克隆一份载荷，避免复制动作把画布对象引用写进剪贴板快照。 */
export function copyClipboardPayload(payload: ClipboardPayload): ClipboardPayload {
  return {
    nodes: payload.nodes.map(normalizeNode),
    edges: payload.edges.map((edge) => ({ ...edge })),
  }
}

/**
 * 从文档里切出所选节点的子图：节点按文档顺序，只保留两端都被选中的边。
 *
 * 位置必须从 layout 拿：`defToGraph` 的兜底分层坐标不在文档里，
 * 复制后再按兜底算一次会让粘贴出来的相对位置与屏幕上看到的不一致。
 */
export function extractWorkflowClipboardSubgraph(
  document: WorkflowDocument,
  nodeIds: Iterable<string>,
): ClipboardPayload {
  const wanted = new Set(nodeIds)
  const fallback = layerPositions(
    document.nodes.map((node) => node.node_id),
    document.edges,
  )
  const nodes: ClipboardNode[] = document.nodes
    .filter((node) => wanted.has(node.node_id))
    .map((node) => ({
      node: JSON.parse(JSON.stringify(node)) as Record<string, unknown>,
      position: { ...(document.layout.positions[node.node_id] ?? fallback[node.node_id] ?? { x: 0, y: 0 }) },
    }))
  const edges: EdgeDef[] = document.edges
    .filter((edge) => wanted.has(edge.source) && wanted.has(edge.target))
    .map((edge) => ({ ...edge }))
  return { nodes, edges }
}

/** 粘贴偏移 = 落点 − 片段左上角，让整块子图跟着光标走，而不是把左上角丢到光标外。 */
export function clipboardOffsetTo(nodes: readonly ClipboardNode[], drop: Position): Position {
  let minX = Infinity
  let minY = Infinity
  for (const item of nodes) {
    if (item.position.x < minX) minX = item.position.x
    if (item.position.y < minY) minY = item.position.y
  }
  if (!Number.isFinite(minX) || !Number.isFinite(minY)) return { x: drop.x, y: drop.y }
  return { x: drop.x - minX, y: drop.y - minY }
}

const isNodeType = (value: unknown): value is NodeType => typeof value === 'string' && Object.hasOwn(NODE_SPECS, value)

function finiteWithin(value: unknown, fallback: number, min: number, max: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? Math.min(max, Math.max(min, value)) : fallback
}

function onFailureOf(value: unknown, fallback: OnFailure): OnFailure {
  return typeof value === 'string' && Object.hasOwn(ON_FAILURE_LABELS, value) ? (value as OnFailure) : fallback
}

/**
 * 把剪贴板里的裸节点补成 NodeDef：类型不在注册表里返回 null（调用方整条丢弃）。
 *
 * 配置按默认值净化后并集：旧片段缺的新字段由默认值补齐，片段里多出来的键保留，
 * 但原型段会在 `sanitizeClipboardJson` 里被逐层丢掉。
 */
export function mergeClipboardNodeDef(record: Record<string, unknown>, base: NodeDef): NodeDef | null {
  if (!isNodeType(record.type)) return null

  const mergedConfig = sanitizeClipboardValueByDefault(base.config, record.config)
  const config = sanitizeClipboardJson(mergedConfig)
  const sla = finiteWithin(record.sla_ms, base.sla_ms, SLA_MIN_MS, SLA_MAX_MS)
  const timeout = Math.min(sla, finiteWithin(record.timeout_ms, base.timeout_ms, SLA_MIN_MS, SLA_MAX_MS))
  const retry = isPlainObject(record.retry) ? record.retry : {}

  return {
    node_id: base.node_id,
    type: record.type,
    name: typeof record.name === 'string' && record.name.trim() !== '' ? record.name : base.name,
    config: isPlainObject(config) ? (config as Record<string, JsonValue>) : { ...base.config },
    sla_ms: sla,
    timeout_ms: timeout,
    on_failure: onFailureOf(record.on_failure, base.on_failure),
    retry: {
      max_attempts: finiteWithin(retry.max_attempts, base.retry.max_attempts, 0, 10),
      backoff_ms: finiteWithin(retry.backoff_ms, base.retry.backoff_ms, 0, 60_000),
    },
  }
}

/**
 * `upstream` 是配置里唯一按 node_id 引用的字段。副本内部互相引用要跟着改号；
 * 指向选区之外的引用必须清掉——粘贴出来的节点没有那条线，后端定义校验会直接 400。
 */
function remapUpstreamConfig(config: Record<string, JsonValue>, idMapping: Readonly<Record<string, string>>): void {
  if (!Object.hasOwn(config, 'upstream')) return
  const upstream = config.upstream

  if (typeof upstream === 'string') {
    config.upstream = idMapping[upstream] ?? ''
    return
  }
  if (!Array.isArray(upstream)) return

  const remapped: JsonValue[] = []
  for (const entry of upstream) {
    if (typeof entry !== 'string') continue
    const mapped = idMapping[entry]
    if (mapped !== undefined && !remapped.includes(mapped)) remapped.push(mapped)
  }
  config.upstream = remapped
}

export interface ClipboardRemapOptions {
  /** 每次都返回未占用的新 id；同名类型连续调用必须给不同结果。 */
  allocateId: (type: NodeType) => string
  /** 该类型的空白定义（节点面板的默认配置），粘贴时作为净化基线。 */
  createDefault: (type: NodeType, nodeId: string) => NodeDef
  offset: Position
}

export interface ClipboardSubgraph {
  nodes: NodeDef[]
  edges: EdgeDef[]
  positions: Record<string, Position>
  /** 旧 node_id → 新 node_id；调用方据此更新本地选择。 */
  idMapping: Record<string, string>
}

/**
 * 给片段换号并落到目标画布：只保留两端都在片段内的边，
 * 边条件原样保留（分支语义跟着源节点走），同号自环直接丢弃。
 */
export function remapWorkflowClipboardSubgraph(
  payload: ClipboardPayload,
  options: ClipboardRemapOptions,
): ClipboardSubgraph {
  const idMapping: Record<string, string> = {}
  const nodes: NodeDef[] = []
  const positions: Record<string, Position> = {}

  for (const item of payload.nodes) {
    const sourceId = item.node.node_id
    if (typeof sourceId !== 'string' || sourceId === '') continue
    const type = item.node.type
    if (!isNodeType(type)) continue

    const nodeId = options.allocateId(type)
    const merged = mergeClipboardNodeDef(item.node, options.createDefault(type, nodeId))
    if (merged === null) continue

    idMapping[sourceId] = nodeId
    nodes.push(merged)
    positions[nodeId] = {
      x: item.position.x + options.offset.x,
      y: item.position.y + options.offset.y,
    }
  }

  const edges: EdgeDef[] = []
  for (const edge of payload.edges) {
    const source = idMapping[edge.source]
    const target = idMapping[edge.target]
    if (source === undefined || target === undefined || source === target) continue
    if (edges.some((row) => row.source === source && row.target === target && row.condition === edge.condition))
      continue
    edges.push({ source, target, condition: edge.condition })
  }

  for (const node of nodes) remapUpstreamConfig(node.config, idMapping)

  return { nodes, edges, positions, idMapping }
}

/** 供 C3 生成 JSON 值时复用：只允许 JsonValue 形状，原型段同样丢弃。 */
export function sanitizeClipboardJson(value: unknown): JsonValue {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return value
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (Array.isArray(value)) return value.map(sanitizeClipboardJson)

  if (isPlainObject(value)) {
    const copy: Record<string, JsonValue> = {}
    for (const [key, child] of Object.entries(value)) {
      if (!isSafeKey(key)) continue
      copy[key] = sanitizeClipboardJson(child)
    }
    return copy
  }

  return null
}
