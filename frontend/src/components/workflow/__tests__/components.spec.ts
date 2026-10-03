import { mount, shallowMount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, h, isReactive, reactive } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import NodeInspector from '@/components/workflow/NodeInspector.vue'
import NodeChips from '@/components/workflow/NodeChips.vue'
import NodeHuman from '@/components/workflow/NodeHuman.vue'
import NodeIntelligence from '@/components/workflow/NodeIntelligence.vue'
import NodeIo from '@/components/workflow/NodeIo.vue'
import NodeLogic from '@/components/workflow/NodeLogic.vue'
import NodePalette from '@/components/workflow/NodePalette.vue'
import NodeShell from '@/components/workflow/NodeShell.vue'
import RuntimeActions from '@/components/workflow/RuntimeActions.vue'
import {
  CATEGORY_LABELS,
  NODE_META,
  PALETTE_GROUPS,
  configPreview,
  createNodeDef,
} from '@/components/workflow/registry'
import { WORKFLOW_NODE_TYPES } from '@/components/workflow/nodeComponents'
import { useWorkflowCanvas } from '@/components/workflow/useWorkflowCanvas'
import { createWorkflowStore, useWorkflowStore } from '@/stores/workflow'
import type { NodeProps } from '@vue-flow/core'
import { NODE_SPECS, type FlowNodeData, type NodeCategory, type NodeType } from '@/utils/graph'
import { backendNodeTypes } from '@/testing/repoSource'

/**
 * 后端节点类型从 `backend/src/aegis/workflow/nodes.py` 现解析，不在前端抄一份清单：
 * 抄来的清单只能证明"两份抄写一致"，证明不了"另一端没变"。
 */
const BACKEND_NODE_TYPES = backendNodeTypes() as NodeType[]

const HANDLE_STUB = { stubs: { Handle: true } }

function cardData(type: NodeType, overrides: Partial<FlowNodeData> = {}): FlowNodeData {
  return {
    def: createNodeDef(type, `${type}_1`),
    category: NODE_SPECS[type].category,
    description: NODE_META[type].description,
    state: null,
    attempts: 0,
    note: '',
    ...overrides,
  }
}

function nodeProps(data: FlowNodeData, id = 'fetch_1') {
  return {
    id,
    type: data.category as string,
    selected: false,
    connectable: true,
    data,
    dimensions: { width: 200, height: 80 },
    position: { x: 0, y: 0 },
    dragging: false,
    resizing: false,
    zIndex: 0,
    // NodeProps.events 是 Vue Flow 已标记废弃的必填项：给空对象并按声明类型收窄，
    // 否则字面量 {} 不满足 NodeEventsOn 的索引签名。
    events: {} as NodeProps['events'],
  }
}

describe('节点类型注册表与后端对齐', () => {
  it('覆盖后端注册的全部类型，无缺无多，并满足"≥10 类节点可视化编排"的考核口径', () => {
    expect(BACKEND_NODE_TYPES.length, '后端注册数就是画布要覆盖的下限来源').toBeGreaterThanOrEqual(10)
    expect(Object.keys(NODE_META).sort()).toEqual([...BACKEND_NODE_TYPES].sort())
    expect(Object.keys(NODE_SPECS).sort()).toEqual([...BACKEND_NODE_TYPES].sort())
    expect(NODE_SPECS).toHaveProperty('degrade_to_rule')
  })

  it('面板分组展开后仍是同一集合（四类卡片组件全覆盖）', () => {
    expect(PALETTE_GROUPS.flatMap((group) => group.types).sort()).toEqual([...BACKEND_NODE_TYPES].sort())
    expect(Object.keys(CATEGORY_LABELS).sort()).toEqual((Object.keys(WORKFLOW_NODE_TYPES) as NodeCategory[]).sort())
    expect(PALETTE_GROUPS.map((group) => group.category)).toEqual(['io', 'logic', 'intelligence', 'human'])
  })

  /**
   * 组件定义必须是 raw 的：Vue Flow 把 node-types 收进自己的 reactive 状态，
   * 未 markRaw 时每次渲染都会刷 "Vue received a Component that was made a reactive object"，
   * 真机画布上 16 个节点就是满屏 warning（控制台脏 = 真事故会被淹没）。
   *
   * 断言先复现 Vue Flow 那一步（放进 reactive 再取出）：直接对导出对象问 isReactive
   * 恒为 false，那样的门禁证明不了任何事。
   */
  it('node-types 映射里的组件扛得住 Vue Flow 的 reactive 收纳', () => {
    const stored = reactive({ ...WORKFLOW_NODE_TYPES })
    for (const category of Object.keys(WORKFLOW_NODE_TYPES) as NodeCategory[]) {
      expect(isReactive(stored[category]), `${category} 被响应式代理了`).toBe(false)
      expect(stored[category]).toBe(WORKFLOW_NODE_TYPES[category])
    }
  })

  it('每类节点都有中文名、描述与默认配置，且默认配置不含未知键', () => {
    for (const type of BACKEND_NODE_TYPES) {
      const meta = NODE_META[type]
      expect(meta.label.length, type).toBeGreaterThan(1)
      expect(meta.description.length, type).toBeGreaterThan(1)
      const allowed = new Set([...NODE_SPECS[type].required_config, ...NODE_SPECS[type].optional_config])
      for (const key of Object.keys(meta.defaults)) {
        expect(allowed.has(key), `${type}.${key}`).toBe(true)
      }
      for (const key of NODE_SPECS[type].required_config) {
        expect(meta.defaults[key], `${type} 缺默认必填项 ${key}`).not.toBeUndefined()
      }
    }
  })

  it('createNodeDef 的默认配置互不共享引用', () => {
    const a = createNodeDef('threshold', 'a_1')
    const b = createNodeDef('threshold', 'b_1')
    expect(a.config).toEqual(b.config)
    expect(a.config).not.toBe(b.config)
  })
})

describe('NodeShell 共用外框', () => {
  const base = {
    label: '风险定级',
    nodeType: 'risk_assess',
    state: null as FlowNodeData['state'],
    slaMs: 5_000,
    timeoutMs: 4_000,
    attempts: 0,
    selected: false,
  }

  it('渲染标签、类型、SLA 与超时读数', () => {
    const wrapper = shallowMount(NodeShell, { props: base })
    expect(wrapper.find('.wf-node__title').text()).toBe('风险定级')
    expect(wrapper.find('.wf-node__type').text()).toBe('risk_assess')
    expect(wrapper.text()).toContain('SLA 5000 ms')
    expect(wrapper.text()).toContain('超时 4000 ms')
  })

  it('无实例时标注未运行，不显示重试次数', () => {
    const wrapper = shallowMount(NodeShell, { props: base })
    expect(wrapper.find('.wf-node__badge').text()).toBe('未运行')
    expect(wrapper.find('.wf-node').classes()).toContain('is-idle')
    expect(wrapper.text()).not.toContain('已试')
  })

  it('bypassed 与 skipped 徽标不同', () => {
    expect(shallowMount(NodeShell, { props: { ...base, state: 'bypassed' } }).find('.wf-node__badge').text()).toBe('人工旁路')
    const skipped = shallowMount(NodeShell, { props: { ...base, state: 'skipped' } })
    expect(skipped.find('.wf-node__badge').text()).toBe('分支跳过')
    expect(skipped.find('.wf-node').classes()).toContain('is-skipped')
  })

  it('选中态与强调色随状态变化', () => {
    const wrapper = shallowMount(NodeShell, { props: { ...base, selected: true, state: 'failed' } })
    expect(wrapper.find('.wf-node').classes()).toContain('is-selected')
    expect(wrapper.find('.wf-node').attributes('style')).toContain('#cf1322')
    expect(wrapper.text()).toContain('已试 0 次')
  })
})

describe('类别卡片组件（16 类 → 4 个组件）', () => {
  it('io 卡片把标量配置渲染成摘要条', () => {
    const wrapper = mount(NodeIo, { global: HANDLE_STUB, props: nodeProps(cardData('data_fetch')) })
    const shell = wrapper.findComponent(NodeShell)
    expect(shell.props('label')).toBe('数据接入')
    expect(shell.props('nodeType')).toBe('data_fetch')
    const chips = wrapper.findComponent(NodeChips)
    expect(chips.props('chips').map((chip) => chip.value)).toEqual(['RG-CHUAN-01', 'rain_10min'])
  })

  it('logic 卡片对分支/汇聚给出规则计数', () => {
    const branch = mount(NodeLogic, { global: HANDLE_STUB, props: nodeProps(cardData('branch')) })
    expect(branch.text()).toContain('1 条规则，兜底 not_triggered')
    const delay = mount(NodeLogic, { global: HANDLE_STUB, props: nodeProps(cardData('delay')) })
    expect(delay.findComponent(NodeChips).props('chips')).toEqual([])
  })

  it('intelligence 卡片区分智能服务与纯规则兜底', () => {
    expect(mount(NodeIntelligence, { global: HANDLE_STUB, props: nodeProps(cardData('risk_assess')) }).text()).toContain('依赖智能服务')
    expect(
      mount(NodeIntelligence, { global: HANDLE_STUB, props: nodeProps(cardData('degrade_to_rule')) }).text(),
    ).toContain('纯规则兜底')
  })

  it('human 卡片露出提示语与候选决策值', () => {
    const wrapper = mount(NodeHuman, { global: HANDLE_STUB, props: nodeProps(cardData('human_review')) })
    expect(wrapper.text()).toContain('请值班指挥员确认')
    expect(wrapper.text()).toContain('approve / reject')
  })

  it('human 卡片缺省值与 nodes.py handler 一致', () => {
    const data = cardData('human_review')
    data.def.config = {}
    const wrapper = mount(NodeHuman, { global: HANDLE_STUB, props: nodeProps(data) })
    expect(wrapper.text()).toContain('请值班指挥员确认')
    expect(wrapper.text()).toContain('approve / reject')
  })

  it('configPreview 只取前两项并折叠复杂结构', () => {
    const def = createNodeDef('threshold', 't_1')
    const chips = configPreview(def, 2)
    expect(chips).toHaveLength(2)
    expect(chips[0]).toEqual({ key: 'conditions', label: '条件列表', value: '1 项' })
    expect(chips[1]?.value).toBe('any')
    expect(configPreview(createNodeDef('device_control', 'd_1'), 3).map((chip) => chip.key)).toEqual([
      'device',
      'action',
      'command_url',
    ])
  })
})

describe('NodePalette 节点面板', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('分组渲染 16 个可拖拽项', () => {
    const store = useWorkflowStore()
    store.resetDefinition('链路')
    const wrapper = mount(NodePalette)
    expect(wrapper.findAll('.wf-palette__group')).toHaveLength(4)
    const items = wrapper.findAll('.wf-palette__item')
    expect(items).toHaveLength(16)
    expect(items.every((item) => item.attributes('draggable') === 'true')).toBe(true)
  })

  it('点击面板项即向画布追加新节点并选中', () => {
    const store = useWorkflowStore()
    store.resetDefinition('链路')
    const wrapper = mount(NodePalette)
    const join = wrapper.findAll('.wf-palette__item').find((item) => item.text().includes('join'))
    expect(join).toBeDefined()
    expect(join?.element instanceof HTMLButtonElement).toBe(true)
    ;(join?.element as HTMLButtonElement).click()
    expect(store.current?.nodes).toHaveLength(1)
    expect(store.current?.nodes[0]?.type).toBe('join')
    expect(store.selectedNodeId).toBe('join_1')
  })

  it('拖拽起始写入被拖类型的 MIME 数据', () => {
    const store = useWorkflowStore()
    store.resetDefinition('链路')
    const wrapper = mount(NodePalette)
    const payload = new Map<string, string>()
    const dataTransfer = { setData: (key: string, value: string) => payload.set(key, value), effectAllowed: '' }
    const item = wrapper.findAll('.wf-palette__item')[0]
    item?.trigger('dragstart', { dataTransfer })
    expect([...payload.values()]).toHaveLength(1)
    expect([...payload.values()][0]).toBeDefined()
  })
})

describe('检查器与运行中操作', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('超时输入的上限即该节点 SLA', () => {
    const store = useWorkflowStore()
    store.resetDefinition('链路')
    store.addNode({ ...createNodeDef('risk_assess', 'assess_1'), sla_ms: 12_000, timeout_ms: 9_000 })
    store.select('assess_1')
    const wrapper = mount(NodeInspector, { global: { stubs: { RuntimeActions: true, ConfigFields: true } } })
    const timeout = wrapper.findAll('input[type="number"]').at(1)
    expect(timeout?.attributes('max')).toBe('12000')
    expect(wrapper.text()).toContain('风险定级')
  })

  it('无实例时运行中操作整体不可用', () => {
    const store = useWorkflowStore()
    store.resetDefinition('链路')
    store.addNode(createNodeDef('human_review', 'review_1'))
    const node = store.current?.nodes[0]
    if (!node) throw new Error('addNode 之后 current.nodes[0] 不应为空')
    const wrapper = mount(RuntimeActions, { props: { node } })
    expect(wrapper.findAll('button').every((button) => button.attributes('disabled') !== undefined)).toBe(true)
    expect(wrapper.text()).toContain('尚未选择运行实例')
  })

  it('等待核签的节点只放行决策与旁路', async () => {
    const store = useWorkflowStore()
    store.resetDefinition('链路')
    const review = { ...createNodeDef('human_review', 'review_1'), config: { prompt: '请核签', options: ['approve', 'adjust', 'reject'] } }
    store.addNode(review)
    store.select('review_1')
    store.instance = {
      instance_id: 'wfi_0123456789ab',
      workflow_id: 'wf_0123456789ab',
      workflow_version: 1,
      trace_id: 't-1',
      status: 'waiting',
      error: null,
      nodes: [{ node_id: 'review_1', type: 'human_review', state: 'awaiting_human', attempts: 1, schedule_latency_ms: 1, duration_ms: 2, output: {}, error: null, notes: ['等待人工决策: 请值班指挥员确认'] }],
    }
    const wrapper = mount(RuntimeActions, { props: { node: review } })
    const buttons = wrapper.findAll('button')
    const byText = (text: string) => buttons.find((button) => button.text() === text)
    expect(byText('以当前参数下发改参')?.attributes('disabled')).toBeDefined()
    expect(byText('核签通过（approve）')?.attributes('disabled')).toBeUndefined()
    expect(byText('核签退回（reject）')?.attributes('disabled')).toBeUndefined()
    expect(byText('绕过该节点')?.attributes('disabled')).toBeUndefined()
    expect(wrapper.text()).toContain('等待人工决策')
  })

  /**
   * 三颗决策按钮必须都看得懂。
   *
   * 真机上中间那颗直接写着英文裸值 `adjust`：值班员不知道点下去引擎会走哪条分支，
   * 而模板里的三元式只认识 approve/reject。标签带裸值，提交出去的也仍是裸值。
   */
  it('非 approve/reject 的候选也有中文标签，提交值仍是裸值', async () => {
    const store = useWorkflowStore()
    store.resetDefinition('链路')
    const review = { ...createNodeDef('human_review', 'review_1'), config: { prompt: '请核签', options: ['approve', 'adjust', 'reject'] } }
    store.addNode(review)
    store.select('review_1')
    store.instance = {
      instance_id: 'wfi_0123456789ab',
      workflow_id: 'wf_0123456789ab',
      workflow_version: 1,
      trace_id: 't-1',
      status: 'waiting',
      error: null,
      nodes: [{ node_id: 'review_1', type: 'human_review', state: 'awaiting_human', attempts: 1, schedule_latency_ms: 1, duration_ms: 2, output: {}, error: null, notes: [] }],
    }
    const submit = vi.spyOn(store, 'submitDecision').mockResolvedValue(true)
    const wrapper = mount(RuntimeActions, { props: { node: review } })
    const adjust = wrapper.findAll('button').find((button) => button.text() === '核签更正（adjust）')
    expect(adjust).toBeDefined()
    expect(adjust?.attributes('disabled')).toBeUndefined()
    await adjust?.trigger('click')
    expect(vi.mocked(submit).mock.calls).toEqual([['review_1', 'adjust', '']])
  })

  /** 决策候选为空（引擎未登记 options，例如失败转人工）时退化为自由文本输入。 */
  it('候选为空时给出自由文本决策框', () => {
    const store = useWorkflowStore()
    store.resetDefinition('链路')
    const review = { ...createNodeDef('human_review', 'review_1'), config: { prompt: '失败接管', options: [] } }
    store.addNode(review)
    store.instance = {
      instance_id: 'wfi_0123456789ab',
      workflow_id: 'wf_0123456789ab',
      workflow_version: 1,
      trace_id: 't-1',
      status: 'waiting',
      error: null,
      nodes: [{ node_id: 'review_1', type: 'human_review', state: 'awaiting_human', attempts: 1, schedule_latency_ms: 1, duration_ms: 2, output: {}, error: null, notes: [] }],
    }
    const wrapper = mount(RuntimeActions, { props: { node: review } })
    expect(wrapper.text()).not.toContain('核签通过')
    expect(wrapper.find('input[placeholder="决策值"]').exists()).toBe(true)
    expect(wrapper.findAll('button').find((b) => b.text() === '提交决策')?.attributes('disabled')).toBeDefined()
  })
})

describe('画布粘合层 useWorkflowCanvas', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  /** composable 要在组件 setup 里取（useVueFlow 需要注入上下文）。 */
  function withCanvas<R>(setup: () => R): R {
    let out!: R
    mount(defineComponent({ setup: () => { out = setup(); return () => h('div') } }))
    return out
  }

  /**
   * 画布高亮必须跟着模型走。
   *
   * 此前视图每次由 defToGraph 整体重建，selected 只活在 Vue Flow 的内部点选里，
   * 1.5s 一次的实例轮询把它整批冲掉；focusInstance 的程序化选中更是完全不可见——
   * 右侧检查器显示"人工核签"，画布上找不出哪一颗是它。
   */
  it('模型选中的节点在视图里高亮，取消选中后整体熄灭', () => {
    const store = createWorkflowStore({} as never)()
    store.resetDefinition('链路')
    store.addNode(createNodeDef('data_fetch', 'fetch_1'))
    store.addNode(createNodeDef('human_review', 'review_1'))
    store.select('review_1')
    const { view } = withCanvas(() => useWorkflowCanvas(store))
    expect(view.value.nodes.filter((node) => node.selected).map((node) => node.id)).toEqual(['review_1'])
    store.select(null)
    expect(view.value.nodes.every((node) => !node.selected)).toBe(true)
  })
})
