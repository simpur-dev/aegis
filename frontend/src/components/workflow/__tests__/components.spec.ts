import { mount, shallowMount } from '@vue/test-utils'
import { Modal, message } from 'ant-design-vue'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, h, isReactive, reactive } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import DefinitionInspector from '@/components/workflow/DefinitionInspector.vue'
import NodeInspector from '@/components/workflow/NodeInspector.vue'
import NodeChips from '@/components/workflow/NodeChips.vue'
import NodeHuman from '@/components/workflow/NodeHuman.vue'
import NodeIntelligence from '@/components/workflow/NodeIntelligence.vue'
import NodeIo from '@/components/workflow/NodeIo.vue'
import NodeLogic from '@/components/workflow/NodeLogic.vue'
import NodePalette from '@/components/workflow/NodePalette.vue'
import NodeShell from '@/components/workflow/NodeShell.vue'
import RuntimeActions from '@/components/workflow/RuntimeActions.vue'
import RunInputDialog from '@/components/workflow/RunInputDialog.vue'
import RuntimePanel from '@/components/workflow/RuntimePanel.vue'
import {
  CATEGORY_LABELS,
  NODE_META,
  PALETTE_GROUPS,
  configPreview,
  createNodeDef,
} from '@/components/workflow/registry'
import { WORKFLOW_NODE_TYPES } from '@/components/workflow/nodeComponents'
import { useWorkflowCanvas } from '@/components/workflow/useWorkflowCanvas'
import { createEditorContext, EDITOR_CONTEXT_KEY, type EditorContext } from '@/components/workflow/editor/context'
import type { DefinitionSummary, InstanceDetail } from '@/api/workflow'
import type { NodeProps } from '@vue-flow/core'
import { MAX_ATTEMPTS, MAX_BACKOFF_MS, NODE_SPECS, findNode, type FlowNodeData, type JsonValue, type NodeCategory, type NodeType } from '@/utils/graph'
import { installUnsavedGuard } from '@/components/workflow/confirmDiscard'
import { backendInternalNodeTypes, backendNodeTypes, backendPanelNodeTypes, readRepoFile } from '@/testing/repoSource'

/**
 * 后端节点类型从 `backend/src/aegis/workflow/nodes.py` 现解析，不在前端抄一份清单：
 * 抄来的清单只能证明"两份抄写一致"，证明不了"另一端没变"。
 */
const BACKEND_NODE_TYPES = backendNodeTypes() as NodeType[]

const HANDLE_STUB = { stubs: { Handle: true } }

/** 每个用例一套 context：组件从注入面取 store，测试与组件看的是同一份对象。 */
function editorContext(): EditorContext {
  const context = createEditorContext()
  // 组件用例不测自动保存：关掉防抖，免得 800ms 后真的走默认网关。
  context.document.autoSaveEnabled = false
  return context
}

function withEditor(context: EditorContext): { provide: Record<string, unknown> } {
  return { provide: { [EDITOR_CONTEXT_KEY]: context } as Record<string, unknown> }
}

/** 组件用例只关心节点与状态；实例详情新增的图/时间戳字段用一份空壳补齐。 */
function detail(part: Partial<InstanceDetail> = {}): InstanceDetail {
  const workflowId = part.workflow_id ?? 'wf_0123456789ab'
  return {
    instance_id: 'wfi_0000000000ab',
    workflow_id: workflowId,
    workflow_version: 1,
    trace_id: 't-1',
    status: 'running',
    error: null,
    payload: {},
    nodes: [],
    lineage_id: workflowId,
    preview: false,
    preview_id: null,
    created_at: '',
    finished_at: null,
    results: {},
    graph: { nodes: [], edges: [], positions: {} },
    ...part,
  }
}

function selectedNode(context: EditorContext) {
  const definition = context.document.definition
  const nodeId = context.editor.selectedNodeId
  if (definition === null || nodeId === null) return null
  return findNode(definition, nodeId)
}

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

  it('面板分组展开后仍是后端的可拖集合（内部原语不进面板，四类卡片组件全覆盖）', () => {
    expect(PALETTE_GROUPS.flatMap((group) => group.types).sort()).toEqual(backendPanelNodeTypes().sort())
    /* note 是画布备注、不是后端节点类型，不在分类集合里，但必须注册成 Vue Flow 节点组件。 */
    const canvasCategories = (Object.keys(WORKFLOW_NODE_TYPES) as Array<NodeCategory | 'note'>)
      .filter((key) => key !== 'note')
      .sort()
    expect(Object.keys(CATEGORY_LABELS).sort()).toEqual(canvasCategories)
    expect(WORKFLOW_NODE_TYPES.note).toBeDefined()
    expect(PALETTE_GROUPS.map((group) => group.category)).toEqual(['io', 'logic', 'intelligence', 'human'])
  })

  it('镜像里标了 internal 的类型与后端一字不差：谁被挡在面板外，两端得说同一句话', () => {
    /**
     * 后端把 `chain_stage` 设成内部原语（它要的是链路盘面，从画布拖出来必然失败），
     * 前端镜像必须同时知道两件事：这个类型存在（画布/检查器要能画它、校验它），
     * 以及它不可拖（面板里不该出现）。少任何一半，症状都是"前端说这张图合法、后端拒收"。
     */
    const mirrored = (Object.keys(NODE_SPECS) as NodeType[]).filter((type) => NODE_SPECS[type].internal === true).sort()
    expect(mirrored).toEqual(backendInternalNodeTypes())
    expect(mirrored, '内部原语应当确实存在于后端注册表').not.toEqual([])
    const draggable = PALETTE_GROUPS.flatMap((group) => group.types)
    for (const type of mirrored) expect(draggable, `${type} 是内部原语，不该出现在面板里`).not.toContain(type)
    expect(draggable).toContain('branch')
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
    known: true,
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

  it('未知类型画成虚线卡并标只读，运行状态徽标让位', () => {
    const wrapper = shallowMount(NodeShell, {
      props: { ...base, nodeType: 'future_type', known: false, state: 'running' },
    })
    expect(wrapper.find('.wf-node').classes()).toContain('is-unknown')
    expect(wrapper.find('.wf-node__type').text()).toBe('未知类型 · future_type')
    expect(wrapper.find('.wf-node__badge').text()).toBe('只读')
  })
})

describe('NodeInspector 对未知类型降级', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('隐藏全部编辑器、给出只读说明，但保留删除入口', () => {
    const context = editorContext()
    context.document.resetDocument('链路')
    context.document.addNode({ ...createNodeDef('notify', 'future_1'), type: 'future_type' as NodeType })
    context.editor.select('future_1')
    const wrapper = mount(NodeInspector, {
      global: { ...withEditor(context), stubs: { RuntimeActions: true, ConfigFields: true } },
    })
    expect(wrapper.find('.wf-inspector__unknown').text()).toContain('future_type')
    expect(wrapper.findAll('input[type="text"]')).toHaveLength(0)
    expect(wrapper.findAll('input[type="number"]')).toHaveLength(0)
    expect(wrapper.text()).toContain('删除节点')
  })
})

describe('类别卡片组件（按类别分卡，4 个组件覆盖全部类型）', () => {
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
    expect(wrapper.text(), '卡片摆裸枚举值，值班员读不懂「adjust」是哪颗按钮').toContain('核签通过 / 核签退回')
    expect(wrapper.text()).not.toContain('approve')
  })

  it('human 卡片缺省值与 nodes.py handler 一致', () => {
    const data = cardData('human_review')
    data.def.config = {}
    const wrapper = mount(NodeHuman, { global: HANDLE_STUB, props: nodeProps(data) })
    expect(wrapper.text()).toContain('请值班指挥员确认')
    expect(wrapper.text()).toContain('核签通过 / 核签退回')
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

  it('分组渲染 18 个可拖拽项', () => {
    const context = editorContext()
    context.document.resetDocument('链路')
    const wrapper = mount(NodePalette, { global: withEditor(context) })
    expect(wrapper.findAll('.wf-palette__group')).toHaveLength(4)
    const items = wrapper.findAll('.wf-palette__item')
    expect(items).toHaveLength(18)
    expect(items.every((item) => item.attributes('draggable') === 'true')).toBe(true)
  })

  it('点击面板项即向画布追加新节点并选中', () => {
    const context = editorContext()
    context.document.resetDocument('链路')
    const wrapper = mount(NodePalette, { global: withEditor(context) })
    const join = wrapper.findAll('.wf-palette__item').find((item) => item.text().includes('join'))
    expect(join).toBeDefined()
    expect(join?.element instanceof HTMLButtonElement).toBe(true)
    ;(join?.element as HTMLButtonElement).click()
    expect(context.document.definition?.nodes).toHaveLength(1)
    expect(context.document.definition?.nodes[0]?.type).toBe('join')
    expect(context.editor.selectedNodeId).toBe('join_1')
  })

  it('拖拽起始写入被拖类型的 MIME 数据', () => {
    const context = editorContext()
    context.document.resetDocument('链路')
    const wrapper = mount(NodePalette, { global: withEditor(context) })
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
    const context = editorContext()
    context.document.resetDocument('链路')
    context.document.addNode({ ...createNodeDef('risk_assess', 'assess_1'), sla_ms: 12_000, timeout_ms: 9_000 })
    context.editor.select('assess_1')
    const wrapper = mount(NodeInspector, {
      global: { ...withEditor(context), stubs: { RuntimeActions: true, ConfigFields: true } },
    })
    const timeout = wrapper.findAll('input[type="number"]').at(1)
    expect(timeout?.attributes('max')).toBe('12000')
    expect(wrapper.text()).toContain('风险定级')
  })

  /**
   * `max` 属性只管住浏览器的步进按钮，管不住键入的数字：
   * 敲 999999 仍然进模型，差别只在"什么时候发现"。这里要求**改完就夹住**，
   * 而不是等到点保存才吃一条 validateGraph 的红字。
   */
  it('敲进超界的超时值会被夹到该节点 SLA', async () => {
    const context = editorContext()
    context.document.resetDocument('链路')
    context.document.addNode({ ...createNodeDef('risk_assess', 'assess_1'), sla_ms: 12_000, timeout_ms: 9_000 })
    context.editor.select('assess_1')
    const wrapper = mount(NodeInspector, {
      global: { ...withEditor(context), stubs: { RuntimeActions: true, ConfigFields: true } },
    })
    const timeout = wrapper.findAll('input[type="number"]').at(1)
    await timeout?.setValue('999999')
    await timeout?.trigger('change')
    expect(selectedNode(context)?.timeout_ms).toBe(12_000)
    expect(selectedNode(context)?.sla_ms).toBe(12_000)
  })

  it('重试两格同样夹住（上界来自 model.py 的那条口径）', async () => {
    const context = editorContext()
    context.document.resetDocument('链路')
    context.document.addNode({ ...createNodeDef('notify', 'notify_1'), retry: { max_attempts: 1, backoff_ms: 200 } })
    context.editor.select('notify_1')
    const wrapper = mount(NodeInspector, {
      global: { ...withEditor(context), stubs: { RuntimeActions: true, ConfigFields: true } },
    })
    const [attempts, backoff] = wrapper.findAll('input[type="number"]').slice(2)
    expect(attempts?.attributes('max')).toBe(String(MAX_ATTEMPTS))
    expect(backoff?.attributes('max')).toBe(String(MAX_BACKOFF_MS))
    await attempts?.setValue('99')
    await attempts?.trigger('change')
    await backoff?.setValue('900000')
    await backoff?.trigger('change')
    expect(selectedNode(context)?.retry).toEqual({ max_attempts: MAX_ATTEMPTS, backoff_ms: MAX_BACKOFF_MS })
  })

  /**
   * 界面上的说明不许与系统实际做的事相反。
   *
   * 这句 hint 曾写着"定义接口当前不接受 retry 字段，保存时不下发，仅本地留存"，
   * 而 `NodeInput` 早已收 retry（后端补齐它正是为了让"打开→存回"不吃 422）：
   * 值班员照着这句话会以为改白改了，于是又点一次、再点一次。
   */
  it('retry 的说明与写接口一致：它会随定义保存', () => {
    const context = editorContext()
    context.document.resetDocument('链路')
    context.document.addNode(createNodeDef('notify', 'notify_1'))
    context.editor.select('notify_1')
    const wrapper = mount(NodeInspector, {
      global: { ...withEditor(context), stubs: { RuntimeActions: true, ConfigFields: true } },
    })
    const text = wrapper.text()
    expect(text).not.toContain('不接受 retry 字段')
    expect(text).toContain('会随定义一起保存')
  })

  it('无实例时运行中操作整体不可用', () => {
    const context = editorContext()
    context.document.resetDocument('链路')
    context.document.addNode(createNodeDef('human_review', 'review_1'))
    const node = context.document.definition?.nodes[0]
    if (!node) throw new Error('addNode 之后 definition.nodes[0] 不应为空')
    const wrapper = mount(RuntimeActions, { global: withEditor(context), props: { node } })
    expect(wrapper.findAll('button').every((button) => button.attributes('disabled') !== undefined)).toBe(true)
    expect(wrapper.text()).toContain('尚未选择运行实例')
  })

  it('等待核签的节点只放行决策与旁路', async () => {
    const context = editorContext()
    context.document.resetDocument('链路')
    const review = { ...createNodeDef('human_review', 'review_1'), config: { prompt: '请核签', options: ['approve', 'adjust', 'reject'] } }
    context.document.addNode(review)
    context.editor.select('review_1')
    context.run.instance = detail({
      instance_id: 'wfi_0123456789ab',
      workflow_id: 'wf_0123456789ab',
      workflow_version: 1,
      trace_id: 't-1',
      status: 'waiting',
      error: null,
      payload: {},
      nodes: [{ node_id: 'review_1', type: 'human_review', state: 'awaiting_human', attempts: 1, schedule_latency_ms: 1, duration_ms: 2, output: {}, error: null, notes: ['等待人工决策: 请值班指挥员确认'] }],
    })
    const wrapper = mount(RuntimeActions, { global: withEditor(context), props: { node: review } })
    const buttons = wrapper.findAll('button')
    const byText = (text: string) => buttons.find((button) => button.text() === text)
    expect(byText('以当前参数下发改参')?.attributes('disabled')).toBeDefined()
    expect(byText('核签通过（approve）')?.attributes('disabled')).toBeUndefined()
    expect(byText('核签退回（reject）')?.attributes('disabled')).toBeUndefined()
    expect(byText('绕过该节点')?.attributes('disabled')).toBeUndefined()
    expect(wrapper.text()).toContain('等待人工决策')
  })

  /**
   * 真机上把 SLA 改成 -5（框里夹回 100）再点「以当前参数下发改参」：请求照发、200 回来，
   * 可里面只有节点参数——SLA/超时/重试是定义级字段（后端 `ConfigPatch` 只带 config）。
   * 按钮写着"当前参数"而人刚改的正是 SLA，所以这句范围得印在旁边，而不是等人去猜哪一下生效了。
   */
  it('改参的说明写明 SLA、超时、重试不在这条接口上', () => {
    const context = editorContext()
    context.document.resetDocument('链路')
    const fetch = createNodeDef('data_fetch', 'fetch_1')
    context.document.addNode(fetch)
    context.editor.select('fetch_1')
    context.run.instance = detail({
      instance_id: 'wfi_0123456789ab',
      workflow_id: 'wf_0123456789ab',
      workflow_version: 1,
      trace_id: 't-1',
      status: 'running',
      error: null,
      payload: {},
      nodes: [{ node_id: 'fetch_1', type: 'data_fetch', state: 'pending', attempts: 0, schedule_latency_ms: null, duration_ms: null, output: {}, error: null, notes: [] }],
    })
    const wrapper = mount(RuntimeActions, { global: withEditor(context), props: { node: fetch } })
    const text = wrapper.text()
    expect(text).toContain('SLA、超时、重试不在这条接口上')
    expect(text).toContain('只影响之后启动的实例')
  })

  /**
   * 值班员界面不夹源码行号：`engine.py:539-540` 这类括号对改代码的人有用，
   * 对签字的人只是噪音（真机 1440×900 走查在运行态面板上量到四处）。
   * 限制说明保留原话，引用回到注释与文档里查。
   */
  it('运行态面板的说明是给人读的，不夹源码行号', () => {
    const context = editorContext()
    context.document.resetDocument('链路')
    const fetch = createNodeDef('data_fetch', 'fetch_1')
    context.document.addNode(fetch)
    context.editor.select('fetch_1')
    context.run.instance = detail({
      instance_id: 'wfi_0123456789ab',
      workflow_id: 'wf_0123456789ab',
      workflow_version: 1,
      trace_id: 't-1',
      status: 'running',
      error: null,
      payload: {},
      nodes: [{ node_id: 'fetch_1', type: 'data_fetch', state: 'pending', attempts: 0, schedule_latency_ms: null, duration_ms: null, output: {}, error: null, notes: [] }],
    })
    const wrapper = mount(RuntimeActions, { global: withEditor(context), props: { node: fetch } })
    const text = wrapper.text()
    expect(text).not.toContain('engine.py')
    expect(text).toContain('仅未开始执行的节点可改参')
    expect(text).toContain('仅待执行/等待中的节点可旁路')
    expect(text).toContain('引擎不会在插入瞬间驱动新节点')
    expect(text).toContain('可提交决策')
  })

  /**
   * 签字的控件必须排在改参/旁路/插入之前：真机量过，决策按钮排在三道控件与长说明之后，
   * 落在 1440×900 首屏之外（y≈976），打开第一张单后还得滚一段才够得着「核签通过」。
   * mount 级用例只能证明按钮"存在"，够不够得着要靠这条顺序门禁 + 真机探针。
   */
  it('决策控件排在改参/旁路/插入之前', () => {
    const source = readRepoFile('frontend', 'src', 'components', 'workflow', 'RuntimeActions.vue')
    const decisions = source.indexOf('wf-runtime__decisions')
    const patch = source.indexOf('以当前参数下发改参')
    expect(decisions).toBeGreaterThan(-1)
    expect(patch).toBeGreaterThan(-1)
    expect(decisions, '核签按钮应排在改参控件的前面').toBeLessThan(patch)
  })

  /**
   * 插入的节点只属于这条实例：画布画的是定义那一张图，点完画面一动不动。
   * 真机实测（wfi_cf831d8cd1a8）：接口 200、实例里多出 `notify_1`、签完 approve 它真的 `succeeded`，
   * 而画布上找不着它——于是这一下看着像没发生。点名一句是这里能给的最便宜的诚实。
   */
  it('插入成功要说出新节点的号；失败不报喜', async () => {
    const context = editorContext()
    context.document.resetDocument('链路')
    const anchor = createNodeDef('data_fetch', 'fetch_1')
    context.document.addNode(anchor)
    context.editor.select('fetch_1')
    context.run.instance = detail({
      instance_id: 'wfi_0123456789ab',
      workflow_id: 'wf_0123456789ab',
      workflow_version: 1,
      trace_id: 't-1',
      status: 'waiting',
      error: null,
      payload: {},
      nodes: [{ node_id: 'fetch_1', type: 'data_fetch', state: 'pending', attempts: 0, schedule_latency_ms: null, duration_ms: null, output: {}, error: null, notes: [] }],
    })
    const inserted = vi.spyOn(context.run, 'insertRuntimeNode').mockResolvedValue(true)
    const say = vi.spyOn(message, 'success').mockImplementation(() => ({ key: 'k' }) as never)
    const wrapper = mount(RuntimeActions, { global: withEditor(context), props: { node: anchor } })
    const button = wrapper.findAll('button').find((item) => item.text() === '在此节点后插入')
    await button?.trigger('click')
    expect(inserted).toHaveBeenCalledTimes(1)
    expect(say).toHaveBeenCalledTimes(1)
    expect(String(say.mock.calls[0]?.[0])).toContain('notify_1')

    say.mockClear()
    inserted.mockResolvedValue(false)
    await button?.trigger('click')
    expect(say, '插入失败不能报喜').not.toHaveBeenCalled()
    say.mockRestore()
    inserted.mockRestore()
  })

  /**
   * 三颗决策按钮必须都看得懂。
   *
   * 真机上中间那颗直接写着英文裸值 `adjust`：值班员不知道点下去引擎会走哪条分支，
   * 而模板里的三元式只认识 approve/reject。标签带裸值，提交出去的也仍是裸值。
   */
  it('非 approve/reject 的候选也有中文标签，提交值仍是裸值', async () => {
    const context = editorContext()
    context.document.resetDocument('链路')
    const review = { ...createNodeDef('human_review', 'review_1'), config: { prompt: '请核签', options: ['approve', 'adjust', 'reject'] } }
    context.document.addNode(review)
    context.editor.select('review_1')
    context.run.instance = detail({
      instance_id: 'wfi_0123456789ab',
      workflow_id: 'wf_0123456789ab',
      workflow_version: 1,
      trace_id: 't-1',
      status: 'waiting',
      error: null,
      payload: {},
      nodes: [{ node_id: 'review_1', type: 'human_review', state: 'awaiting_human', attempts: 1, schedule_latency_ms: 1, duration_ms: 2, output: {}, error: null, notes: [] }],
    })
    const submit = vi.spyOn(context.run, 'submitDecision').mockResolvedValue(true)
    const wrapper = mount(RuntimeActions, { global: withEditor(context), props: { node: review } })
    const adjust = wrapper.findAll('button').find((button) => button.text() === '核签更正（adjust）')
    expect(adjust).toBeDefined()
    expect(adjust?.attributes('disabled')).toBeUndefined()
    await adjust?.trigger('click')
    // 第五个参数是改判等级：没选就是 null（前端不发 level 键），不是 0，也不是替人填一个等级
    expect(vi.mocked(submit).mock.calls).toEqual([['review_1', 'adjust', '', '', null]])
    submit.mockRestore()
  })

  /**
   * 改判要能当场录等级。
   *
   * 后端缺 `level` 就产不出标注样本（它不替人编真值等级），而这件事此前只在接口响应里说、
   * 页面上一个字都不提：值班员签完一张"核签更正"，真值表里一行没多，谁也不知道。
   * 现在两头发都钉住：选了等级就带下去，没选就明确是 null，并且这个旋钮只在有 adjust 候选时出现。
   */
  it('核签更正当场能选改判等级，选了才带下去', async () => {
    const context = editorContext()
    context.document.resetDocument('链路')
    const review = { ...createNodeDef('human_review', 'review_1'), config: { prompt: '请核签', options: ['approve', 'adjust', 'reject'] } }
    context.document.addNode(review)
    context.editor.select('review_1')
    context.run.instance = detail({
      instance_id: 'wfi_0123456789ab',
      workflow_id: 'wf_0123456ab',
      workflow_version: 1,
      trace_id: 't-1',
      status: 'waiting',
      error: null,
      payload: {},
      nodes: [{ node_id: 'review_1', type: 'human_review', state: 'awaiting_human', attempts: 1, schedule_latency_ms: 1, duration_ms: 2, output: {}, error: null, notes: [] }],
    })
    const submit = vi.spyOn(context.run, 'submitDecision').mockResolvedValue(true)
    const wrapper = mount(RuntimeActions, { global: withEditor(context), props: { node: review } })
    const level = wrapper.find('[data-testid="decision-level"]')
    expect(level.exists(), '有 adjust 候选却没有等级旋钮，这一票注定进不了真值表').toBe(true)
    await level.setValue('1')
    await wrapper.findAll('button').find((button) => button.text() === '核签更正（adjust）')?.trigger('click')
    expect(vi.mocked(submit).mock.calls.at(-1)).toEqual(['review_1', 'adjust', '', '', 1])

    await wrapper.find('[data-testid="decision-level"]').setValue('5')
    await wrapper.findAll('button').find((button) => button.text() === '核签通过（approve）')?.trigger('click')
    expect(vi.mocked(submit).mock.calls.at(-1), 'approve 用系统给的等级，不该把改判旋钮上的值顺手发出去').toEqual(['review_1', 'approve', '', '', null])
    submit.mockRestore()
  })

  it('没有改判候选的节点不给等级旋钮', () => {
    const context = editorContext()
    context.document.resetDocument('链路')
    const review = { ...createNodeDef('human_review', 'review_2'), config: { prompt: '失败接管', options: ['approve', 'reject'] } }
    context.document.addNode(review)
    context.editor.select('review_2')
    context.run.instance = detail({
      instance_id: 'wfi_0123456789ab',
      workflow_id: 'wf_0123456ab',
      workflow_version: 1,
      trace_id: 't-1',
      status: 'waiting',
      error: null,
      payload: {},
      nodes: [{ node_id: 'review_2', type: 'human_review', state: 'awaiting_human', attempts: 1, schedule_latency_ms: 1, duration_ms: 2, output: {}, error: null, notes: [] }],
    })
    expect(
      mount(RuntimeActions, { global: withEditor(context), props: { node: review } })
        .find('[data-testid="decision-level"]')
        .exists(),
    ).toBe(false)
  })

  /**
   * 这一票的去向必须出现在签单的那个人脸上。
   *
   * 后端只在那一次响应里回 `标注沉淀`，实例快照不带它——前端不显示，"签了"和"进了真值表"
   * 在页面上就成了同一件事，而内存部署下它从来没进过。
   */
  it('签完把"这一票进没进真值表"显示出来：落地的说落地，没落的说没落，没这一格就不说', () => {
    const context = editorContext()
    context.document.resetDocument('链路')
    const review = { ...createNodeDef('human_review', 'review_1'), config: { prompt: '请核签', options: ['approve', 'adjust'] } }
    context.document.addNode(review)
    context.editor.select('review_1')
    context.run.instance = detail({
      instance_id: 'wfi_0123456789ab',
      workflow_id: 'wf_0123456ab',
      workflow_version: 1,
      trace_id: 't-1',
      status: 'waiting',
      error: null,
      payload: {},
      nodes: [{ node_id: 'review_1', type: 'human_review', state: 'awaiting_human', attempts: 1, schedule_latency_ms: 1, duration_ms: 2, output: {}, error: null, notes: [] }],
    })
    /** 每次挂载前设好回执再挂：`captureFor` 是普通函数不是响应式源，同一个 wrapper 里改它不会自己重算。 */
    const shown = vi.spyOn(context.run, 'captureFor')

    shown.mockReturnValue({ text: '这一票没进真值表：改判(adjust)必须同时给出 level，否则无法确定真值等级', landed: false })
    const missing = mount(RuntimeActions, { global: withEditor(context), props: { node: review } }).find(
      '[data-testid="label-capture"]',
    )
    expect(missing.text()).toContain('没进真值表')
    expect(missing.classes()).toContain('is-missing')

    shown.mockReturnValue({ text: '这一票已进真值表：报=True 等级=5（事件 evt_9c1）', landed: true })
    const landed = mount(RuntimeActions, { global: withEditor(context), props: { node: review } }).find(
      '[data-testid="label-capture"]',
    )
    expect(landed.text()).toContain('已进真值表')
    expect(landed.classes()).toContain('is-landed')

    shown.mockReturnValue(null)
    expect(
      mount(RuntimeActions, { global: withEditor(context), props: { node: review } })
        .find('[data-testid="label-capture"]')
        .exists(),
      '这张单没这一格就别凭空造一行账',
    ).toBe(false)
    shown.mockRestore()
  })

  /**
   * 真机实测（wfi_53cfe5b1d5eb）：签"核签更正"并写了批注，接口台账里全
   * （`output.decision` = 选项 + 签字人 + 批注），而页面上一个字都不显示——
   * 节点卡还是那句提问，运行态面板还是"等待人工决策"。这套流程的说明写着
   * "结果留痕…供阈值标定回看"，回看的人在自己脸上看不到自己签了什么。
   */
  it('签过的决策在运行态面板里读得回来：选项、签字人、批注', () => {
    const context = editorContext()
    context.document.resetDocument('链路')
    const review = { ...createNodeDef('human_review', 'review_1'), config: { prompt: '请核签', options: ['approve', 'adjust', 'reject'] } }
    context.document.addNode(review)
    context.editor.select('review_1')
    context.run.instance = detail({
      instance_id: 'wfi_0123456789ab',
      workflow_id: 'wf_0123456789ab',
      workflow_version: 1,
      trace_id: 't-1',
      status: 'succeeded',
      error: null,
      payload: {},
      nodes: [
        {
          node_id: 'review_1',
          type: 'human_review',
          state: 'succeeded',
          attempts: 1,
          schedule_latency_ms: 1,
          duration_ms: 2,
          output: { decision: { choice: 'adjust', by: '巡护员扎西', comment: '实际是坡面裂缝，改判滑坡' }, choice: 'adjust' },
          error: null,
          notes: [],
        },
      ],
    })
    const trace = mount(RuntimeActions, { global: withEditor(context), props: { node: review } }).find(
      '[data-testid="decision-trace"]',
    )
    expect(trace.exists()).toBe(true)
    expect(trace.text()).toContain('核签更正（adjust）')
    expect(trace.text()).toContain('巡护员扎西')
    expect(trace.text()).toContain('实际是坡面裂缝，改判滑坡')
  })

  it('台账里的签字人是 unknown 时，页面不替它扮一个角色', () => {
    const context = editorContext()
    context.document.resetDocument('链路')
    const review = { ...createNodeDef('human_review', 'review_1'), config: { prompt: '请核签', options: ['approve'] } }
    context.document.addNode(review)
    context.run.instance = detail({
      instance_id: 'wfi_0123456789ab',
      workflow_id: 'wf_0123456789ab',
      workflow_version: 1,
      trace_id: 't-1',
      status: 'succeeded',
      error: null,
      payload: {},
      nodes: [
        {
          node_id: 'review_1',
          type: 'human_review',
          state: 'succeeded',
          attempts: 1,
          schedule_latency_ms: 1,
          duration_ms: 2,
          output: { decision: { choice: 'approve', by: 'unknown', comment: '' } },
          error: null,
          notes: [],
        },
      ],
    })
    const trace = mount(RuntimeActions, { global: withEditor(context), props: { node: review } })
      .find('[data-testid="decision-trace"]')
      .text()
    expect(trace).toContain('未填（台账记为 unknown）')
    expect(trace).toContain('（无批注）')
  })

  /** 没签过的节点不给留痕行；签字人那一格写进提交载荷（不填由 store 侧省略该键）。 */
  it('签字人填了才进载荷，未签的节点不显示留痕行', async () => {
    const context = editorContext()
    context.document.resetDocument('链路')
    const review = { ...createNodeDef('human_review', 'review_1'), config: { prompt: '请核签', options: ['approve', 'reject'] } }
    context.document.addNode(review)
    context.editor.select('review_1')
    context.run.instance = detail({
      instance_id: 'wfi_0123456789ab',
      workflow_id: 'wf_0123456789ab',
      workflow_version: 1,
      trace_id: 't-1',
      status: 'waiting',
      error: null,
      payload: {},
      nodes: [{ node_id: 'review_1', type: 'human_review', state: 'awaiting_human', attempts: 1, schedule_latency_ms: 1, duration_ms: 2, output: {}, error: null, notes: [] }],
    })
    const submitted = vi.spyOn(context.run, 'submitDecision').mockResolvedValue(true)
    const wrapper = mount(RuntimeActions, { global: withEditor(context), props: { node: review } })
    expect(wrapper.find('[data-testid="decision-trace"]').exists(), '还没签，不该凭空冒出一行留痕').toBe(false)
    await wrapper.find('[data-testid="decision-signer"]').setValue('值班员李四')
    await wrapper.findAll('button').find((b) => b.text() === '核签通过（approve）')?.trigger('click')
    expect(submitted.mock.calls).toEqual([['review_1', 'approve', '', '值班员李四', null]])
    submitted.mockRestore()
  })

  /** 决策候选为空（引擎未登记 options，例如失败转人工）时退化为自由文本输入。 */
  it('候选为空时给出自由文本决策框', () => {
    const context = editorContext()
    context.document.resetDocument('链路')
    const review = { ...createNodeDef('human_review', 'review_1'), config: { prompt: '失败接管', options: [] } }
    context.document.addNode(review)
    context.run.instance = detail({
      instance_id: 'wfi_0123456789ab',
      workflow_id: 'wf_0123456789ab',
      workflow_version: 1,
      trace_id: 't-1',
      status: 'waiting',
      error: null,
      payload: {},
      nodes: [{ node_id: 'review_1', type: 'human_review', state: 'awaiting_human', attempts: 1, schedule_latency_ms: 1, duration_ms: 2, output: {}, error: null, notes: [] }],
    })
    const wrapper = mount(RuntimeActions, { global: withEditor(context), props: { node: review } })
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
    const context = editorContext()
    context.document.resetDocument('链路')
    context.document.addNode(createNodeDef('data_fetch', 'fetch_1'))
    context.document.addNode(createNodeDef('human_review', 'review_1'))
    context.editor.select('review_1')
    const { view } = withCanvas(() => useWorkflowCanvas(context))
    expect(view.value.nodes.filter((node) => node.selected).map((node) => node.id)).toEqual(['review_1'])
    context.editor.select(null)
    expect(view.value.nodes.every((node) => !node.selected)).toBe(true)
  })
})

/**
 * 「打开」会整体覆盖画布，所以脏画布上必须先问一句。
 *
 * 真机上这一步是直接的：摆了五个节点没保存，点一下别的定义，五个就没了，
 * 页面连一句"未保存"都不说。这里验的是接线：脏的时候走确认，不脏的时候直接打开。
 */
/**
 * 刷新/关标签页这一路。
 *
 * 真机量过的原样：画布上摆好 3 个节点、界面上写着"未保存"，按 F5 之后节点归零，
 * 浏览器一次确认都没弹。站内覆盖有确认框，最容易丢图的那一下却什么都没拦。
 */
describe('installUnsavedGuard：刷新前的未保存拦截', () => {
  /**
   * 只看 `defaultPrevented`。
   *
   * `returnValue` 在现代浏览器里是"给旧 Safari 看的字符串"，jsdom 却把它实现成
   * DOM Level 2 的**布尔别名**（`event.returnValue = ''` 会读回 `false`），
   * 拿它断言等于在测替身的方言。真正让浏览器弹确认框的是 `preventDefault()`。
   */
  function fireUnload(): boolean {
    const event = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(event)
    return event.defaultPrevented
  }

  it('有未保存改动时拦住这次刷新', () => {
    const detach = installUnsavedGuard(() => true)
    try {
      expect(fireUnload()).toBe(true)
    } finally {
      detach()
    }
  })

  it('没有改动就不拦：不然每次刷新都要人多按一下', () => {
    const detach = installUnsavedGuard(() => false)
    try {
      expect(fireUnload()).toBe(false)
    } finally {
      detach()
    }
  })

  it('解除监听后不再拦：离开这页就不该替别的页面管刷新', () => {
    const detach = installUnsavedGuard(() => true)
    detach()
    expect(fireUnload()).toBe(false)
  })

  it('编排页真的装上了它（只在工具函数里写着等于没写）', () => {
    const view = readRepoFile('frontend', 'src', 'views', 'WorkflowView.vue')
    expect(view).toContain('installUnsavedGuard(() => documentStore.isDirty)')
    expect(view).toContain('detachUnsavedGuard?.()')
  })
})

describe('DefinitionInspector 打开前的未保存确认', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  function savedRow(): DefinitionSummary {
    return {
      workflow_id: 'wf_saved000001',
      lineage_id: 'wf_lineage00001',
      name: '已存链路',
      version: 1,
      status: 'active',
      description: '',
      node_count: 2,
      edge_count: 1,
      created_at: '2026-10-01T00:00:00.000Z',
      content_hash: 'sha256:abc',
    }
  }

  it('画布脏的时候点"打开"只弹确认，不覆盖画布', async () => {
    const context = editorContext()
    context.document.resetDocument('我的草稿')
    context.document.addNode(createNodeDef('data_fetch', 'fetch_1'))
    context.document.addNode(createNodeDef('notify', 'notify_1'))
    expect(context.document.isDirty).toBe(true)
    context.catalog.definitions = [savedRow()]
    const opened = vi.spyOn(context.document, 'openLineage').mockResolvedValue(true)
    const confirm = vi.spyOn(Modal, 'confirm').mockReturnValue({ destroy: vi.fn(), update: vi.fn() })
    const wrapper = mount(DefinitionInspector, { global: withEditor(context) })
    await wrapper.find('[data-testid^="open-"]').trigger('click')
    expect(confirm).toHaveBeenCalledTimes(1)
    expect(opened).not.toHaveBeenCalled()
    expect(context.document.definition?.nodes).toHaveLength(2)
    confirm.mockRestore()
  })

  it('确认之后才真的覆盖画布', async () => {
    const context = editorContext()
    context.document.resetDocument('我的草稿')
    context.document.addNode(createNodeDef('data_fetch', 'fetch_1'))
    context.catalog.definitions = [savedRow()]
    const opened = vi.spyOn(context.document, 'openLineage').mockResolvedValue(true)
    let onOk: (() => void) | undefined
    const confirm = vi.spyOn(Modal, 'confirm').mockImplementation((options) => {
      onOk = (options as { onOk?: () => void }).onOk
      return { destroy: vi.fn(), update: vi.fn() }
    })
    const wrapper = mount(DefinitionInspector, { global: withEditor(context) })
    await wrapper.find('[data-testid^="open-"]').trigger('click')
    expect(opened).not.toHaveBeenCalled()
    expect(onOk, '确认框必须带"继续"这个动作').toBeTypeOf('function')
    onOk?.()
    expect(opened).toHaveBeenCalledWith('wf_lineage00001')
    confirm.mockRestore()
  })

  it('画布干净时不拦路：直接打开', async () => {
    const context = editorContext()
    context.document.resetDocument('空白')
    expect(context.document.isDirty).toBe(false)
    context.catalog.definitions = [savedRow()]
    const opened = vi.spyOn(context.document, 'openLineage').mockResolvedValue(true)
    const confirm = vi.spyOn(Modal, 'confirm').mockReturnValue({ destroy: vi.fn(), update: vi.fn() })
    const wrapper = mount(DefinitionInspector, { global: withEditor(context) })
    await wrapper.find('[data-testid^="open-"]').trigger('click')
    expect(confirm).not.toHaveBeenCalled()
    expect(opened).toHaveBeenCalledTimes(1)
    confirm.mockRestore()
  })
})

/**
 * 归档改的是服务端那份定义的状态，而且是"一按生效"的单向动作：
 * 真机上把内置的「人工上报核签流程」归档后，本班次每条低置信度上报都开不出工单，
 * 而列表上原先只有"打开 / 归档"两个按钮，没有任何回头路。
 */
describe('DefinitionInspector 归档与取消归档', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  function twoRows(): DefinitionSummary[] {
    return [
      {
        workflow_id: 'wf_active000001',
        lineage_id: 'wf_lineage00001',
        name: '启用中的链路',
        version: 1,
        status: 'active',
        description: '',
        node_count: 2,
        edge_count: 1,
        created_at: '2026-10-01T00:00:00.000Z',
        content_hash: 'sha256:a',
      },
      {
        workflow_id: 'wf_archived0001',
        lineage_id: 'wf_lineage00002',
        name: '人工上报核签流程',
        version: 1,
        status: 'archived',
        description: '',
        node_count: 3,
        edge_count: 2,
        created_at: '2026-10-02T00:00:00.000Z',
        content_hash: 'sha256:b',
      },
    ]
  }

  it('按钮文案跟着这一行的状态走：归档态给的是「取消归档」', () => {
    const context = editorContext()
    context.document.resetDocument('空白')
    context.catalog.definitions = twoRows()
    const wrapper = mount(DefinitionInspector, { global: withEditor(context) })
    expect(wrapper.find('[data-testid="archive-wf_active000001"]').text()).toBe('归档')
    expect(wrapper.find('[data-testid="archive-wf_archived0001"]').text()).toBe('取消归档')
  })

  it('点「归档」只弹确认，不立刻改服务端状态', async () => {
    const context = editorContext()
    context.document.resetDocument('空白')
    context.catalog.definitions = twoRows()
    const archived = vi.spyOn(context.catalog, 'archiveDefinition').mockResolvedValue(true)
    const confirm = vi.spyOn(Modal, 'confirm').mockReturnValue({ destroy: vi.fn(), update: vi.fn() })
    const wrapper = mount(DefinitionInspector, { global: withEditor(context) })
    await wrapper.find('[data-testid="archive-wf_active000001"]').trigger('click')
    expect(confirm).toHaveBeenCalledTimes(1)
    expect(archived).not.toHaveBeenCalled()
    confirm.mockRestore()
  })

  it('确认之后才归档，且归档的是点的那一行', async () => {
    const context = editorContext()
    context.document.resetDocument('空白')
    context.catalog.definitions = twoRows()
    const archived = vi.spyOn(context.catalog, 'archiveDefinition').mockResolvedValue(true)
    let onOk: (() => void) | undefined
    const confirm = vi.spyOn(Modal, 'confirm').mockImplementation((options) => {
      onOk = (options as { onOk?: () => void }).onOk
      return { destroy: vi.fn(), update: vi.fn() }
    })
    const wrapper = mount(DefinitionInspector, { global: withEditor(context) })
    await wrapper.find('[data-testid="archive-wf_active000001"]').trigger('click')
    expect(onOk, '确认框必须带"归档"这个动作').toBeTypeOf('function')
    onOk?.()
    expect(archived).toHaveBeenCalledTimes(1)
    expect(archived.mock.calls[0]?.[0]).toBe('wf_active000001')
    confirm.mockRestore()
  })

  it('「取消归档」是撤回动作，不再拦第二遍', async () => {
    const context = editorContext()
    context.document.resetDocument('空白')
    context.catalog.definitions = twoRows()
    const restored = vi.spyOn(context.catalog, 'restoreDefinition').mockResolvedValue(true)
    const confirm = vi.spyOn(Modal, 'confirm').mockReturnValue({ destroy: vi.fn(), update: vi.fn() })
    const wrapper = mount(DefinitionInspector, { global: withEditor(context) })
    await wrapper.find('[data-testid="archive-wf_archived0001"]').trigger('click')
    expect(confirm).not.toHaveBeenCalled()
    expect(restored).toHaveBeenCalledTimes(1)
    expect(restored.mock.calls[0]?.[0]).toBe('wf_archived0001')
    confirm.mockRestore()
  })

  it('撤销口在后端真实存在（前端写个没人认领的 URL 等于没写）', () => {
    const api = readRepoFile('backend', 'src', 'aegis', 'api', 'workflow_api.py')
    expect(api).toContain('"/definitions/{workflow_id}/restore"')
    const client = readRepoFile('frontend', 'src', 'api', 'workflow', 'client.ts')
    expect(client).toContain('/restore')
    // 状态改回来要落在列表上：否则那一行继续写"启用中"，而服务端已经归档
    const catalog = readRepoFile('frontend', 'src', 'stores', 'workflow', 'catalog.ts')
    expect(catalog).toContain('restoreDefinition')
    expect(catalog).toContain('loadDefinitions')
  })
})

/**
 * 真机一轮量出来的：低置信度上报开出的核签工单只出现在这张列表里，
 * 列表又是后端给的创建顺序（最旧的在第一行），11 张等签的混在 16 条里，
 * 而 `/dashboard`、`/warnings` 上"核签 / 待签 / 人工"字样各 0 次——
 * 一张等着"这条预警要不要按现状生效"的单，靠人滚动去撞见是不负责任的。
 */
describe('RuntimePanel 等人签的工单要数得出来、找得到', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  function row(instanceId: string, status: string): InstanceDetail {
    return detail({
      instance_id: instanceId,
      workflow_id: 'wf_0123456789ab',
      workflow_version: 1,
      trace_id: `trc_${instanceId}`,
      status,
      error: null,
      payload: {},
      nodes: [],
    })
  }

  function picked(wrapper: ReturnType<typeof mount>): string[] {
    return wrapper.findAll('.wf-run__pick').map((button) => button.find('span').text())
  }

  it('等人签的排最前，其余按新到旧（创建顺序倒过来）', () => {
    const context = editorContext()
    context.document.resetDocument('链路')
    context.run.instances = [row('wfi_旧签', 'waiting'), row('wfi_已完', 'succeeded'), row('wfi_新签', 'waiting')]
    const wrapper = mount(RuntimePanel, { global: withEditor(context) })
    expect(picked(wrapper)).toEqual(['wfi_新签', 'wfi_旧签', 'wfi_已完'])
    expect(wrapper.text()).toContain('等人签 2 张')
  })

  it('没有等签的实例时不喊"等人签 0 张"', () => {
    const context = editorContext()
    context.document.resetDocument('链路')
    context.run.instances = [row('wfi_已完', 'succeeded')]
    const wrapper = mount(RuntimePanel, { global: withEditor(context) })
    expect(wrapper.text()).not.toContain('等人签')
  })

  it('勾选"只看这些"就把列表收成等签的那几张', async () => {
    const context = editorContext()
    context.document.resetDocument('链路')
    context.run.instances = [row('wfi_旧签', 'waiting'), row('wfi_已完', 'succeeded'), row('wfi_新签', 'waiting')]
    const wrapper = mount(RuntimePanel, { global: withEditor(context) })
    await wrapper.find('[data-testid="only-waiting"]').setValue(true)
    expect(picked(wrapper)).toEqual(['wfi_新签', 'wfi_旧签'])
  })

  it('筛完之后一张都不剩要说一句，别留个空列表不解释', async () => {
    const context = editorContext()
    context.document.resetDocument('链路')
    context.run.instances = [row('wfi_在签', 'waiting')]
    const wrapper = mount(RuntimePanel, { global: withEditor(context) })
    await wrapper.find('[data-testid="only-waiting"]').setValue(true)
    context.run.instances = [row('wfi_在签', 'succeeded')]
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[data-testid="only-waiting"]').exists(), '勾选状态要看得见，不然人不知道列表为什么是空的').toBe(true)
    expect(wrapper.text()).toContain('这一列里没有等人签的实例了')
  })

  it('队列标题下写着这一列是什么时候取的数、多久之前', () => {
    const context = editorContext()
    context.document.resetDocument('链路')
    context.run.instances = [row('wfi_已完', 'succeeded')]
    context.run.instancesUpdatedAt = '2026-10-04T19:20:00.000Z'
    const wrapper = mount(RuntimePanel, { global: withEditor(context) })
    const stamp = wrapper.find('[data-testid="freshness"]')
    expect(stamp.exists(), '新鲜度条不见了').toBe(true)
    expect(stamp.text()).toContain('取数')
    expect(stamp.text(), '只写时间不写节奏，人没法判断自己是不是在看一份不会动的账').toContain('15 秒')
    expect(stamp.text()).toMatch(/秒前|分前|比预期周期慢/)
  })

  it('一次都没取到时不许拿当前时间冒充"取数"', () => {
    const context = editorContext()
    context.document.resetDocument('链路')
    context.run.instancesUpdatedAt = null
    const wrapper = mount(RuntimePanel, { global: withEditor(context) })
    const stamp = wrapper.find('[data-testid="freshness"]').text()
    expect(stamp).toContain('尚未取到数据')
    expect(stamp, '没取到数却写"取数 …"，是凭空造出来的时间戳').not.toContain('取数')
  })
})

/**
 * 「刷新实例」这颗按钮接的是哪个方法，是真机上量出来的差别：只重拉当前实例详情时，
 * 点它之后小标题的"等人签 1 张"纹丝不动（接口那边已经归零）。
 * 组件用例都在自己桩里直接调 store，没人管视图上这颗按钮接没接对，所以这里读源码对账。
 */
describe('编排页顶栏「刷新实例」的接线', () => {
  it('绑的是 refreshAll（详情与列表一起对），不是只拉详情的 refreshInstance', () => {
    const view = readRepoFile('frontend', 'src', 'views', 'WorkflowView.vue')
    expect(view, '刷新按钮搬到编辑器头部后，视图必须把 refresh 事件接到 refreshAll').toMatch(
      /@refresh="run\.refreshAll"/,
    )
    expect(view, '这颗按钮只拉详情，点下去张数不变，等于没这个按钮').not.toMatch(/@refresh="run\.refreshInstance"/)
    const header = readRepoFile('frontend', 'src', 'components', 'workflow', 'editor', 'EditorHeader.vue')
    expect(header).toMatch(/@click="emit\('refresh'\)"\s*>\s*刷新实例/)
  })

  /**
   * 队列自查的接线：真机上页面开着 40 秒，接口从 6 条变 7 条而"服务端实例（6）"一动不动。
   * 起表必须挂在挂载上、停表必须挂在卸载上——少后半条，切走页面还在打接口。
   */
  it('挂载起队列自查、卸载停表，两者都接的是 store 导出的方法', () => {
    const view = readRepoFile('frontend', 'src', 'views', 'WorkflowView.vue')
    expect(view).toMatch(/onMounted\(\(\) => \{[\s\S]*?run\.startQueuePolling\(\)/)
    expect(view, '卸载不停表，切到别的页签也一直在打实例接口').toMatch(/onUnmounted\(\(\) => \{[\s\S]*?run\.stopQueuePolling\(\)/)
    const store = readRepoFile('frontend', 'src', 'stores', 'workflow', 'run.ts')
    expect(store).toMatch(/^\s+startQueuePolling,$/m)
    expect(store).toMatch(/^\s+stopQueuePolling,$/m)
    expect(store, '时间戳没人写就永远读不到差别').toMatch(/^\s+instancesUpdatedAt,$/m)
  })

  /**
   * 定义列表自查的接线（第二十八批）：与队列那条同一个理由——
   * 「服务端已存定义」此前只在进页/保存/归档时对账，别人刚存的那一版要刷新整页才看得见。
   */
  it('挂载起定义自查、卸载停表，且定义那一列也挂上了新鲜度条', () => {
    const view = readRepoFile('frontend', 'src', 'views', 'WorkflowView.vue')
    expect(view).toMatch(/onMounted\(\(\) => \{[\s\S]*?catalog\.startDefinitionPolling\(\)/)
    expect(view, '卸载不停表，切到别的页也一直在打定义接口').toMatch(/onUnmounted\(\(\) => \{[\s\S]*?catalog\.stopDefinitionPolling\(\)/)
    const store = readRepoFile('frontend', 'src', 'stores', 'workflow', 'catalog.ts')
    expect(store).toMatch(/^\s+startDefinitionPolling,$/m)
    expect(store).toMatch(/^\s+stopDefinitionPolling,$/m)
    expect(store, '时间戳没人写就永远读不到差别').toMatch(/^\s+definitionsUpdatedAt,$/m)
    const inspector = readRepoFile('frontend', 'src', 'components', 'workflow', 'DefinitionInspector.vue')
    expect(inspector, '自己刷了却不说什么时刻取的数，读的人仍分不清"没有新的"与"这一列是旧的"').toMatch(
      /<FreshnessBar[\s\S]*?:fetched-at="catalog\.definitionsUpdatedAt"/,
    )
  })

  it('store 真的导出了 refreshAll：视图取用未导出的方法，运行时就是 undefined', () => {
    const store = readRepoFile('frontend', 'src', 'stores', 'workflow', 'run.ts')
    expect(store).toMatch(/^\s+refreshAll,$/m)
  })

  it('运行中三个会改变「等人签」张数的动作都补了列表对账', () => {
    const source = readRepoFile('frontend', 'src', 'stores', 'workflow', 'run.ts')
    for (const action of ['submitDecision', 'bypassNode', 'abortInstance']) {
      const start = source.indexOf(`async function ${action}(`)
      expect(start, `${action} 的实现找不到`).toBeGreaterThan(-1)
      expect(source.slice(start, start + 1_200), `${action} 之后要把实例列表重拉一遍`).toContain('syncInstancesQuietly()')
    }
  })
})

/**
 * 值班员在流程页的第一眼：等签队列在右栏第三块、1440×900 首屏之外
 * （真机量到 queueVisibleWithoutScroll=false）。画布上方摆一条待办条，
 * 点它打开列表首行那张——打开后 focusInstance 自动选中待签节点，决策面板随之就位。
 */
describe('编排页画布上方的待办条', () => {
  it('只在这一页真有等签工单时摆出，动作接 run.openFirstWaiting', () => {
    const view = readRepoFile('frontend', 'src', 'views', 'WorkflowView.vue')
    expect(view, '待办条必须挂在等人签数量上，不能常驻占着画布上方').toMatch(/v-if="waitingTickets\.length > 0"/)
    expect(view).toMatch(/data-testid="waiting-banner"/)
    expect(view).toMatch(/@click="run\.openFirstWaiting"\s*>\s*打开第一张/)
  })

  it('store 真的导出了 openFirstWaiting：模板取用未导出的方法，运行时就是 undefined', () => {
    const store = readRepoFile('frontend', 'src', 'stores', 'workflow', 'run.ts')
    expect(store).toMatch(/^\s+openFirstWaiting,$/m)
  })
})

/**
 * 试运行与正式运行的界面分流：一次演练与一次真处置，按钮、弹窗文案和横幅
 * 必须从第一眼就能区分，否则"跑了一遍"会被读成"处置过了"。
 */
describe('RuntimePanel 试运行与正式运行分流', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  function draftWithLineage(): EditorContext {
    const context = editorContext()
    context.document.resetDocument('链路')
    context.document.addNode({ ...createNodeDef('start', 'start_1'), config: { variables: [] } })
    context.document.document = { ...context.document.document!, lineage_id: 'wf_0123456789ab' }
    return context
  }

  it('空白画布没有 lineage 时试运行按钮不可用', () => {
    const context = editorContext()
    context.document.resetDocument('空白')
    const wrapper = mount(RuntimePanel, { global: withEditor(context) })
    expect(wrapper.find('[data-testid="preview-start"]').attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-testid="preview-start"]').attributes('title')).toContain('lineage')
  })

  it('实例模式两个启动口都关着，并给出返回草稿的出口', () => {
    const context = draftWithLineage()
    context.editor.setMode({ kind: 'instance', instanceId: 'wfi_1', readOnly: true })
    const wrapper = mount(RuntimePanel, { global: withEditor(context) })
    expect(wrapper.find('[data-testid="preview-start"]').attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-testid="instance-start"]').attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-testid="exit-instance"]').exists()).toBe(true)
  })

  it('点试运行先开输入弹窗，不直接发请求', async () => {
    const context = draftWithLineage()
    const preview = vi.spyOn(context.run, 'startPreview')
    const wrapper = mount(RuntimePanel, {
      global: { ...withEditor(context), stubs: { Teleport: true } },
    })
    await wrapper.find('[data-testid="preview-start"]').trigger('click')
    expect(wrapper.find('[data-testid="run-input-dialog"]').exists(), '输入弹窗没有出现').toBe(true)
    expect(wrapper.text()).toContain('试运行（不产生外部副作用）')
    expect(preview).not.toHaveBeenCalled()
  })

  it('试运行结果旁边写明没有外部副作用，清除后画布不再叠状态', async () => {
    const context = draftWithLineage()
    const wrapper = mount(RuntimePanel, { global: withEditor(context) })
    // 先让挂载时那趟 document 监听跑完，再摆结果；真实路径里结果由 startPreview 一并写入签名。
    await wrapper.vm.$nextTick()
    context.run.preview = detail({
      instance_id: 'pvp_1',
      preview: true,
      preview_id: 'pvp_1',
      status: 'succeeded',
      nodes: [
        {
          node_id: 'start_1',
          type: 'start',
          state: 'succeeded',
          attempts: 1,
          schedule_latency_ms: 0,
          duration_ms: 2,
          output: {},
          error: null,
          notes: [],
        },
      ],
    })
    await wrapper.vm.$nextTick()
    const banner = wrapper.find('[data-testid="preview-banner"]')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain('没有写入任何持久面')
    expect(banner.text()).toContain('没有产生外部副作用')

    await banner.find('button').trigger('click')
    expect(context.run.preview).toBeNull()
  })

  it('正式启动入口说明跑的是已发布版本', () => {
    const context = draftWithLineage()
    context.document.versions = [
      {
        workflow_id: 'wf_0123456789cd',
        lineage_id: 'wf_0123456789ab',
        version: 3,
        name: '链路',
        description: '',
        status: 'active',
        node_count: 1,
        edge_count: 0,
        created_at: '2026-10-22T00:00:00Z',
        content_hash: 'sha256:abc',
        is_current: true,
      },
    ]
    const wrapper = mount(RuntimePanel, { global: withEditor(context) })
    expect(wrapper.text()).toContain('将执行已发布的 v3')
    expect(wrapper.find('[data-testid="instance-start"]').attributes('disabled')).toBeUndefined()
  })
})

describe('RunInputDialog：运行输入', () => {
  function contextWithStart(variables: JsonValue[]): EditorContext {
    const context = editorContext()
    context.document.resetDocument('链路')
    context.document.addNode({ ...createNodeDef('start', 'start_1'), config: { variables } })
    return context
  }

  function mountDialog(context: EditorContext, mode: 'preview' | 'start' = 'preview') {
    return mount(RunInputDialog, { global: withEditor(context), props: { mode, busy: false } })
  }

  const VARIABLES: JsonValue[] = [
    { name: 'alert', label: '预警文本', type: 'string', required: true },
    { name: 'level', type: 'number', default: 3 },
    { name: 'urgent', type: 'boolean' },
    { name: 'meta', type: 'json' },
  ]

  it('Start 变量生成表单：默认值、类型转换与空白可选键都按语义处理', async () => {
    const wrapper = mountDialog(contextWithStart(VARIABLES))
    expect(wrapper.text()).toContain('预警文本')
    expect(wrapper.text()).toContain('alert')
    expect((wrapper.find('[data-testid="run-input-level"]').element as HTMLInputElement).value).toBe('3')

    await wrapper.find('[data-testid="run-input-alert"]').setValue('沟道泥位抬升')
    await wrapper.find('[data-testid="run-input-urgent"]').setValue('true')
    await wrapper.find('[data-testid="run-input-meta"]').setValue('{"station":"S-1"}')
    await wrapper.find('[data-testid="run-input-submit"]').trigger('click')

    expect(wrapper.emitted('submit')?.[0]?.[0]).toEqual({
      alert: '沟道泥位抬升',
      level: 3,
      urgent: true,
      meta: { station: 'S-1' },
    })
  })

  it('必填为空时挡在弹窗里，不发载荷', async () => {
    const wrapper = mountDialog(contextWithStart(VARIABLES))
    await wrapper.find('[data-testid="run-input-submit"]').trigger('click')
    expect(wrapper.emitted('submit')).toBeUndefined()
    expect(wrapper.find('[data-testid="run-input-error"]').text()).toContain('变量 alert 必填')
  })

  it('数字填错当场点名，不把 NaN 送进引擎', async () => {
    const wrapper = mountDialog(contextWithStart(VARIABLES))
    await wrapper.find('[data-testid="run-input-alert"]').setValue('有险情')
    await wrapper.find('[data-testid="run-input-level"]').setValue('三级')
    await wrapper.find('[data-testid="run-input-submit"]').trigger('click')
    expect(wrapper.emitted('submit')).toBeUndefined()
    expect(wrapper.find('[data-testid="run-input-error"]').text()).toContain('变量 level 应为数字')
  })

  it('旧定义 JSON 一档仍可贴任意对象；数组与非对象被拒', async () => {
    const wrapper = mountDialog(contextWithStart([]))
    await wrapper.find('[data-testid="input-tab-json"]').trigger('click')
    expect(wrapper.find('.wf-runinput__empty').exists()).toBe(false)

    await wrapper.find('[data-testid="run-input-json"]').setValue('[1, 2]')
    await wrapper.find('[data-testid="run-input-submit"]').trigger('click')
    expect(wrapper.find('[data-testid="run-input-error"]').text()).toContain('JSON 必须是对象')

    await wrapper.find('[data-testid="run-input-json"]').setValue('{"alert":"有险情"}')
    await wrapper.find('[data-testid="run-input-submit"]').trigger('click')
    expect(wrapper.emitted('submit')?.[0]?.[0]).toEqual({ alert: '有险情' })
  })

  it('两档标题各自说清跑的是什么、会不会产生外部副作用', () => {
    const preview = mountDialog(contextWithStart([]), 'preview')
    expect(preview.text()).toContain('不产生外部副作用')
    expect(preview.text()).toContain('当前画布草稿')
    const formal = mountDialog(contextWithStart([]), 'start')
    expect(formal.text()).toContain('真实副作用')
    expect(formal.text()).toContain('已发布版本')
    expect(formal.find('[data-testid="run-input-submit"]').text()).toBe('正式启动')
  })
})
