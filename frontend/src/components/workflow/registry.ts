/**
 * 节点面板 / 检查器的元数据注册表（纯数据，无 Vue 依赖）。
 *
 * 节点的"类别归属"与"配置白名单"在 @/utils/graph 的 NODE_SPECS（后端 nodes.py 的镜像，19 类），
 * 这里只补 UI 需要的中文名、字段编辑器形态与新建节点时的默认配置。
 * Record<NodeType, …> 的穷尽性由 TS 保证：后端新增类型时本文件编译即失败。
 */

import { NODE_SPECS, PANEL_NODE_TYPES, type JsonValue, type NodeCategory, type NodeDef, type NodeType } from '@/utils/graph'

/**
 * 参数编辑器的形态。新增一种形态 = 加一个 kind + ConfigFields 里一个分支；
 * 节点类型只挑形态，不各自写组件，19 类表单才不会长成 19 套行为。
 */
export type FieldKind =
  | 'text'
  | 'template'
  | 'number'
  | 'select'
  | 'string_list'
  | 'json'
  | 'node_ref'
  | 'node_ref_list'
  | 'rules'
  | 'conditions'
  | 'start_variables'

export interface SelectOption {
  value: string
  label: string
}

export interface ConfigField {
  key: string
  label: string
  kind: FieldKind
  placeholder: string
  /** `select` 的候选；其余形态忽略。 */
  options?: readonly SelectOption[]
  /** 数字形态的取值域：越界的错误由 configValidation 就地展示。 */
  min?: number
  max?: number
  /** 字段下方的补充说明（为什么这一格不能为空、值的语义等）。 */
  hint?: string
}

export interface NodeMeta {
  /** 画布/面板上显示的中文短名。 */
  label: string
  /** 与 nodes.py 注册描述保持一致的兜底文案（后端 node-types 可用时优先展示后端值）。 */
  description: string
  fields: readonly ConfigField[]
  defaults: Record<string, JsonValue>
}

export const CATEGORY_LABELS: Record<NodeCategory, string> = {
  io: '数据与执行',
  logic: '逻辑编排',
  intelligence: '智能分析',
  human: '人工介入',
}

const textField = (key: string, label: string, placeholder = ''): ConfigField => ({ key, label, kind: 'text', placeholder })
/** 文本里允许出现 `{{#节点.输出#}}`：这类格子才挂变量选择器。 */
const templateField = (key: string, label: string, placeholder = '可插入上游变量'): ConfigField => ({
  key,
  label,
  kind: 'template',
  placeholder,
})
const numberField = (
  key: string,
  label: string,
  placeholder = '数值',
  bounds: { min?: number; max?: number } = {},
): ConfigField => ({ key, label, kind: 'number', placeholder, ...bounds })
const selectField = (key: string, label: string, options: readonly string[]): ConfigField => ({
  key,
  label,
  kind: 'select',
  placeholder: '',
  options: options.map((value) => ({ value, label: value })),
})
const listField = (key: string, label: string, placeholder = '逐项填写，可增删'): ConfigField => ({
  key,
  label,
  kind: 'string_list',
  placeholder,
})
const jsonField = (key: string, label: string, placeholder = 'JSON 数组或对象'): ConfigField => ({
  key,
  label,
  kind: 'json',
  placeholder,
})
/** 点名上游节点：候选只来自真实入边，所以填了没连线的节点在面板上就该看见错误，而不是等发布吃 422。 */
const nodeRefField = (key: string, label: string, multiple = false, hint = ''): ConfigField => ({
  key,
  label,
  kind: multiple ? 'node_ref_list' : 'node_ref',
  placeholder: '',
  hint,
})
const rulesField = (key: string, label: string): ConfigField => ({ key, label, kind: 'rules', placeholder: '' })
const conditionsField = (key: string, label: string): ConfigField => ({ key, label, kind: 'conditions', placeholder: '' })

const UPSTREAM_HINT = '只能选已连到本节点的上游；选了没连线的节点，发布会被服务端拒收并点名这条缺失的连线'


export const NODE_META: Record<NodeType, NodeMeta> = {
  start: {
    label: '开始',
    description: '起点：声明运行输入变量',
    fields: [
      {
        key: 'variables',
        label: '输入变量',
        kind: 'start_variables',
        placeholder: '',
        hint: '变量名进 payload：只允许字母/数字/下划线、不超 32 项，且不能是 __proto__ 这类原型链名字',
      },
    ],
    defaults: { variables: [] },
  },
  end: {
    label: '结束',
    description: '终点：标记流程结束',
    fields: [],
    defaults: {},
  },
  data_fetch: {
    label: '数据接入',
    description: '数据接入：按区域/指标拉取最新读数',
    fields: [
      textField('region_code', '区域编码', '如 RG-CHUAN-01'),
      textField('metric', '指标', '如 rain_10min'),
      numberField('limit', '读数上限', '条数', { min: 0 }),
    ],
    defaults: { region_code: 'RG-CHUAN-01', metric: 'rain_10min', limit: 200 },
  },
  api_call: {
    label: '外部接口',
    description: '外部 API 调用',
    fields: [
      textField('method', '请求方法', 'GET / POST'),
      templateField('url', '接口地址', '/api/v1/... 或 https://…，可插入上游变量'),
      jsonField('body', '请求体', 'JSON 对象'),
    ],
    defaults: { method: 'GET', url: '/healthz' },
  },
  device_control: {
    label: '设备联动',
    description: '设备控制：广播/路障等联动',
    fields: [
      textField('device', '设备标识', 'broadcast_01'),
      textField('action', '动作', 'play / raise'),
      templateField('command_url', '指令地址', '/api/v1/devices/...，可插入上游变量'),
    ],
    defaults: { device: 'broadcast_01', action: 'play', command_url: '/api/v1/devices/broadcast' },
  },
  notify: {
    label: '事件通报',
    description: '通知：总线事件广播',
    fields: [
      templateField('text', '通报内容', '可插入上游变量，如 {{#fetch_1.count#}} 条读数'),
      textField('level', '级别', 'debug / info / warning'),
    ],
    defaults: { text: '态势通报', level: 'info' },
  },
  threshold: {
    label: '阈值判断',
    description: '阈值判断：对读数做触发条件判定',
    fields: [
      conditionsField('conditions', '条件列表'),
      nodeRefField('upstream', '取数上游', false, UPSTREAM_HINT),
      selectField('mode', '判定模式', ['any', 'all']),
    ],
    defaults: { conditions: [{ metric: 'rain_10min', op: '>=', threshold: 25, agg: 'max' }], mode: 'any' },
  },
  branch: {
    label: '条件分支',
    description: '条件分支：按规则路由',
    fields: [
      rulesField('rules', '分支规则'),
      nodeRefField('upstream', '取值上游', false, UPSTREAM_HINT),
      textField('default', '兜底分支名', 'else'),
    ],
    defaults: { rules: [{ when: 'triggered', op: '==', value: true, then: 'triggered' }], default: 'not_triggered' },
  },
  join: {
    label: '并行汇聚',
    description: '并行汇聚：合并多路上游结果',
    fields: [nodeRefField('upstream', '参与汇聚的上游', true, UPSTREAM_HINT)],
    defaults: { upstream: [] },
  },
  // 内部原语：出现在"标准五段处置链路"这类系统模板里，面板不给拖（PALETTE_GROUPS 按 internal 滤掉），
  // 但画布要能把它画出来、检查器要能显示它的段名——所以镜像与元数据都得有它。
  chain_stage: {
    label: '链路段（系统）',
    description: '链路段：把标准五段之一交给链路侧执行（不可从面板拖入）',
    fields: [selectField('stage', '段名', ['perceive', 'assess', 'plan', 'execute', 'feedback'])],
    defaults: { stage: 'perceive' },
  },
  delay: {
    label: '延时等待',
    description: '延时等待：节流与错峰',
    fields: [
      // 运行期是 `min(seconds, 30)`（nodes.py:_delay）：填 60 也只会等 30，
      // 所以取值域写在这里，而不是等人到实例日志里发现"怎么只等了 30 秒"。
      numberField('seconds', '等待秒数', '0-30，超过 30 按 30 执行', { min: 0, max: 30 }),
    ],
    defaults: {},
  },
  feedback_collect: {
    label: '反馈采集',
    description: '反馈采集：回执与响应状态',
    fields: [numberField('wait_seconds', '等待回执秒数', '秒', { min: 0 })],
    defaults: {},
  },
  hazard_identify: {
    label: '灾种识别',
    description: '灾种识别：判定候选灾种',
    fields: [jsonField('extra', '附加上下文', 'JSON 对象')],
    defaults: {},
  },
  risk_assess: {
    label: '风险定级',
    description: '风险定级：产出 1-5 级结论',
    fields: [
      nodeRefField('upstream', '合并进定级输入的上游', true, UPSTREAM_HINT),
      jsonField('extra', '附加上下文', 'JSON 对象'),
    ],
    defaults: {},
  },
  situation_simulate: {
    label: '态势推演',
    description: '态势推演：多情景趋势',
    fields: [
      // 后端把 `upstream` 收进白名单（optional_config），推演自己会合并全部入边产出；
      // 面板不给这一格，存量定义里的这项就只能改 JSON。
      nodeRefField('upstream', '参考的上游', true, UPSTREAM_HINT),
      jsonField('extra', '附加上下文', 'JSON 对象'),
    ],
    defaults: {},
  },
  warning_generate: {
    label: '预警生成',
    description: '预警生成：产出分级预警内容',
    fields: [jsonField('extra', '附加上下文', 'JSON 对象')],
    defaults: {},
  },
  warning_publish: {
    label: '预警发布',
    description: '预警发布：多通道靶向触达',
    fields: [listField('channels', '发布通道', '如 sms / broadcast / wechat')],
    defaults: {},
  },
  degrade_to_rule: {
    label: '规则降级',
    description: '规则降级：智能体不可用时保守定级',
    // 1-5 之外运行期直接判失败（nodes.py:_degrade_to_rule），所以这里当场拦住。
    fields: [numberField('risk_level', '降级风险等级', '1-5', { min: 1, max: 5 })],
    defaults: { risk_level: 4 },
  },
  human_review: {
    label: '人工核签',
    description: '人工审核/会签：等待决策',
    fields: [
      templateField('prompt', '核签提示语', '值班员看到的那一句话，可插入上游变量'),
      listField('options', '可选决策值', 'approve / reject'),
    ],
    defaults: { prompt: '请值班指挥员确认', options: ['approve', 'reject'] },
  },
}

export interface PaletteGroup {
  category: NodeCategory
  label: string
  types: NodeType[]
}

/** `upstream` 那几格的候选：一个真实入边节点（ID + 显示名 + 类型）。 */
export interface NodeRefCandidate {
  id: string
  label: string
  type: string
}

/** 节点面板分组：类别顺序即展示顺序，组内按类型名排序保证渲染稳定。
 *  取材用 PANEL_NODE_TYPES（镜像去掉内部原语），与后端 `/node-types` 同一个口径。 */
export const PALETTE_GROUPS: PaletteGroup[] = (Object.keys(CATEGORY_LABELS) as NodeCategory[])
  .map((category) => ({
    category,
    label: CATEGORY_LABELS[category],
    types: PANEL_NODE_TYPES.filter((type) => NODE_SPECS[type].category === category),
  }))
  .filter((group) => group.types.length > 0)

export function nodeMeta(type: string): NodeMeta | null {
  return (NODE_META as Record<string, NodeMeta | undefined>)[type] ?? null
}

/** 面板 → 画布拖放时携带的类型标识（浏览器剪贴板 MIME，自定前缀避免与站点其它拖放冲突）。 */
export const NODE_DRAG_MIME = 'application/x-aegis-node-type'

export interface ConfigChip {
  key: string
  label: string
  value: string
}

function chipValue(value: JsonValue | undefined): string | null {
  if (value === undefined || value === null || value === '') return null
  if (Array.isArray(value)) return `${value.length} 项`
  if (typeof value === 'object') {
    const keys = Object.keys(value)
    return keys.length === 0 ? null : `${keys.length} 个字段`
  }
  return typeof value === 'string' ? value : String(value)
}

/** 卡片上只露该类型前两项标量配置，复杂结构（条件/规则）留给右侧检查器。 */
export function configPreview(def: NodeDef, limit = 2): ConfigChip[] {
  const meta = nodeMeta(def.type)
  if (meta === null) return []
  const chips: ConfigChip[] = []
  for (const field of meta.fields) {
    const value = chipValue(def.config[field.key])
    if (value === null) continue
    chips.push({ key: field.key, label: field.label, value })
    if (chips.length >= limit) break
  }
  return chips
}

/**
 * 新建节点的初始定义：sla/timeout/retry 取后端默认值（workflow_api.py:29-30、model.py:45-46），
 * config 用注册表默认值，保证"拖上来即可保存"不触发必填校验失败。
 */
export function createNodeDef(type: NodeType, nodeId: string): NodeDef {
  const meta = NODE_META[type]
  return {
    node_id: nodeId,
    type,
    name: meta.label,
    config: structuredClone(meta.defaults),
    sla_ms: 5_000,
    timeout_ms: 5_000,
    on_failure: 'retry',
    retry: { max_attempts: 1, backoff_ms: 200 },
  }
}