/**
 * 节点参数的就地体检：把后端会在定义期拒绝的事，在面板上当场说出来。
 *
 * 为什么要在前端重算一遍：`validateGraph` 的产出是一张整图的错误清单，落在保存按钮旁边，
 * 人得先认出"哪一格"再滚回去改。这里按字段拆到每一格下面，改一格、看一眼那一格就消红。
 *
 * 判定口径与后端同源但不越权：
 * - 必填/未知参数/数值下限取自 `NODE_SPECS`（nodes.py 的镜像），不自己发明；
 * - `upstream` 必须是入边，镜像 `engine._validate_wired_upstreams`；
 * - Start 变量镜像 `documents.StartVariable`（名字形状、类型枚举、≤32 项、原型链名）；
 * - 算子与 agg 镜像 `nodes._compare` / `_threshold`，表外的值运行期必炸。
 * 真正的裁决仍在服务端——这里只是让"必然被拒"的那一下不必等一次网络往返。
 */
import { nodeSpec, type JsonValue, type NodeDef } from '@/utils/graph'
import { isSafeVariableKey } from '@/utils/graph/variable'

import { nodeMeta, type ConfigField } from './registry'

/** 一条待修：挂在哪个配置键上（`''` 表示整节点级）、给人读的一句话。 */
export interface NodeIssue {
  key: string
  message: string
}

/** 与后端 `documents.MAX_START_VARIABLES` 同一条口径。 */
export const MAX_START_VARIABLES = 32
const START_NAME = /^[A-Za-z_][A-Za-z0-9_]{0,31}$/
const START_TYPES = ['string', 'number', 'boolean', 'json']
const PROTOTYPE_SEGMENTS = new Set(['__proto__', 'prototype', 'constructor'])
/** `nodes._compare` 硬编码的算子集合。 */
export const COMPARE_OPS = ['>=', '>', '<=', '<', '==', '!=', 'in', 'not_in']
/** `nodes._threshold` 的聚合口径：max/sum 之外一律取末值。 */
export const THRESHOLD_AGGS = ['max', 'sum', 'last']

export interface ConfigIssueContext {
  /** 该节点的入边源（`inboundSources`）；未给时跳过连线校验，只查值本身。 */
  readonly inbound?: readonly string[]
}

function isBlank(value: JsonValue | undefined): boolean {
  return value === undefined || value === null || value === ''
}

function asRecord(value: JsonValue | undefined): Record<string, JsonValue> | null {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return null
  return value as Record<string, JsonValue>
}

function rows(value: JsonValue | undefined): Record<string, JsonValue>[] | null {
  if (!Array.isArray(value)) return null
  const list: Record<string, JsonValue>[] = []
  for (const item of value) {
    const row = asRecord(item)
    if (row === null) return null
    list.push(row)
  }
  return list
}

function labelOf(field: ConfigField): string {
  return field.label
}

function checkNumber(field: ConfigField, value: JsonValue, push: (message: string) => void): void {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    push(`「${labelOf(field)}」要填数字`)
    return
  }
  if (field.min !== undefined && value < field.min) push(`「${labelOf(field)}」不得小于 ${field.min}`)
  if (field.max !== undefined && value > field.max) push(`「${labelOf(field)}」不得超过 ${field.max}`)
}

function checkSelect(field: ConfigField, value: JsonValue, push: (message: string) => void): void {
  const allowed = (field.options ?? []).map((option) => option.value)
  if (typeof value !== 'string' || !allowed.includes(value)) {
    push(`「${labelOf(field)}」只能是 ${allowed.join(' / ')}`)
  }
}

function checkStringList(field: ConfigField, value: JsonValue, push: (message: string) => void): void {
  if (!Array.isArray(value) || value.some((item) => typeof item !== 'string' || item.trim() === '')) {
    push(`「${labelOf(field)}」每一项都要填文字`)
  }
}

/** 单值与列表两种写法都要查（`upstream` 在存量定义里两种都出现过，后端同一条口径）。 */
function checkNodeRef(
  field: ConfigField,
  value: JsonValue,
  nodeId: string,
  inbound: readonly string[] | undefined,
  push: (key: string, message: string) => void,
): void {
  const listed = field.kind === 'node_ref_list'
  const ids: string[] = []
  if (Array.isArray(value)) {
    for (const item of value) {
      if (typeof item === 'string' && item !== '') ids.push(item)
      else push(field.key, `「${labelOf(field)}」含空白项`)
    }
  } else if (value === null || typeof value !== 'string') {
    push(field.key, listed ? `「${labelOf(field)}」要是节点 ID 的列表` : `「${labelOf(field)}」要是节点 ID`)
  } else if (value !== '') ids.push(value)

  if (inbound === undefined) return
  for (const id of ids) {
    if (inbound.includes(id)) continue
    push(
      field.key,
      `「${labelOf(field)}」点了 ${id}，但画布上没有 ${id} → ${nodeId} 这条连线；` +
        `当前入边：${inbound.length > 0 ? inbound.join('、') : '无'}`,
    )
  }
}

function checkRuleRows(
  field: ConfigField,
  value: JsonValue,
  push: (key: string, message: string) => void,
): void {
  const listed = rows(value)
  if (listed === null) {
    push(field.key, `「${labelOf(field)}」要是规则列表`)
    return
  }
  listed.forEach((row, index) => {
    const at = `第 ${index + 1} 条`
    if (typeof row.when !== 'string' || row.when.trim() === '') push(field.key, `${at}规则缺少取值路径`)
    if (typeof row.op !== 'string' || !COMPARE_OPS.includes(row.op)) {
      push(field.key, `${at}规则的算子只能是 ${COMPARE_OPS.join(' / ')}`)
    }
    if (!('value' in row)) push(field.key, `${at}规则缺少比较值`)
    if (typeof row.then !== 'string' || row.then.trim() === '') push(field.key, `${at}规则没有写出分支名`)
  })
}

function checkConditionRows(
  field: ConfigField,
  value: JsonValue,
  push: (key: string, message: string) => void,
): void {
  const listed = rows(value)
  if (listed === null) {
    push(field.key, `「${labelOf(field)}」要是条件列表`)
    return
  }
  listed.forEach((row, index) => {
    const at = `第 ${index + 1} 条`
    if (typeof row.metric !== 'string' || row.metric.trim() === '') push(field.key, `${at}条件缺少指标名`)
    if (typeof row.op !== 'string' || !COMPARE_OPS.includes(row.op)) {
      push(field.key, `${at}条件的算子只能是 ${COMPARE_OPS.join(' / ')}`)
    }
    if (typeof row.threshold !== 'number' || !Number.isFinite(row.threshold)) {
      push(field.key, `${at}条件的阈值要填数字`)
    }
    if (row.agg !== undefined && (typeof row.agg !== 'string' || !THRESHOLD_AGGS.includes(row.agg))) {
      push(field.key, `${at}条件的聚合只能是 ${THRESHOLD_AGGS.join(' / ')}`)
    }
  })
}

function checkStartVariables(
  field: ConfigField,
  value: JsonValue,
  push: (key: string, message: string) => void,
): void {
  const listed = rows(value)
  if (listed === null) {
    push(field.key, '「输入变量」要是变量列表')
    return
  }
  if (listed.length > MAX_START_VARIABLES) {
    push(field.key, `「输入变量」不得超过 ${MAX_START_VARIABLES} 项，当前 ${listed.length} 项`)
  }
  const seen = new Set<string>()
  listed.forEach((row, index) => {
    const at = `第 ${index + 1} 个变量`
    const name = typeof row.name === 'string' ? row.name : ''
    if (name === '') {
      push(field.key, `${at}缺少变量名`)
    } else if (!START_NAME.test(name)) {
      push(field.key, `${at}「${name}」的名字不合法：字母或下划线开头，只含字母/数字/下划线，不超 32 字符`)
    } else if (PROTOTYPE_SEGMENTS.has(name)) {
      push(field.key, `${at}「${name}」是原型链上的名字，不能当变量名`)
    } else if (seen.has(name)) {
      push(field.key, `变量名「${name}」重复`)
    }
    seen.add(name)
    const type = row.type === undefined ? 'string' : row.type
    if (typeof type !== 'string' || !START_TYPES.includes(type)) {
      push(field.key, `${at}的类型只能是 ${START_TYPES.join(' / ')}`)
    } else {
      const mismatch = defaultMismatch(at, type, row.default)
      if (mismatch !== '') push(field.key, mismatch)
    }
  })
}

/** 后端 `StartVariable._default_matches_type` 的同一条判据：默认值与声明的类型不符就在面板上说。 */
function defaultMismatch(at: string, type: string, value: JsonValue | undefined): string {
  if (value === undefined || value === null) return ''
  if (type === 'string' && typeof value !== 'string') return `${at}的默认值要写成文字`
  if (type === 'number' && typeof value !== 'number') return `${at}的默认值要写成数字`
  if (type === 'boolean' && typeof value !== 'boolean') return `${at}的默认值要写成 true 或 false`
  return ''
}

/**
 * 单个节点的配置待修清单。
 *
 * 未注册类型返回空：检查器已经把它整体降级成只读，这时候再吐一串"未知参数"只会让人
 * 以为改错了东西。
 */
export function nodeConfigIssues(node: Pick<NodeDef, 'node_id' | 'type' | 'config'>, context: ConfigIssueContext = {}): NodeIssue[] {
  const spec = nodeSpec(node.type)
  const meta = nodeMeta(node.type)
  if (spec === null || meta === null) return []

  const issues: NodeIssue[] = []
  const seen = new Set<string>()
  const push = (key: string, message: string): void => {
    // 同一句话可能从两条路进来（后端声明的下限 + 面板声明的取值域），并排两条红字只让人觉得吵。
    const fingerprint = `${key}|${message}`
    if (seen.has(fingerprint)) return
    seen.add(fingerprint)
    issues.push({ key, message })
  }

  for (const key of spec.required_config) {
    if (isBlank(node.config[key])) {
      const label = meta.fields.find((field) => field.key === key)?.label ?? key
      push(key, `「${label}」是必填项，留空会在发布时被服务端拒收`)
    }
  }

  const allowed = new Set([...spec.required_config, ...spec.optional_config])
  for (const key of Object.keys(node.config)) {
    if (!allowed.has(key)) push(key, `参数「${key}」不在 ${node.type} 的配置白名单里`)
  }

  for (const [key, floor] of Object.entries(spec.min_config)) {
    const value = node.config[key]
    if (typeof value === 'number' && value < floor) {
      const label = meta.fields.find((field) => field.key === key)?.label ?? key
      push(key, `「${label}」不得小于 ${floor}`)
    }
  }

  for (const field of meta.fields) {
    const value = node.config[field.key]
    if (value === undefined || value === null) continue
    switch (field.kind) {
      case 'number':
        checkNumber(field, value, (message) => push(field.key, message))
        break
      case 'select':
        checkSelect(field, value, (message) => push(field.key, message))
        break
      case 'text':
      case 'template':
        if (typeof value !== 'string') push(field.key, `「${labelOf(field)}」要是文字`)
        break
      case 'string_list':
        checkStringList(field, value, (message) => push(field.key, message))
        break
      case 'node_ref':
      case 'node_ref_list':
        checkNodeRef(field, value, node.node_id, context.inbound, push)
        break
      case 'rules':
        checkRuleRows(field, value, push)
        break
      case 'conditions':
        checkConditionRows(field, value, push)
        break
      case 'start_variables':
        checkStartVariables(field, value, push)
        break
      case 'json':
        break
    }
  }
  return issues
}

/** 一格可能同时中好几条（必填 + 类型），并进同一行显示，免得字段下方堆成一叠红字。 */
export function issuesForField(issues: readonly NodeIssue[], key: string): string {
  return issues
    .filter((issue) => issue.key === key)
    .map((issue) => issue.message)
    .join('；')
}

/**
 * 配置文本里的变量引用（`{{#节点.路径#}}`）逐条挑出来。
 *
 * 只做形状与合法性判定，不求值：求值在服务端（ADR-0009 §9）。这里要回答的是
 * "这串看起来像引用的东西，人是不是写错了"——写错的引用发布时不会报错，
 * 只会在实例日志里变成空值，那是最难查的一类事故。
 */
export interface VariableReference {
  raw: string
  nodeId: string
  path: string
  reason: string
}

const REFERENCE = /\{\{#([^#]*)#\}\}/g

export function variableReferences(config: Readonly<Record<string, unknown>>): string[] {
  const found: string[] = []
  for (const value of Object.values(config)) {
    if (typeof value !== 'string') continue
    for (const match of value.matchAll(REFERENCE)) {
      if (match[0] !== undefined && !found.includes(match[0])) found.push(match[0])
    }
  }
  return found
}

/**
 * 每条引用的判据：节点得是祖先、每一段得是安全键、路径深度 ≤8（ADR-0009 §10 的容量口径）。
 * `ancestors` 未给时跳过祖先判定（画布还没连线的草稿阶段）。
 */
export function inspectVariableReferences(
  config: Readonly<Record<string, JsonValue>>,
  options: { readonly ancestors?: readonly string[] } = {},
): VariableReference[] {
  const problems: VariableReference[] = []
  for (const raw of variableReferences(config)) {
    const segments = raw.replace(/^\{\{#|#\}\}$/g, '').split('.')
    const nodeId = segments[0] ?? ''
    const path = segments.slice(1).join('.')
    const bad = (reason: string): void => {
      problems.push({ raw, nodeId, path, reason })
    }
    if (segments.length < 2) bad('引用要写成 {{#节点.输出键#}}，只点名节点取不到值')
    else if (nodeId === '') bad('引用缺少节点 ID')
    else if (!isSafeVariableKey(nodeId) && !PROTOTYPE_SEGMENTS.has(nodeId)) bad(`引用里的节点 ID「${nodeId}」形状不合法`)
    else if (segments.slice(1).some((segment) => PROTOTYPE_SEGMENTS.has(segment))) {
      bad('引用路径里出现了原型链名字')
    } else if (segments.slice(1).some((segment) => !isSafeVariableKey(segment))) {
      bad(`引用路径「${path}」里有不合法的段`)
    } else if (segments.length > 8) bad('引用路径最多 8 段（节点 ID 也算一段）')

    else if (options.ancestors !== undefined && !options.ancestors.includes(nodeId)) {
      bad(`引用的节点 ${nodeId} 不是本节点的上游（画布上没有通向它的路径）`)
    }
  }
  return problems
}

/** 节点 config 里所有字符串格子的引用体检（规则/条件这类结构里不解析变量，与后端同一口径）。 */
export function referenceIssues(
  node: Pick<NodeDef, 'type' | 'config'>,
  options: { readonly ancestors?: readonly string[] } = {},
): NodeIssue[] {
  return inspectVariableReferences(node.config, options).map((problem) => ({
    key: configKeyHolding(node.config, problem.raw),
    message: `变量 ${problem.raw} ${problem.reason}`,
  }))
}

/** 引用可能出现在任意文本格里：找出去重后的第一处，好把红字挂到对的那一格。 */
function configKeyHolding(config: Readonly<Record<string, JsonValue>>, raw: string): string {
  for (const [key, value] of Object.entries(config)) {
    if (typeof value === 'string' && value.includes(raw)) return key
  }
  return ''
}
