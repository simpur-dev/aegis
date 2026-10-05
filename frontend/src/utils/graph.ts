/**
 * 工作流图 ↔ Vue Flow 视图的纯映射层。
 *
 * 约束：本文件零依赖（不 import Vue / axios / CSS），因此可离线单测，
 * 也是画布唯一的"后端契约镜像"。类型全程保持后端 snake_case，
 * 避免"视图 camelCase / 契约 snake_case"双向映射带来的字段漂移。
 *
 * 校验规则与后端一一对应（提交前拦截，不替代服务端校验）：
 * - 图不变量：model.py:96-114（重复 ID / 悬空边 / 自环 / 环路）
 * - SLA 上界：model.py:61-66（timeout_ms ≤ sla_ms）
 * - 节点配置：nodes.py:105-118（必填缺失 / 未知参数 / 下限）
 * - 字段取值域：workflow_api.py:22-86（NodeInput / EdgeInput / DefinitionInput）
 * 同名版本冲突、节点类型是否在服务端注册、并发下的定义变更仍只有后端能裁决。
 */

export type NodeCategory = 'io' | 'logic' | 'intelligence' | 'human'

/** 后端 nodes.py:351-366 已注册的 16 类节点，新增类型必须同步 NODE_SPECS 与节点面板。 */
export type NodeType =
  | 'api_call'
  | 'branch'
  | 'data_fetch'
  | 'delay'
  | 'degrade_to_rule'
  | 'device_control'
  | 'feedback_collect'
  | 'hazard_identify'
  | 'human_review'
  | 'join'
  | 'notify'
  | 'risk_assess'
  | 'situation_simulate'
  | 'threshold'
  | 'warning_generate'
  | 'warning_publish'

/** model.py:21-32 NodeState 全集（含 BYPASSED，与 SKIPPED 语义不可混用）。 */
export type NodeState =
  | 'pending'
  | 'ready'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'skipped'
  | 'bypassed'
  | 'degraded'
  | 'awaiting_human'
  | 'cancelled'
  | 'timeout'

/** model.py:154-159 InstanceStatus。 */
export type InstanceStatus = 'running' | 'waiting' | 'succeeded' | 'failed' | 'aborted'

/** model.py:58 on_failure 取值，语义见 engine.py:439-470。 */
export type OnFailure = 'retry' | 'degrade' | 'escalate' | 'skip' | 'abort'

export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue }

export interface RetryPolicy {
  /** 重试次数，不含首次执行；0 表示不重试（model.py:40-46）。 */
  max_attempts: number
  backoff_ms: number
}

export interface NodeDef {
  node_id: string
  type: NodeType
  name: string
  config: Record<string, JsonValue>
  sla_ms: number
  timeout_ms: number
  on_failure: OnFailure
  retry: RetryPolicy
}

export interface EdgeDef {
  source: string
  target: string
  condition: string
}

export interface WorkflowDef {
  workflow_id: string
  name: string
  description: string
  version: number
  nodes: NodeDef[]
  edges: EdgeDef[]
  created_by: string
  status: 'active' | 'archived'
}

export interface Position {
  x: number
  y: number
}

/** 画布节点携带完整定义（往返无损）+ 运行态快照（无实例时为 null）。 */
export interface FlowNodeData {
  def: NodeDef
  category: NodeCategory
  description: string
  state: NodeState | null
  attempts: number
  note: string
}

export interface FlowNode {
  id: string
  type: NodeCategory
  position: Position
  /** 选中态由模型给出（store.selectedNodeId），不依赖 Vue Flow 的内部点选：
   *  视图每次都由 defToGraph 重建，内部点选会在下一次轮询时被整体替换掉。 */
  selected: boolean
  data: FlowNodeData
}

export interface FlowEdge {
  id: string
  source: string
  target: string
  label: string
  data: EdgeDef
}

export interface GraphView {
  nodes: FlowNode[]
  edges: FlowEdge[]
}

export interface NodeSpec {
  category: NodeCategory
  required_config: readonly string[]
  optional_config: readonly string[]
  min_config: Readonly<Record<string, number>>
}

/** 后端注册表的前端镜像；类别 + 配置白名单的唯一真源。 */
export const NODE_SPECS: Record<NodeType, NodeSpec> = {
  api_call: { category: 'io', required_config: ['method', 'url'], optional_config: ['body'], min_config: {} },
  branch: { category: 'logic', required_config: ['rules'], optional_config: ['upstream', 'default'], min_config: {} },
  data_fetch: { category: 'io', required_config: [], optional_config: ['region_code', 'metric', 'limit'], min_config: {} },
  delay: { category: 'logic', required_config: [], optional_config: ['seconds'], min_config: {} },
  degrade_to_rule: {
    category: 'intelligence',
    required_config: [],
    optional_config: ['risk_level'],
    min_config: { risk_level: 1 },
  },
  device_control: {
    category: 'io',
    required_config: ['device', 'action', 'command_url'],
    optional_config: [],
    min_config: {},
  },
  feedback_collect: { category: 'logic', required_config: [], optional_config: ['wait_seconds'], min_config: {} },
  hazard_identify: { category: 'intelligence', required_config: [], optional_config: ['extra'], min_config: {} },
  human_review: { category: 'human', required_config: [], optional_config: ['prompt', 'options'], min_config: {} },
  join: { category: 'logic', required_config: ['upstream'], optional_config: [], min_config: {} },
  notify: { category: 'io', required_config: ['text'], optional_config: ['level'], min_config: {} },
  risk_assess: { category: 'intelligence', required_config: [], optional_config: ['upstream', 'extra'], min_config: {} },
  situation_simulate: { category: 'intelligence', required_config: [], optional_config: ['extra', 'upstream'], min_config: {} },
  threshold: { category: 'logic', required_config: ['conditions'], optional_config: ['upstream', 'mode'], min_config: {} },
  warning_generate: { category: 'intelligence', required_config: [], optional_config: ['extra'], min_config: {} },
  warning_publish: { category: 'intelligence', required_config: [], optional_config: ['channels'], min_config: {} },
}

export const NODE_TYPES: NodeType[] = Object.keys(NODE_SPECS) as NodeType[]

/** 11 个节点状态的画布样式；Record<NodeState, …> 保证后端新增状态时编译期即报错。 */
export interface NodeStateStyle {
  label: string
  color: string
  /** 追加到节点根元素的类名，供 NodeShell 着色。 */
  className: string
}

export const NODE_STATE_STYLES: Record<NodeState, NodeStateStyle> = {
  pending: { label: '待执行', color: '#8c8c8c', className: 'is-pending' },
  ready: { label: '就绪', color: '#1677ff', className: 'is-ready' },
  running: { label: '执行中', color: '#13c2c2', className: 'is-running' },
  succeeded: { label: '成功', color: '#52c41a', className: 'is-succeeded' },
  failed: { label: '失败', color: '#cf1322', className: 'is-failed' },
  skipped: { label: '分支跳过', color: '#bfbfbf', className: 'is-skipped' },
  bypassed: { label: '人工旁路', color: '#faad14', className: 'is-bypassed' },
  degraded: { label: '已降级', color: '#fa8c16', className: 'is-degraded' },
  awaiting_human: { label: '待人工核签', color: '#722ed1', className: 'is-awaiting-human' },
  cancelled: { label: '已取消', color: '#595959', className: 'is-cancelled' },
  timeout: { label: '超时', color: '#eb2f96', className: 'is-timeout' },
}

/** 后端若新增状态而前端未映射时的兜底：画布不得因契约漂移而崩。 */
const UNMAPPED_STATE: NodeStateStyle = { label: '未知状态', color: '#434343', className: 'is-unmapped' }

export const INSTANCE_STATUS_LABELS: Record<InstanceStatus, string> = {
  running: '执行中',
  waiting: '等待人工',
  succeeded: '已完成',
  failed: '已失败',
  aborted: '已中止',
}

/** 实例状态来自 HTTP 报文（string），这里做运行时收窄，未知值不 crash 画布。 */
export function isInstanceStatus(raw: string): raw is InstanceStatus {
  return raw in INSTANCE_STATUS_LABELS
}

/**
 * 人工核签的决策值 → 值班员读的中文。
 *
 * 候选值由定义里的 options 决定（templates.py:309 是 approve/adjust/reject），
 * 按钮与节点卡共用这一份映射：此前按钮只翻译 approve/reject，中间那颗永远是没人
 * 看得懂的 `adjust`；节点卡更直接把枚举裸值摆出来（"候选决策：approve / adjust / reject"）。
 */
export const DECISION_LABELS: Record<string, string> = {
  approve: '核签通过',
  adjust: '核签更正',
  reject: '核签退回',
}

/** 按钮与留痕用：中文 + 原值后缀，命令与回执里的枚举对得上。表外的值原样显示。 */
export function decisionLabel(value: string): string {
  const label = DECISION_LABELS[value]
  return label === undefined ? value : `${label}（${value}）`
}

/** 节点卡这类窄位置用：只给中文。表外的值原样显示。 */
export function decisionText(value: string): string {
  return DECISION_LABELS[value] ?? value
}

/** WorkflowDef.status 取值（model.py:94）。 */
export const DEFINITION_STATUS_LABELS: Record<'active' | 'archived', string> = {
  active: '启用中',
  archived: '已归档',
}

export function definitionStatusLabel(status: string): string {
  return (DEFINITION_STATUS_LABELS as Record<string, string>)[status] ?? status
}

export const ON_FAILURE_LABELS: Record<OnFailure, string> = {
  retry: '重试后失败',
  degrade: '降级继续',
  escalate: '转人工接管',
  skip: '跳过本节点',
  abort: '中止实例',
}

/**
 * 与后端一致的取值域（写接口 `workflow_api.py` 的 DefinitionInput/NodeInput 与 `model.py` 的 RetryPolicy）。
 *
 * 这些数是抄来的，抄错的症状不是报错而是"来回"：前端放行、后端 422，
 * 或前端把合法值夹住、用户以为界面坏了。所以 `graphLimitsContract.spec.ts`
 * 直接读后端源码逐个比对——改后端不改这里就是红灯。
 */
export const NODE_ID_PATTERN = /^[a-z][a-z0-9_-]{0,31}$/
export const NODE_TYPE_PATTERN = /^[a-z][a-z0-9_]{1,31}$/
export const MAX_NODES = 64
export const MAX_EDGES = 256
export const SLA_MIN_MS = 100
export const SLA_MAX_MS = 3_600_000
export const MAX_ATTEMPTS = 5
export const MAX_BACKOFF_MS = 60_000
export const MIN_DEFINITION_NAME_CHARS = 2
export const MAX_DEFINITION_NAME_CHARS = 64
export const MAX_DESCRIPTION_CHARS = 512
export const MAX_NODE_NAME_CHARS = 64
/** 自定义决策值的上界（DecisionInput.choice：min 1 / max 32）。 */
export const MAX_CHOICE_CHARS = 32
/** 核签意见的上界（DecisionInput.comment max_length）。 */
export const MAX_DECISION_COMMENT_CHARS = 512
/** 签字人的上界（DecisionInput.by max_length）。平台没有登录态，这一格由签字的人自己填。 */
export const MAX_SIGNER_CHARS = 64

const LAYER_GAP_X = 280
const NODE_GAP_Y = 132

export class GraphValidationError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'GraphValidationError'
  }
}

export function nodeSpec(type: string): NodeSpec | null {
  return (NODE_SPECS as Record<string, NodeSpec | undefined>)[type] ?? null
}

/**
 * 类型 → 画布卡片类别。未注册类型回落到 io：后端先行扩展时画布不整页崩，
 * 真正的拒绝由 validateGraph 报"未注册的节点类型"（镜像 nodes.py:133-137 require()）。
 */
export function categoryOfNode(type: string): NodeCategory {
  return nodeSpec(type)?.category ?? 'io'
}

export function stateStyle(state: string): NodeStateStyle {
  return (NODE_STATE_STYLES as Record<string, NodeStateStyle | undefined>)[state] ?? UNMAPPED_STATE
}

export function isTerminalNodeState(state: NodeState): boolean {
  // model.py:35-37 TERMINAL_STATES：timeout / awaiting_human 不在终态集合内。
  return (
    state === 'succeeded' ||
    state === 'failed' ||
    state === 'skipped' ||
    state === 'bypassed' ||
    state === 'degraded' ||
    state === 'cancelled'
  )
}

/** engine.py:539-540：仅未开始执行的节点可改参。 */
export function canPatchRuntimeConfig(state: NodeState | null): boolean {
  return state === 'pending' || state === 'ready'
}

/** engine.py:582-583：等待中/未执行的节点可人工旁路；已成功的不可。 */
export function canBypass(state: NodeState | null): boolean {
  return state === 'pending' || state === 'ready' || state === 'awaiting_human'
}

/** engine.py:515-516：仅 awaiting_human 可提交决策（无实例时当然不可）。 */
export function canDecide(state: NodeState | null): boolean {
  return state === 'awaiting_human'
}

export function isInstanceStateTerminal(status: InstanceStatus): boolean {
  // model.py:177 TERMINAL：终态实例不再轮询、不再接受运行中操作。
  return status === 'succeeded' || status === 'failed' || status === 'aborted'
}

/** Kahn 分层，语义镜像 model.py:215-237 topological_levels（每层按 ID 排序）。 */
export function layoutLayers(nodeIds: readonly string[], edges: readonly EdgeDef[]): string[][] {
  const indegree = new Map<string, number>(nodeIds.map((id) => [id, 0]))
  const adjacency = new Map<string, string[]>(nodeIds.map((id) => [id, []]))
  for (const edge of edges) {
    const targets = adjacency.get(edge.source)
    if (targets !== undefined) targets.push(edge.target)
    const degree = indegree.get(edge.target)
    if (degree !== undefined) indegree.set(edge.target, degree + 1)
  }

  const levels: string[][] = []
  let frontier = nodeIds.filter((id) => (indegree.get(id) ?? 0) === 0).sort()
  let seen = 0
  while (frontier.length > 0) {
    levels.push(frontier)
    seen += frontier.length
    const next: string[] = []
    for (const node of frontier) {
      for (const child of adjacency.get(node) ?? []) {
        const degree = (indegree.get(child) ?? 0) - 1
        indegree.set(child, degree)
        if (degree === 0) next.push(child)
      }
    }
    frontier = next.sort()
  }
  if (seen !== nodeIds.length) {
    throw new GraphValidationError('工作流图存在环: 无法拓扑分层')
  }
  return levels
}

/** 分层自动布局坐标：同一层纵向居中排布，层间横向推进。 */
export function layerPositions(nodeIds: readonly string[], edges: readonly EdgeDef[]): Record<string, Position> {
  const positions: Record<string, Position> = {}
  layoutLayers(nodeIds, edges).forEach((layer, index) => {
    layer.forEach((id, rowIndex) => {
      positions[id] = { x: index * LAYER_GAP_X, y: (rowIndex - (layer.length - 1) / 2) * NODE_GAP_Y }
    })
  })
  return positions
}

export function edgeId(index: number, edge: EdgeDef): string {
  return `${index}|${edge.source}|${edge.target}|${edge.condition}`
}

export interface DefToGraphOptions {
  positions?: Readonly<Record<string, Position>>
  states?: Readonly<Record<string, { state: NodeState; attempts: number; note: string }>>
  descriptions?: Readonly<Record<string, string>>
  selectedNodeId?: string | null
}

export function defToGraph(def: WorkflowDef, options: DefToGraphOptions = {}): GraphView {
  const fallback = layerPositions(
    def.nodes.map((node) => node.node_id),
    def.edges,
  )
  const nodes: FlowNode[] = def.nodes.map((node) => {
    const category = categoryOfNode(node.type)
    const run = options.states?.[node.node_id]
    return {
      id: node.node_id,
      type: category,
      position: options.positions?.[node.node_id] ?? fallback[node.node_id] ?? { x: 0, y: 0 },
      selected: node.node_id === options.selectedNodeId,
      data: {
        def: node,
        category,
        description: options.descriptions?.[node.type] ?? '',
        state: run?.state ?? null,
        attempts: run?.attempts ?? 0,
        note: run?.note ?? '',
      },
    }
  })
  const edges: FlowEdge[] = def.edges.map((edge, index) => ({
    id: edgeId(index, edge),
    source: edge.source,
    target: edge.target,
    label: edge.condition,
    data: edge,
  }))
  return { nodes, edges }
}

/**
 * 逆映射：视图 → 可提交后端的定义。身份字段取自 base，positions 不上线（后端 extra=forbid）。
 *
 * 节点名在这里裁一次首尾空格：后端也裁（`workflow_api.py` 的 `_TrimsText`），
 * 但只裁服务端的话画布上还留着空格、存完重开才变——"保存改了我的名字"看着像 bug。
 * 在这里裁，提交的、留在 store 的、画布显示的是同一份。
 */
export function graphToDef(view: GraphView, base: WorkflowDef): WorkflowDef {
  return {
    ...base,
    nodes: view.nodes.map((node) => ({ ...node.data.def, name: node.data.def.name.trim() })),
    edges: view.edges.map((edge) => edge.data),
  }
}

export function findNode(def: WorkflowDef, nodeId: string): NodeDef | null {
  return def.nodes.find((node) => node.node_id === nodeId) ?? null
}

/** 某节点的入边源（去重、按连线顺序）：join/branch 的 upstream 配置需与真实连线一致。 */
export function inboundSources(def: WorkflowDef, nodeId: string): string[] {
  const sources: string[] = []
  for (const edge of def.edges) {
    if (edge.target === nodeId && !sources.includes(edge.source)) sources.push(edge.source)
  }
  return sources
}

/** 生成与既有 ID 不冲突的节点 ID（保持后端 ^[a-z][a-z0-9_-]{0,31}$ 取值域）。 */
export function nextAvailableId(taken: Iterable<string>, type: string): string {
  const used = new Set(taken)
  const prefix = type.slice(0, 20)
  for (let index = 1; index <= 9_999; index += 1) {
    const candidate = `${prefix}_${index}`
    if (!used.has(candidate)) return candidate
  }
  throw new GraphValidationError('节点 ID 已耗尽，请清理画布')
}

export function nextNodeId(def: WorkflowDef, type: string): string {
  return nextAvailableId(
    def.nodes.map((node) => node.node_id),
    type,
  )
}

/** 节点卡的实际尺寸（`.wf-node` 宽度钉死 208px，高度按最长的摘要条估）。 */
export const NODE_FOOTPRINT = { width: 208, height: 120 }

/** 画布默认缩放。模板里的 `:default-viewport` 与快捷键 `0`（回到默认）共用这一个值。 */
export const CANVAS_DEFAULT_ZOOM = 0.9

/** 画布快捷键只管"看哪儿"这四件；其余按键一律不接（Tab 那套装焦点围栏在用）。 */
export type CanvasShortcut = 'fit' | 'reset' | 'zoom-in' | 'zoom-out'

export function canvasShortcut(key: string, heldModifier: boolean): CanvasShortcut | null {
  if (heldModifier) return null
  if (key === '1') return 'fit'
  if (key === '0') return 'reset'
  if (key === '+' || key === '=') return 'zoom-in'
  if (key === '-' || key === '_') return 'zoom-out'
  return null
}

/** 画布可视区在 flow 坐标系里的矩形（flow 单位与 zoom 无关：卡片宽恒为 208 flow 单位）。 */
export interface FlowBox {
  x: number
  y: number
  width: number
  height: number
}

/**
 * 落点让位：拖节点进画布时，卡片既要落在可视区里，又不许压住已有节点和不透明浮层。
 *
 * 真机量过三处缺陷：
 * - 往画布同一处连拖两个节点，第二张压住第一张 **389×234px**——落点原样取光标位置，
 *   而节点卡有 208px 宽。宁可远一点，也不许叠着（叠着之后要一颗颗拖开，比多走几步贵）。
 * - 只躲压叠不躲可视区时，第二张落到屏幕 y=704..957，而画布下沿是 880——不压叠但整张垂到
 *   画布外面，看不见等同于没建。所以让位方向必须在可视框里挑。
 * - 收进框内之后又压在小地图那块不透明卡片下（变异复测 171×102px）——"落下去没反应"的错觉。
 */
export function freeDropPosition(
  taken: Position[],
  wanted: Position,
  gap = 24,
  bounds: FlowBox | null = null,
  avoid: FlowBox[] = [],
): Position {
  const stepX = NODE_FOOTPRINT.width + gap
  const stepY = NODE_FOOTPRINT.height + gap
  const collides = (p: Position): boolean =>
    taken.some((n) => Math.abs(n.x - p.x) < stepX && Math.abs(n.y - p.y) < stepY)
  /* 卡片以落点为左上角，光标停在画布下沿时卡片会整张伸到框外，所以先把落点收进框内。 */
  const inside = (p: Position): Position => {
    if (bounds === null) return { x: p.x, y: p.y }
    const max = {
      x: bounds.x + Math.max(0, bounds.width - NODE_FOOTPRINT.width),
      y: bounds.y + Math.max(0, bounds.height - NODE_FOOTPRINT.height),
    }
    return { x: Math.min(Math.max(p.x, bounds.x), max.x), y: Math.min(Math.max(p.y, bounds.y), max.y) }
  }
  const fits = (p: Position): boolean =>
    bounds === null || (inside(p).x === p.x && inside(p).y === p.y)
  const covered = (p: Position): boolean =>
    avoid.some(
      (b) =>
        p.x < b.x + b.width &&
        p.x + NODE_FOOTPRINT.width > b.x &&
        p.y < b.y + b.height &&
        p.y + NODE_FOOTPRINT.height > b.y,
    )
  /* start 已被 inside 收进框内，只有"让位之后"才可能出框。 */
  const start = inside(wanted)
  if (!collides(start) && !covered(start)) return start
  /* 让位方向要挑"往右下"优先：真机第一版按 -ring 先试，结果把第二张推到画布左边外面
     （屏幕 x=9 而画布左边缘是 240），不压叠但看不见了——同样是要修的缺陷。 */
  const offsets: { dx: number; dy: number }[] = []
  for (let ring = 1; ring <= 8; ring += 1) {
    for (let dx = -ring; dx <= ring; dx += 1) {
      for (let dy = -ring; dy <= ring; dy += 1) {
        if (Math.max(Math.abs(dx), Math.abs(dy)) !== ring) continue
        offsets.push({ dx, dy })
      }
    }
  }
  const score = (o: { dx: number; dy: number }): number =>
    (o.dx < 0 ? 100 : 0) + (o.dy < 0 ? 100 : 0) + Math.max(Math.abs(o.dx), Math.abs(o.dy))
  offsets.sort((a, b) => score(a) - score(b))
  const around = (o: { dx: number; dy: number }): Position => ({
    x: start.x + o.dx * stepX,
    y: start.y + o.dy * stepY,
  })
  const candidates = offsets.map(around)
  const ideal = candidates.find((p) => !collides(p) && fits(p) && !covered(p))
  if (ideal !== undefined) return ideal
  /* 视口很小（例如被 init-fit 顶到 maxZoom）时框内可能真没空位，取舍按"看得见"优先：
     先要框内且不压叠（被浮层盖住还能平移画布找回来），再退一步只保不压叠。 */
  const visible = candidates.find((p) => !collides(p) && fits(p))
  if (visible !== undefined) return visible
  const crowded = candidates.find((p) => !collides(p))
  if (crowded !== undefined) return crowded
  return inside({ x: start.x + stepX * 9, y: start.y })
}

/** 节点补丁：retry 允许只给单个字段（其余沿用原值），node_id 不在可改集合内。 */
export type NodePatch = Omit<Partial<NodeDef>, 'retry' | 'node_id'> & { retry?: Partial<RetryPolicy> }

export function patchNode(def: WorkflowDef, nodeId: string, patch: NodePatch): WorkflowDef {
  const nodes = def.nodes.map((node) => {
    if (node.node_id !== nodeId) return node
    const { retry, ...rest } = patch
    delete (rest as { node_id?: string }).node_id
    const merged: NodeDef = { ...node, ...rest }
    merged.retry = { ...node.retry, ...(retry ?? {}) }
    return merged
  })
  // node_id 恒定（改了等于换节点），因此连线端点无需随之重写。
  return { ...def, nodes }
}

export function removeNodes(def: WorkflowDef, nodeIds: Iterable<string>): WorkflowDef {
  const doomed = new Set(nodeIds)
  return {
    ...def,
    nodes: def.nodes.filter((node) => !doomed.has(node.node_id)),
    edges: def.edges.filter((edge) => !doomed.has(edge.source) && !doomed.has(edge.target)),
  }
}

export function removeEdge(def: WorkflowDef, id: string): WorkflowDef {
  return { ...def, edges: def.edges.filter((edge, index) => edgeId(index, edge) !== id) }
}

/** 同一对端点 + 同一条件视为重复，画布直接丢弃（后端不去重，重复边会污染拓扑）。 */
export function upsertEdge(def: WorkflowDef, source: string, target: string, condition = ''): WorkflowDef {
  const duplicated = def.edges.some(
    (edge) => edge.source === source && edge.target === target && edge.condition === condition,
  )
  if (duplicated) return def
  return { ...def, edges: [...def.edges, { source, target, condition }] }
}

export function setEdgeCondition(def: WorkflowDef, id: string, condition: string): WorkflowDef {
  const normalized = condition.trim().toLowerCase()
  return {
    ...def,
    edges: def.edges.map((edge, index) => (edgeId(index, edge) === id ? { ...edge, condition: normalized } : edge)),
  }
}

function isJsonValue(value: unknown): value is JsonValue {
  if (value === null) return true
  const kind = typeof value
  if (kind === 'string' || kind === 'number' || kind === 'boolean') return true
  if (Array.isArray(value)) return value.every(isJsonValue)
  if (kind === 'object') return Object.values(value as Record<string, unknown>).every(isJsonValue)
  return false
}

function checkConfig(node: NodeDef, errors: string[]): void {
  const spec = nodeSpec(node.type)
  if (spec === null) {
    errors.push(`未注册的节点类型: ${node.type}`)
    return
  }
  const missing = spec.required_config.filter((key) => {
    const value = node.config[key]
    return value === undefined || value === null || value === ''
  })
  if (missing.length > 0) {
    errors.push(`节点 ${node.type} 缺少必填参数: [${missing.join(', ')}]`)
  }
  const allowed = new Set([...spec.required_config, ...spec.optional_config])
  const unknown = Object.keys(node.config).filter((key) => !allowed.has(key))
  if (unknown.length > 0) {
    errors.push(`节点 ${node.type} 含未知参数: [${unknown.join(', ')}]`)
  }
  for (const [key, floor] of Object.entries(spec.min_config)) {
    const value = node.config[key]
    if (typeof value === 'number' && value < floor) {
      errors.push(`节点 ${node.type} 参数 ${key} 不得小于 ${floor}`)
    }
  }
}

/**
 * 提交前的全量体检，返回全部中文错误（不抛异常，便于表单逐条展示）。
 * 与后端同源但不等价：服务端还会检查重名版本、真实注册表等。
 */
export function validateGraph(def: WorkflowDef): string[] {
  const errors: string[] = []
  if (def.name.trim().length < MIN_DEFINITION_NAME_CHARS) errors.push(`工作流名称至少 ${MIN_DEFINITION_NAME_CHARS} 个字符`)
  if (def.name.length > MAX_DEFINITION_NAME_CHARS) errors.push(`工作流名称不得超过 ${MAX_DEFINITION_NAME_CHARS} 字符`)
  if (def.description.length > MAX_DESCRIPTION_CHARS) errors.push(`工作流描述不得超过 ${MAX_DESCRIPTION_CHARS} 字符`)
  if (def.nodes.length === 0) errors.push('工作流至少需要一个节点')
  if (def.nodes.length > MAX_NODES) errors.push(`节点数不得超过 ${MAX_NODES}`)
  if (def.edges.length > MAX_EDGES) errors.push(`连线数不得超过 ${MAX_EDGES}`)

  const ids = def.nodes.map((node) => node.node_id)
  if (new Set(ids).size !== ids.length) errors.push('节点 ID 重复')

  for (const node of def.nodes) {
    if (!NODE_ID_PATTERN.test(node.node_id)) errors.push(`节点 ID 非法: ${node.node_id}`)
    if (!NODE_TYPE_PATTERN.test(node.type)) errors.push(`节点类型非法: ${node.type}`)
    if (!(node.on_failure in ON_FAILURE_LABELS)) {
      errors.push(`节点 [${node.node_id}] 失败策略非法: ${String(node.on_failure)}`)
    }
    if (node.name.length > MAX_NODE_NAME_CHARS) errors.push(`节点 ${node.node_id} 名称不得超过 ${MAX_NODE_NAME_CHARS} 字符`)
    if (node.sla_ms < SLA_MIN_MS || node.sla_ms > SLA_MAX_MS) {
      errors.push(`节点 [${node.node_id}] sla_ms 须在 ${SLA_MIN_MS}-${SLA_MAX_MS} 之间`)
    }
    if (node.timeout_ms < SLA_MIN_MS || node.timeout_ms > SLA_MAX_MS) {
      errors.push(`节点 [${node.node_id}] timeout_ms 须在 ${SLA_MIN_MS}-${SLA_MAX_MS} 之间`)
    }
    // model.py:61-66：超时是单次执行硬上限，必须落在 SLA 预算内。
    if (node.timeout_ms > node.sla_ms) {
      errors.push(
        `节点 [${node.node_id}] timeout_ms (${node.timeout_ms}) 不得大于 sla_ms (${node.sla_ms})`,
      )
    }
    if (node.retry.max_attempts < 0 || node.retry.max_attempts > MAX_ATTEMPTS) {
      errors.push(`节点 [${node.node_id}] 重试次数须在 0-${MAX_ATTEMPTS} 之间（0 表示不重试）`)
    }
    if (node.retry.backoff_ms < 0 || node.retry.backoff_ms > MAX_BACKOFF_MS) {
      errors.push(`节点 [${node.node_id}] 重试退避须在 0-${MAX_BACKOFF_MS} 毫秒之间`)
    }
    for (const value of Object.values(node.config)) {
      if (!isJsonValue(value)) {
        errors.push(`节点 [${node.node_id}] 配置含不可序列化取值`)
        break
      }
    }
    checkConfig(node, errors)
  }

  const known = new Set(ids)
  for (const edge of def.edges) {
    if (!known.has(edge.source)) errors.push(`边的源节点不存在: ${edge.source}`)
    if (!known.has(edge.target)) errors.push(`边的目标节点不存在: ${edge.target}`)
    if (edge.source === edge.target) errors.push(`不允许自环: ${edge.source}`)
    if (edge.condition.length > 64) errors.push(`边条件不得超过 64 字符: ${edge.source} → ${edge.target}`)
  }

  if (errors.length === 0) {
    // 环路信息量太大时不再叠加：前面已有结构性错误就先报结构。
    try {
      layoutLayers(ids, def.edges)
    } catch (error) {
      const cycle = findCyclePath(ids, def.edges)
      errors.push(cycle === null ? '工作流图存在环' : `工作流图存在环: ${cycle.join(' → ')}`)
    }
  }
  return errors
}

export function assertValidGraph(def: WorkflowDef): void {
  const errors = validateGraph(def)
  if (errors.length > 0) throw new GraphValidationError(errors.join('；'))
}

/** DFS 三色标记找环，语义镜像 model.py:186-212 find_cycle（返回闭合路径便于定位）。 */
export function findCyclePath(nodeIds: readonly string[], edges: readonly EdgeDef[]): string[] | null {
  const adjacency = new Map<string, string[]>(nodeIds.map((id) => [id, []]))
  for (const edge of edges) {
    const targets = adjacency.get(edge.source)
    if (targets !== undefined) targets.push(edge.target)
  }
  const white = 0
  const grey = 1
  const black = 2
  const color = new Map<string, number>(nodeIds.map((id) => [id, white]))
  const stack: string[] = []

  const visit = (node: string): string[] | null => {
    color.set(node, grey)
    stack.push(node)
    for (const child of adjacency.get(node) ?? []) {
      const state = color.get(child) ?? white
      if (state === grey) {
        const start = stack.indexOf(child)
        return [...stack.slice(start < 0 ? 0 : start), child]
      }
      if (state === white) {
        const found = visit(child)
        if (found !== null) return found
      }
    }
    stack.pop()
    color.set(node, black)
    return null
  }

  for (const id of nodeIds) {
    if ((color.get(id) ?? white) === white) {
      const found = visit(id)
      if (found !== null) return found
    }
  }
  return null
}
