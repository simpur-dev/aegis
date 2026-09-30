import { mount, shallowMount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'

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
import { useWorkflowStore } from '@/stores/workflow'
import type { NodeProps } from '@vue-flow/core'
import { NODE_SPECS, type FlowNodeData, type NodeCategory, type NodeType } from '@/utils/graph'

/**
 * 与 backend/src/aegis/workflow/nodes.py:351-366 逐字对齐的清单。
 * 后端注册新类型而前端未跟进时，这里的集合相等断言会失败（漂移门禁）。
 */
const BACKEND_NODE_TYPES: NodeType[] = [
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
]

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
  it('恰好覆盖 nodes.py 的 16 类节点，无缺无多', () => {
    expect(Object.keys(NODE_META).sort()).toEqual([...BACKEND_NODE_TYPES].sort())
    expect(Object.keys(NODE_SPECS).sort()).toEqual([...BACKEND_NODE_TYPES].sort())
    expect(BACKEND_NODE_TYPES).toHaveLength(16)
  })

  it('面板分组展开后仍是同一集合（四类卡片组件全覆盖）', () => {
    expect(PALETTE_GROUPS.flatMap((group) => group.types).sort()).toEqual([...BACKEND_NODE_TYPES].sort())
    expect(Object.keys(CATEGORY_LABELS).sort()).toEqual((Object.keys(WORKFLOW_NODE_TYPES) as NodeCategory[]).sort())
    expect(PALETTE_GROUPS.map((group) => group.category)).toEqual(['io', 'logic', 'intelligence', 'human'])
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
    const review = createNodeDef('human_review', 'review_1')
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
    expect(byText('核签通过')?.attributes('disabled')).toBeUndefined()
    expect(byText('核签退回')?.attributes('disabled')).toBeUndefined()
    expect(byText('绕过该节点')?.attributes('disabled')).toBeUndefined()
    expect(wrapper.text()).toContain('等待人工决策')
  })
})
