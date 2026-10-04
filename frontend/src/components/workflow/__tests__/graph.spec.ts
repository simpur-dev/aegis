import { describe, expect, it } from 'vitest'

import {
  GraphValidationError,
  MAX_NODES,
  NODE_SPECS,
  NODE_STATE_STYLES,
  canBypass,
  canDecide,
  canPatchRuntimeConfig,
  categoryOfNode,
  defToGraph,
  edgeId,
  findCyclePath,
  graphToDef,
  inboundSources,
  isInstanceStateTerminal,
  isTerminalNodeState,
  layerPositions,
  layoutLayers,
  nextAvailableId,
  nextNodeId,
  patchNode,
  removeEdge,
  removeNodes,
  setEdgeCondition,
  stateStyle,
  upsertEdge,
  validateGraph,
  type EdgeDef,
  type NodeDef,
  type NodeState,
  type NodeType,
  type WorkflowDef,
} from '@/utils/graph'

/**
 * 与后端 nodes.py:21-32 / model.py 的取值域手工对齐：
 * 后端新增枚举值而这里未同步时，下列 set-equality 断言会失败（刻意用作漂移门禁）。
 */
const BACKEND_NODE_STATES: NodeState[] = [
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

function node(nodeId: string, type: NodeType = 'data_fetch', overrides: Partial<NodeDef> = {}): NodeDef {
  return {
    node_id: nodeId,
    type,
    name: nodeId,
    config: {},
    sla_ms: 5_000,
    timeout_ms: 5_000,
    on_failure: 'retry',
    retry: { max_attempts: 1, backoff_ms: 200 },
    ...overrides,
  }
}

function edge(source: string, target: string, condition = ''): EdgeDef {
  return { source, target, condition }
}

function definition(nodes: NodeDef[], edges: EdgeDef[] = [], overrides: Partial<WorkflowDef> = {}): WorkflowDef {
  return {
    workflow_id: 'wf_0123456789ab',
    name: '泥石流防控链路',
    description: '阈值 → 识别 → 定级 → 核签 → 发布',
    version: 3,
    nodes,
    edges,
    created_by: 'platform.web',
    status: 'active',
    ...overrides,
  }
}

function fullCanvas(): WorkflowDef {
  const branch = node('branch_risk', 'branch', {
    name: '风险分流',
    config: { rules: [{ when: 'risk_level', op: '<=', value: 2, then: 'high' }], default: 'low' },
    sla_ms: 8_000,
    timeout_ms: 6_000,
    on_failure: 'degrade',
    retry: { max_attempts: 2, backoff_ms: 500 },
  })
  const join = node('join_all', 'join', { config: { upstream: ['publish_high', 'notify_low'] } })
  const nodes = [
    node('fetch_1', 'data_fetch', { config: { region_code: 'RG-01', metric: 'rain_10min', limit: 100 } }),
    node('threshold_1', 'threshold', { config: { conditions: [{ metric: 'rain_10min', op: '>=', threshold: 30 }], mode: 'any' } }),
    node('assess_1', 'risk_assess'),
    branch,
    node('publish_high', 'warning_publish', { config: { channels: ['sms', 'broadcast'] } }),
    node('notify_low', 'notify', { config: { text: '风险较低', level: 'info' } }),
    join,
    node('review_1', 'human_review', { config: { prompt: '请确认', options: ['approve', 'reject'] } }),
  ]
  const edges = [
    edge('fetch_1', 'threshold_1'),
    edge('threshold_1', 'assess_1'),
    edge('assess_1', 'branch_risk'),
    edge('branch_risk', 'publish_high', 'high'),
    edge('branch_risk', 'notify_low', 'low'),
    edge('branch_risk', 'review_1', 'approve'),
    edge('publish_high', 'join_all'),
    edge('notify_low', 'join_all'),
  ]
  return definition(nodes, edges)
}

describe('utils/graph 往返映射', () => {
  it('def → 视图 → def 保留全部字段', () => {
    const def = fullCanvas()
    expect(graphToDef(defToGraph(def), def)).toEqual(def)
  })

  it('视图只改坐标不丢模型字段（graphToDef 忽略 positions）', () => {
    const def = fullCanvas()
    const moved = defToGraph(def, { positions: { fetch_1: { x: 999, y: -999 } } })
    expect(graphToDef(moved, def)).toEqual(def)
  })

  /**
   * 真机测出来的：节点名写成 `数据接入 `（尾巴多一个空格）保存后，
   * 列表里出现"看着同名、其实不同名"的两行。后端已裁（`_TrimsText`），
   * 这里同步裁，否则画布与存进去的内容差一个空格，重开才"变"。
   */
  it('节点名带首尾空格时逆映射裁掉，其余字段一个都不动', () => {
    const def = fullCanvas()
    const padded: WorkflowDef = {
      ...def,
      nodes: def.nodes.map((node, index) => (index === 0 ? { ...node, name: '  数据接入  ' } : node)),
    }
    const back = graphToDef(defToGraph(padded), padded)
    expect(back.nodes[0]?.name).toBe('数据接入')
    expect(back.nodes[0]).toEqual({ ...padded.nodes[0], name: '数据接入' })
    expect(back.nodes.map((node) => node.name).slice(1)).toEqual(def.nodes.slice(1).map((node) => node.name))
  })

  it('视图节点携带完整定义与类别，运行态缺省为 null', () => {
    const view = defToGraph(fullCanvas())
    const first = view.nodes[0]
    expect(first?.data.def.node_id).toBe('fetch_1')
    expect(first?.data.category).toBe('io')
    expect(first?.data.state).toBeNull()
    expect(first?.data.attempts).toBe(0)
  })

  it('运行态与描述可注入卡片数据', () => {
    const def = fullCanvas()
    const view = defToGraph(def, {
      states: { fetch_1: { state: 'bypassed', attempts: 2, note: '人工旁路' } },
      descriptions: { data_fetch: '数据接入：按区域/指标拉取最新读数' },
      positions: { fetch_1: { x: 12, y: 34 } },
    })
    const card = view.nodes[0]
    expect(card?.position).toEqual({ x: 12, y: 34 })
    expect(card?.data.state).toBe('bypassed')
    expect(card?.data.description).toContain('数据接入')
  })

  /**
   * 选中态是模型给的，不是 Vue Flow 内部点出来的。
   *
   * 画布视图每次都由 defToGraph 整体重建，Vue Flow 的点选会在下一次实例轮询时被
   * 整批替换掉——真机上表现为"右侧检查器改的是 review，画布上看不出是哪一颗"。
   */
  it('选中节点在视图里 selected=true，其余为 false', () => {
    const view = defToGraph(fullCanvas(), { selectedNodeId: 'review_1' })
    expect(view.nodes.filter((node) => node.selected).map((node) => node.id)).toEqual(['review_1'])
    expect(defToGraph(fullCanvas()).nodes.every((node) => node.selected === false)).toBe(true)
  })

  it('连线 id 稳定且唯一，可据此回删', () => {
    const def = fullCanvas()
    const ids = defToGraph(def).edges.map((item) => item.id)
    expect(new Set(ids).size).toBe(ids.length)
    expect(defToGraph(def).edges.map((item) => item.id)).toEqual(ids)
    const trimmed = removeEdge(def, ids[3])
    expect(trimmed.edges).toHaveLength(def.edges.length - 1)
    expect(trimmed.edges.some((item) => item.source === 'branch_risk' && item.target === 'publish_high')).toBe(false)
  })

  it('未注册类型仍渲染（回落到 io 卡片），但由校验单独报错', () => {
    const def = definition([node('x_1', 'data_fetch'), { ...node('odd_1'), type: 'brand_new' as NodeType }])
    const view = defToGraph(def)
    expect(categoryOfNode('brand_new')).toBe('io')
    expect(view.nodes[1]?.type).toBe('io')
    expect(validateGraph(def).join('；')).toContain('未注册的节点类型: brand_new')
  })
})

describe('utils/graph 自动布局', () => {
  it('分层把 join 排在 branch 之后', () => {
    const def = fullCanvas()
    const layers = layoutLayers(
      def.nodes.map((item) => item.node_id),
      def.edges,
    )
    const depth = (id: string): number => layers.findIndex((layer) => layer.includes(id))
    expect(depth('join_all')).toBeGreaterThan(depth('branch_risk'))
    expect(depth('branch_risk')).toBeGreaterThan(depth('assess_1'))
    expect(depth('fetch_1')).toBe(0)
  })

  it('同层节点按 ID 排序、纵向居中，层间横向推进', () => {
    const def = fullCanvas()
    const positions = layerPositions(
      def.nodes.map((item) => item.node_id),
      def.edges,
    )
    // 第 4 层是 notify_low / publish_high / review_1，按 ID 排序后关于 0 上下对称
    expect(positions.notify_low?.y).toBeLessThan(positions.publish_high?.y ?? 0)
    expect(positions.publish_high?.y).toBe(0)
    expect(positions.review_1?.y).toBeGreaterThan(positions.publish_high?.y ?? 0)
    expect(positions.join_all?.x ?? 0).toBeGreaterThan(positions.fetch_1?.x ?? 0)
  })

  it('存在环时抛中文错误', () => {
    expect(() => layoutLayers(['a_1', 'b_1'], [edge('a_1', 'b_1'), edge('b_1', 'a_1')])).toThrow(GraphValidationError)
  })

  it('defToGraph 在没有手工坐标时按分层落位', () => {
    const def = definition([node('a_1'), node('b_1')], [edge('a_1', 'b_1')])
    const view = defToGraph(def)
    const [a, b] = view.nodes
    expect((b?.position.x ?? 0)).toBeGreaterThan(a?.position.x ?? 0)
  })
})

describe('utils/graph 提交前校验', () => {
  it('重复节点 ID', () => {
    const def = definition([node('dup_1'), node('dup_1')], [])
    expect(validateGraph(def)).toContain('节点 ID 重复')
  })

  it('悬空边（源/目标各报一次）', () => {
    const def = definition([node('a_1')], [edge('a_1', 'ghost_1'), edge('ghost_2', 'a_1')])
    const errors = validateGraph(def)
    expect(errors).toContain('边的目标节点不存在: ghost_1')
    expect(errors).toContain('边的源节点不存在: ghost_2')
  })

  it('自环边', () => {
    const def = definition([node('a_1')], [edge('a_1', 'a_1')])
    expect(validateGraph(def)).toContain('不允许自环: a_1')
  })

  it('环路：给出环路径', () => {
    const def = definition([node('a_1'), node('b_1'), node('c_1')], [edge('a_1', 'b_1'), edge('b_1', 'c_1'), edge('c_1', 'a_1')])
    const errors = validateGraph(def)
    expect(errors.some((message) => message.startsWith('工作流图存在环: '))).toBe(true)
    expect(findCyclePath(['a_1', 'b_1', 'c_1'], def.edges)).toEqual(['a_1', 'b_1', 'c_1', 'a_1'])
  })

  it('timeout_ms 大于 sla_ms 在前端即拦截', () => {
    const def = definition([node('a_1', 'data_fetch', { sla_ms: 1_000, timeout_ms: 2_000 })], [])
    expect(validateGraph(def)).toContain('节点 [a_1] timeout_ms (2000) 不得大于 sla_ms (1000)')
  })

  it('重试次数 0 合法（表示不重试），6 非法', () => {
    expect(validateGraph(definition([node('a_1', 'data_fetch', { retry: { max_attempts: 0, backoff_ms: 0 } })], []))).toEqual([])
    const tooMany = definition([node('a_1', 'data_fetch', { retry: { max_attempts: 6, backoff_ms: 200 } })], [])
    expect(validateGraph(tooMany).join()).toContain('重试次数须在 0-5 之间')
  })

  it('必填参数缺失与未知参数按后端同文案报错', () => {
    const missing = definition([node('t_1', 'threshold')], [])
    expect(validateGraph(missing).join()).toContain('节点 threshold 缺少必填参数: [conditions]')
    const unknown = definition([node('t_1', 'threshold', { config: { conditions: [1], nope: 'x' } })], [])
    expect(validateGraph(unknown).join()).toContain('节点 threshold 含未知参数: [nope]')
  })

  it('min_config 下限（degrade_to_rule.risk_level ≥ 1）', () => {
    const def = definition([node('d_1', 'degrade_to_rule', { config: { risk_level: 0 } })], [])
    expect(validateGraph(def).join()).toContain('参数 risk_level 不得小于 1')
  })

  it('取值域：ID 非法、SLA 越界、名称过短', () => {
    const def = definition([node('Bad ID', 'data_fetch', { sla_ms: 10, timeout_ms: 10 })], [], { name: '单' })
    const errors = validateGraph(def)
    expect(errors).toContain('节点 ID 非法: Bad ID')
    expect(errors.some((message) => message.includes('sla_ms 须在 100-3600000 之间'))).toBe(true)
    expect(errors).toContain('工作流名称至少 2 个字符')
  })

  it('节点数上限与后端一致（64）', () => {
    const nodes = Array.from({ length: MAX_NODES + 1 }, (_, index) => node(`n_${index}`))
    expect(validateGraph(definition(nodes, [])).join()).toContain('节点数不得超过 64')
  })

  it('合法图零报错', () => {
    expect(validateGraph(fullCanvas())).toEqual([])
  })
})

describe('utils/graph 状态与运行中判定', () => {
  it('样式表覆盖 NodeState 全集（含 BYPASSED），新增状态即失败', () => {
    expect(Object.keys(NODE_STATE_STYLES).sort()).toEqual([...BACKEND_NODE_STATES].sort())
  })

  it('每个状态都有中文标签、颜色与类名', () => {
    for (const state of BACKEND_NODE_STATES) {
      const style = NODE_STATE_STYLES[state]
      expect(style.label.length, state).toBeGreaterThan(1)
      expect(style.color, state).toMatch(/^#[0-9a-f]{6}$/)
      expect(style.className, state).toBe(`is-${state.replace(/_/gu, '-')}`)
    }
  })

  it('bypassed 与 skipped 是不同语义（不同标签与颜色）', () => {
    expect(stateStyle('bypassed').label).toBe('人工旁路')
    expect(stateStyle('skipped').label).toBe('分支跳过')
    expect(stateStyle('bypassed').color).not.toBe(stateStyle('skipped').color)
  })

  it('未映射的后端状态走兜底而不崩', () => {
    expect(stateStyle('exploded').className).toBe('is-unmapped')
  })

  it('终态集合与 model.py:35-37 一致', () => {
    const terminal = BACKEND_NODE_STATES.filter(isTerminalNodeState)
    expect(terminal.sort()).toEqual(['bypassed', 'cancelled', 'degraded', 'failed', 'skipped', 'succeeded'])
  })

  it('改参/旁路/核签的状态白名单照搬引擎', () => {
    expect(BACKEND_NODE_STATES.filter((state) => canPatchRuntimeConfig(state))).toEqual(['pending', 'ready'])
    expect(BACKEND_NODE_STATES.filter((state) => canBypass(state))).toEqual(['pending', 'ready', 'awaiting_human'])
    expect(BACKEND_NODE_STATES.filter(canDecide)).toEqual(['awaiting_human'])
    expect(canBypass(null)).toBe(false)
    expect(canPatchRuntimeConfig('succeeded')).toBe(false)
  })

  it('实例终态不再轮询', () => {
    expect(isInstanceStateTerminal('succeeded')).toBe(true)
    expect(isInstanceStateTerminal('waiting')).toBe(false)
  })
})

describe('utils/graph 模型编辑', () => {
  it('upsertEdge 去重且拒绝自环由校验兜住', () => {
    const def = definition([node('a_1'), node('b_1')], [])
    const once = upsertEdge(def, 'a_1', 'b_1')
    expect(once.edges).toHaveLength(1)
    expect(upsertEdge(once, 'a_1', 'b_1').edges).toHaveLength(1)
    expect(upsertEdge(once, 'a_1', 'b_1', 'high').edges).toHaveLength(2)
  })

  it('removeNodes 级联删除端点连线', () => {
    const def = fullCanvas()
    const trimmed = removeNodes(def, ['branch_risk'])
    expect(trimmed.nodes).toHaveLength(def.nodes.length - 1)
    expect(trimmed.edges.some((item) => item.source === 'branch_risk' || item.target === 'branch_risk')).toBe(false)
  })

  it('patchNode 不改 node_id，retry 逐字段合并', () => {
    const def = fullCanvas()
    // 故意构造带 node_id 的越界补丁（NodePatch 类型上不含该字段），验证运行时也不会被偷换
    const hostile = { node_id: 'hijack', sla_ms: 9_000, timeout_ms: 9_000, retry: { backoff_ms: 1_000 } } as Partial<NodeDef>
    const patched = patchNode(def, 'assess_1', hostile)
    const assess = patched.nodes.find((item) => item.node_id === 'assess_1')
    expect(assess?.retry).toEqual({ max_attempts: 1, backoff_ms: 1_000 })
    expect(patched.nodes.some((item) => item.node_id === 'hijack')).toBe(false)
  })

  it('边条件按后端语义归一化（trim + 小写）', () => {
    const def = fullCanvas()
    const first = def.edges[3]
    const id = edgeId(3, first)
    const updated = setEdgeCondition(def, id, '  HIGH  ')
    expect(updated.edges[3]?.condition).toBe('high')
    expect(validateGraph(updated)).toEqual([])
  })

  it('新节点 ID 不冲突且满足后端正则', () => {
    const def = definition([node('fetch_1'), node('fetch_2')], [])
    const created = nextNodeId(def, 'fetch')
    expect(created).toBe('fetch_3')
    expect(/^[a-z][a-z0-9_-]{0,31}$/u.test(nextAvailableId([], 'warning_publish'))).toBe(true)
  })

  it('入边源去重，供 upstream 回填', () => {
    const def = fullCanvas()
    expect(inboundSources(def, 'join_all')).toEqual(['publish_high', 'notify_low'])
    expect(inboundSources(def, 'fetch_1')).toEqual([])
  })

  it('16 类节点的类别归属唯一真源（nodes.py 的镜像）', () => {
    expect(Object.keys(NODE_SPECS).sort()).toEqual(
      [
        'api_call',
        'branch',
        'data_fetch',
        'delay',
        'degrade_to_rule',
        'device_control',
        'feedback_collect',
        'hazard_identify',
        'human_review',
        'join',
        'notify',
        'risk_assess',
        'situation_simulate',
        'threshold',
        'warning_generate',
        'warning_publish',
      ].sort(),
    )
    expect(categoryOfNode('human_review')).toBe('human')
    expect(categoryOfNode('degrade_to_rule')).toBe('intelligence')
    expect(categoryOfNode('feedback_collect')).toBe('logic')
    expect(categoryOfNode('notify')).toBe('io')
  })
})
