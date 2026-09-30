/**
 * 节点面板 / 检查器的元数据注册表（纯数据，无 Vue 依赖）。
 *
 * 16 类节点的"类别归属"与"配置白名单"在 @/utils/graph 的 NODE_SPECS（后端 nodes.py 的镜像），
 * 这里只补 UI 需要的中文名、字段编辑器形态与新建节点时的默认配置。
 * Record<NodeType, …> 的穷尽性由 TS 保证：后端新增类型时本文件编译即失败。
 */

import { NODE_SPECS, NODE_TYPES, type JsonValue, type NodeCategory, type NodeDef, type NodeType } from '@/utils/graph'

export type FieldKind = 'text' | 'number' | 'string_list' | 'json'

export interface ConfigField {
  key: string
  label: string
  kind: FieldKind
  placeholder: string
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
const numberField = (key: string, label: string, placeholder = '数值'): ConfigField => ({ key, label, kind: 'number', placeholder })
const listField = (key: string, label: string, placeholder = '逐项填写，可增删'): ConfigField => ({ key, label, kind: 'string_list', placeholder })
const jsonField = (key: string, label: string, placeholder = 'JSON 数组或对象'): ConfigField => ({
  key,
  label,
  kind: 'json',
  placeholder,
})


export const NODE_META: Record<NodeType, NodeMeta> = {
  data_fetch: {
    label: '数据接入',
    description: '数据接入：按区域/指标拉取最新读数',
    fields: [textField('region_code', '区域编码', '如 RG-CHUAN-01'), textField('metric', '指标', '如 rain_10min'), numberField('limit', '读数上限', '条数')],
    defaults: { region_code: 'RG-CHUAN-01', metric: 'rain_10min', limit: 200 },
  },
  api_call: {
    label: '外部接口',
    description: '外部 API 调用',
    fields: [textField('method', '请求方法', 'GET / POST'), textField('url', '接口地址', '/api/v1/...'), jsonField('body', '请求体', 'JSON 对象')],
    defaults: { method: 'GET', url: '/healthz' },
  },
  device_control: {
    label: '设备联动',
    description: '设备控制：广播/路障等联动',
    fields: [
      textField('device', '设备标识', 'broadcast_01'),
      textField('action', '动作', 'play / raise'),
      textField('command_url', '指令地址', '/api/v1/devices/...'),
    ],
    defaults: { device: 'broadcast_01', action: 'play', command_url: '/api/v1/devices/broadcast' },
  },
  notify: {
    label: '事件通报',
    description: '通知：总线事件广播',
    fields: [textField('text', '通报内容'), textField('level', '级别', 'info / warn / critical')],
    defaults: { text: '态势通报', level: 'info' },
  },
  threshold: {
    label: '阈值判断',
    description: '阈值判断：对读数做触发条件判定',
    fields: [
      jsonField('conditions', '条件列表', '[{"metric":"rain_10min","op":">=","threshold":25,"agg":"max"}]'),
      textField('upstream', '上游节点 ID', '留空则用实例 payload.rows'),
      textField('mode', '判定模式', 'any / all'),
    ],
    defaults: { conditions: [{ metric: 'rain_10min', op: '>=', threshold: 25, agg: 'max' }], mode: 'any' },
  },
  branch: {
    label: '条件分支',
    description: '条件分支：按规则路由',
    fields: [
      jsonField('rules', '分支规则', '[{"when":"risk_level","op":"<=","value":2,"then":"high"}]'),
      textField('upstream', '取值上游节点 ID'),
      textField('default', '兜底分支名', 'else'),
    ],
    defaults: { rules: [{ when: 'triggered', op: '==', value: true, then: 'triggered' }], default: 'not_triggered' },
  },
  join: {
    label: '并行汇聚',
    description: '并行汇聚：合并多路上游结果',
    fields: [listField('upstream', '参与汇聚的上游节点')],
    defaults: { upstream: [] },
  },
  delay: {
    label: '延时等待',
    description: '延时等待：节流与错峰',
    fields: [numberField('seconds', '等待秒数', '上限 30 秒')],
    defaults: {},
  },
  feedback_collect: {
    label: '反馈采集',
    description: '反馈采集：回执与响应状态',
    fields: [numberField('wait_seconds', '等待回执秒数')],
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
    fields: [listField('upstream', '合并进定级输入的上游节点'), jsonField('extra', '附加上下文', 'JSON 对象')],
    defaults: {},
  },
  situation_simulate: {
    label: '态势推演',
    description: '态势推演：多情景趋势',
    fields: [jsonField('extra', '附加上下文', 'JSON 对象')],
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
    fields: [numberField('risk_level', '降级风险等级', '1-5')],
    defaults: { risk_level: 4 },
  },
  human_review: {
    label: '人工核签',
    description: '人工审核/会签：等待决策',
    fields: [textField('prompt', '核签提示语'), listField('options', '可选决策值', 'approve / reject')],
    defaults: { prompt: '请值班指挥员确认', options: ['approve', 'reject'] },
  },
}

export interface PaletteGroup {
  category: NodeCategory
  label: string
  types: NodeType[]
}

/** 节点面板分组：类别顺序即展示顺序，组内按类型名排序保证渲染稳定。 */
export const PALETTE_GROUPS: PaletteGroup[] = (Object.keys(CATEGORY_LABELS) as NodeCategory[])
  .map((category) => ({
    category,
    label: CATEGORY_LABELS[category],
    types: NODE_TYPES.filter((type) => NODE_SPECS[type].category === category),
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