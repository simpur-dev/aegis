import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'

import ConfigFields from '@/components/workflow/ConfigFields.vue'
import JsonField from '@/components/workflow/JsonField.vue'
import NodeInspector from '@/components/workflow/NodeInspector.vue'
import NodeRefPicker from '@/components/workflow/NodeRefPicker.vue'
import RuleEditor from '@/components/workflow/RuleEditor.vue'
import StartVariablesEditor from '@/components/workflow/StartVariablesEditor.vue'
import TemplateField from '@/components/workflow/TemplateField.vue'
import ValidationList from '@/components/workflow/ValidationList.vue'
import VariableListEditor from '@/components/workflow/VariableListEditor.vue'
import VariablePicker from '@/components/workflow/VariablePicker.vue'
import {
  COMPARE_OPS,
  MAX_START_VARIABLES,
  THRESHOLD_AGGS,
  inspectVariableReferences,
  nodeConfigIssues,
  referenceIssues,
  variableReferences,
} from '@/components/workflow/configValidation'
import { EDITOR_CONTEXT_KEY, createEditorContext, type EditorContext } from '@/components/workflow/editor/context'
import type { UpstreamVariableGroup } from '@/composables/workflow/useUpstreamVariables'
import type { NodeTypeEntry } from '@/api/workflow/contract'
import { NODE_META, type FieldKind, type NodeRefCandidate } from '@/components/workflow/registry'
import { NODE_SPECS, NODE_TYPES, edgeId, validateGraph, type JsonValue, type NodeDef, type NodeType } from '@/utils/graph'

/**
 * 批次 D3：19 类节点的专属表单。
 *
 * 这批用例的落点不是"组件长什么样"，而是**每一格能不能把后端会拒的东西当场说清楚**：
 * 必填、上下限、算子表外、引用了没连线的上游、变量敲错一个字母——
 * 这五类过去都是"画布说合法、发布/运行才炸"，人拿到的是延迟一次的错误。
 */

const SUPPORTED: FieldKind[] = [
  'text',
  'template',
  'number',
  'select',
  'string_list',
  'json',
  'node_ref',
  'node_ref_list',
  'rules',
  'conditions',
  'start_variables',
]

/** 多数用例要从"这一格是空的"开始，所以不套默认配置。 */
function bareDef(type: NodeType, config: Record<string, JsonValue> = {}, nodeId = 'n_1'): NodeDef {
  return {
    node_id: nodeId,
    type,
    name: NODE_META[type].label,
    config,
    sla_ms: 5_000,
    timeout_ms: 5_000,
    on_failure: 'retry',
    retry: { max_attempts: 1, backoff_ms: 200 },
  }
}

/** 与面板拖上来时同一份默认配置：体检用例要判的是"刚拖出来能不能存"。 */
function createDef(type: NodeType, nodeId: string): NodeDef {
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

function mountFields(
  def: NodeDef,
  options: { candidates?: readonly NodeRefCandidate[]; variables?: readonly UpstreamVariableGroup[] } = {},
) {
  return mount(ConfigFields, {
    props: {
      def,
      disabled: false,
      candidates: options.candidates ?? [],
      variables: options.variables ?? [],
    },
  })
}

const errorOf = (wrapper: ReturnType<typeof mount>, key: string): string =>
  wrapper.find(`[data-testid="error-${key}"]`).text()

const group = (nodeId: string, nodeType: string, keys: string[]) => ({ nodeId, nodeType, keys })

describe('19 类表单的覆盖面', () => {
  it('后端的每一类节点都带着字段清单进来，形态全部落在受支持的编辑器上', () => {
    expect(NODE_TYPES).toHaveLength(19)
    for (const type of NODE_TYPES) {
      const meta = NODE_META[type]
      for (const field of meta.fields) {
        expect(SUPPORTED, `${type}.${field.key} 的形态 ${field.kind} 没有对应编辑器`).toContain(field.kind)
        expect(field.label, `${type}.${field.key} 没有中文名`).not.toBe('')
      }
    }
  })

  it('面板字段与后端配置白名单逐类一一对应：不缺一格，也不多一格', () => {
    /**
     * 少一格 = 存量定义里那个键只能在 JSON 里改；多一格 = 写进去必吃后端的"未知参数"。
     * 两个方向都是"画布能填但存不下"，所以按集合相等判，而不是按包含判。
     */
    for (const type of NODE_TYPES) {
      const spec = NODE_SPECS[type]
      const allowed = [...spec.required_config, ...spec.optional_config].sort()
      expect(NODE_META[type].fields.map((field) => field.key).sort(), `${type} 的字段与白名单不一致`).toEqual(allowed)
    }
  })

  it('每类的默认配置都通过体检：拖上来即可保存，不会第一秒就一片红', () => {
    for (const type of NODE_TYPES) {
      const issues = nodeConfigIssues(createDef(type, `${type}_1`), { inbound: [] })
      expect(issues, `${type} 默认配置不自洽: ${issues.map((i) => i.message).join('；')}`).toEqual([])
    }
  })

  it('ConfigFields 按形态把每一格渲染成对应编辑器', () => {
    for (const type of NODE_TYPES) {
      const def = createDef(type, `${type}_1`)
      const wrapper = mountFields(def)
      const fields = NODE_META[type].fields
      if (fields.length === 0) {
        expect(wrapper.text()).toContain('该节点类型无参数')
        continue
      }
      for (const field of fields) {
        expect(wrapper.find(`[data-testid="field-${field.key}"]`).exists(), `${type}.${field.key} 没渲染出来`).toBe(true)
      }
      for (const field of fields) {
        const host = wrapper.find(`[data-testid="field-${field.key}"]`)
        switch (field.kind) {
          case 'json':
            expect(host.findComponent(JsonField).exists(), `${type}.extra 不是 JSON 编辑器`).toBe(true)
            break
          case 'rules':
          case 'conditions':
            expect(host.findComponent(RuleEditor).props('kind')).toBe(field.kind)
            break
          case 'start_variables':
            expect(host.findComponent(StartVariablesEditor).exists()).toBe(true)
            break
          case 'node_ref':
          case 'node_ref_list':
            expect(host.findComponent(NodeRefPicker).props('multiple')).toBe(field.kind === 'node_ref_list')
            break
          case 'string_list':
            expect(host.findComponent(VariableListEditor).exists()).toBe(true)
            break
          case 'template':
            expect(host.findComponent(TemplateField).exists()).toBe(true)
            break
          case 'select':
            expect(host.find(`select[aria-label="${field.label}"]`).exists(), `${type}.${field.key} 不是下拉`).toBe(true)
            break
          case 'number':
            expect(host.find(`input[type="number"][aria-label="${field.label}"]`).exists()).toBe(true)
            break
          case 'text':
            expect(host.find(`input[type="text"][aria-label="${field.label}"]`).exists()).toBe(true)
            break
        }
      }
    }
  })

  it('字段清单里的 select 候选非空，number 的上下界成对写清', () => {
    for (const type of NODE_TYPES) {
      for (const field of NODE_META[type].fields) {
        if (field.kind === 'select') {
          expect((field.options ?? []).length, `${type}.${field.key} 的下拉没有候选`).toBeGreaterThan(0)
        }
        if (field.kind === 'number' && field.max !== undefined) {
          expect(field.min, `${type}.${field.key} 只写了上限没写下限`).toBeTypeOf('number')
        }
      }
    }
  })
})

describe('必填 / 最小值 / 最大值就地可见', () => {
  it('通报内容清空后，必填直接落在那一格下面', () => {
    const wrapper = mountFields(bareDef('notify', { level: 'info' }))
    expect(errorOf(wrapper, 'text')).toContain('必填')
    const input = wrapper.findComponent(TemplateField)
    expect(input.props('invalid')).toBe(true)
    expect(wrapper.find('[data-testid="error-text"]').attributes('role')).toBe('alert')
    /* 真机上量到的重复红字：编辑器自己说一遍、外层又说一遍。一句话只许出现一次。 */
    expect(wrapper.text().split('是必填项').length - 1).toBe(1)
  })

  it('阈值条件被删成缺失时同样点名必填', () => {
    const wrapper = mountFields(bareDef('threshold', { mode: 'any' }))
    expect(errorOf(wrapper, 'conditions')).toContain('必填')
  })

  it('降级等级越界：低于后端声明的下限、高于面板声明的上限，各说一次', () => {
    expect(nodeConfigIssues(bareDef('degrade_to_rule', { risk_level: 0 }))
      .map((issue) => issue.message)
      .join('；'))
      .toContain('不得小于 1')
    const tooHigh = mountFields(bareDef('degrade_to_rule', { risk_level: 6 }))
    expect(errorOf(tooHigh, 'risk_level')).toContain('不得超过 5')
  })

  it('同一句话不重复摆两遍（后端下限与面板下限撞在一起时）', () => {
    const issues = nodeConfigIssues(bareDef('degrade_to_rule', { risk_level: 0 }))
    const messages = issues.map((issue) => issue.message)
    expect(messages).toHaveLength(new Set(messages).size)
  })

  it('延时秒数超过运行期真会等的上限就说出来：填 60 也只会等 30', () => {
    const wrapper = mountFields(bareDef('delay', { seconds: 60 }))
    expect(errorOf(wrapper, 'seconds')).toContain('不得超过 30')
  })

  it('数字格塞了文字、下拉塞了表外取值，各自点名', () => {
    const wrapper = mountFields(bareDef('data_fetch', { limit: '200' }))
    expect(errorOf(wrapper, 'limit')).toContain('要填数字')
    const mode = mountFields(bareDef('threshold', { conditions: [], mode: 'ANY' }))
    expect(errorOf(mode, 'mode')).toContain('any / all')
  })

  it('白名单外的参数不能悄悄留在配置里', () => {
    const wrapper = mountFields(bareDef('notify', { text: 'ok', ghost: 1 }))
    expect(wrapper.find('[data-testid="stray-issues"]').text()).toContain('参数「ghost」不在 notify 的配置白名单里')
  })

  it('下拉留空等于删键：回落到后端默认，不写一个空字符串进去', async () => {
    const wrapper = mountFields(createDef('threshold', 't_1'))
    const select = wrapper.find('select[aria-label="判定模式"]')
    expect((select.element as HTMLSelectElement).value).toBe('any')
    await select.setValue('')
    const emitted = wrapper.emitted('update:config')?.at(-1)?.[0] as Record<string, JsonValue>
    expect('mode' in emitted).toBe(false)
  })
})

describe('NodeRefPicker：候选只有真实入边', () => {
  const candidates: NodeRefCandidate[] = [
    { id: 'fetch_1', label: '取数', type: 'data_fetch' },
    { id: 'fetch_2', label: '补数', type: 'data_fetch' },
  ]

  it('单值下拉里只有入边，选完写出节点 ID', async () => {
    const wrapper = mount(NodeRefPicker, { props: { multiple: false, modelValue: null, candidates, disabled: false } })
    const options = wrapper.findAll('option').map((option) => option.text())
    expect(options[0]).toContain('不指定')
    expect(options).toContain('取数 · fetch_1')
    await wrapper.find('select').setValue('fetch_2')
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual(['fetch_2'])
  })

  it('选了没连线的节点：面板上标出"没有连线"，体检里点名缺哪条边', () => {
    const wrapper = mount(NodeRefPicker, { props: { multiple: false, modelValue: 'ghost', candidates, disabled: false } })
    expect(wrapper.text()).toContain('ghost（没有连线）')
    const issues = nodeConfigIssues(bareDef('threshold', { conditions: [], upstream: 'ghost' }, 'judge_1'), {
      inbound: ['fetch_1'],
    })
    const message = issues.map((issue) => issue.message).join('；')
    expect(message).toContain('ghost → judge_1')
    expect(message).toContain('当前入边：fetch_1')
  })

  it('多值勾选：加一项、去一项都写回列表', async () => {
    const wrapper = mount(NodeRefPicker, {
      props: { multiple: true, modelValue: ['fetch_1'], candidates, disabled: false },
    })
    await wrapper.find('[data-testid="ref-fetch_2"]').setValue(true)
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual([['fetch_1', 'fetch_2']])
    // 勾选之后要把新值喂回去：真实父组件（store）会立刻重渲染，组件本身不持有状态。
    await wrapper.setProps({ modelValue: ['fetch_1', 'fetch_2'] })
    await wrapper.find('[data-testid="ref-fetch_1"]').setValue(false)
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual([['fetch_2']])
  })

  it('多值里的失效引用照样摆出来并可取消，不静默替人改参数', async () => {
    const wrapper = mount(NodeRefPicker, {
      props: { multiple: true, modelValue: ['ghost_1', 'fetch_1'], candidates: [candidates[0]], disabled: false },
    })
    expect(wrapper.text()).toContain('没有连线')
    const stale = wrapper.findAll('li').find((row) => row.text().includes('ghost_1'))
    await stale?.find('input').setValue(false)
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual([['fetch_1']])
  })

  it('一颗入边都没有时给出下一步动作，而不是留一个空框', () => {
    const wrapper = mount(NodeRefPicker, { props: { multiple: true, modelValue: [], candidates: [], disabled: false } })
    expect(wrapper.text()).toContain('先在画布上连线')
  })
})

describe('RuleEditor：分支规则与阈值条件', () => {
  const branchRow = { when: 'risk_level', op: '<=', value: 2, then: 'traffic_check' }

  it('按行渲染分支规则，算子下拉只给后端认识的那几个', () => {
    const wrapper = mount(RuleEditor, { props: { kind: 'rules', modelValue: [branchRow], disabled: false } })
    expect(wrapper.find<HTMLInputElement>('[data-testid="rule-when-0"]').element.value).toBe('risk_level')
    expect(wrapper.find<HTMLInputElement>('[data-testid="rule-value-0"]').element.value).toBe('2')
    const ops = wrapper.findAll('[data-testid="rule-op-0"] option').map((option) => option.text())
    expect(ops).toEqual(COMPARE_OPS)
  })

  it('比较值保住类型：2 是数字、true 是布尔、sms 才是字符串', async () => {
    const wrapper = mount(RuleEditor, { props: { kind: 'rules', modelValue: [branchRow], disabled: false } })
    const cell = wrapper.find('[data-testid="rule-value-0"]')
    await cell.setValue('3')
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual([[{ ...branchRow, value: 3 }]])
    await cell.setValue('true')
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual([[{ ...branchRow, value: true }]])
    await cell.setValue('sms')
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual([[{ ...branchRow, value: 'sms' }]])
  })

  it('改一格不丢另一格：规则里本编辑器不管的键原样留着', async () => {
    const row = { ...branchRow, note: '现场口径' }
    const wrapper = mount(RuleEditor, { props: { kind: 'rules', modelValue: [row], disabled: false } })
    await wrapper.find('[data-testid="rule-then-0"]').setValue('slow_down')
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual([[{ ...row, then: 'slow_down' }]])
  })

  it('新增一条给的是可编辑的空行，删除一条只删那一条', async () => {
    const wrapper = mount(RuleEditor, {
      props: { kind: 'rules', modelValue: [branchRow, { ...branchRow, then: 'other' }], disabled: false },
    })
    await wrapper.find('[data-testid="rule-add-rules"]').trigger('click')
    const added = wrapper.emitted('update:modelValue')?.at(-1)?.[0] as Record<string, JsonValue>[]
    expect(added).toHaveLength(3)
    expect(added[2]).toEqual({ when: '', op: '==', value: '', then: '' })

    await wrapper.setProps({ modelValue: added })
    await wrapper.find('[data-testid="rule-remove-0"]').trigger('click')
    const removed = wrapper.emitted('update:modelValue')?.at(-1)?.[0] as Record<string, JsonValue>[]
    expect(removed.map((row) => row.then)).toEqual(['other', ''])
  })

  it('阈值条件用指标/阈值/聚合三列，聚合候选与后端一致', async () => {
    const wrapper = mount(RuleEditor, {
      props: { kind: 'conditions', modelValue: [{ metric: 'rain_10min', op: '>=', threshold: 30, agg: 'max' }], disabled: false },
    })
    expect(wrapper.find<HTMLInputElement>('[data-testid="condition-metric-0"]').element.value).toBe('rain_10min')
    expect(wrapper.find<HTMLInputElement>('[data-testid="condition-threshold-0"]').element.value).toBe('30')
    const aggs = wrapper.findAll('[data-testid="condition-agg-0"] option').map((option) => option.text())
    expect(aggs).toEqual(THRESHOLD_AGGS)
    await wrapper.find('[data-testid="condition-threshold-0"]').setValue('50')
    const rows = wrapper.emitted('update:modelValue')?.at(-1)?.[0] as Record<string, JsonValue>[]
    expect(rows).toEqual([{ metric: 'rain_10min', op: '>=', threshold: 50, agg: 'max' }])
  })

  it('规则写残了当场说出来：缺路径、缺分支名、算子表外、阈值不是数字', () => {
    const rules = nodeConfigIssues(
      bareDef('branch', { rules: [{ when: '', op: '~=', then: '' }] }),
      {},
    ).map((issue) => issue.message)
    expect(rules.join('；')).toContain('第 1 条规则缺少取值路径')
    expect(rules.join('；')).toContain('算子只能是')
    expect(rules.join('；')).toContain('第 1 条规则缺少比较值')
    expect(rules.join('；')).toContain('第 1 条规则没有写出分支名')
    const conditions = nodeConfigIssues(bareDef('threshold', { conditions: [{ metric: 'x', op: '>=', threshold: 'y' }] }))
      .map((issue) => issue.message)
      .join('；')
    expect(conditions).toContain('阈值要填数字')
  })

  it('规则表整体不是列表时也要说，而不是抛异常把面板弄没了', () => {
    expect(nodeConfigIssues(bareDef('branch', { rules: 'nope' })).map((i) => i.message).join()).toContain('规则列表')
    expect(nodeConfigIssues(bareDef('threshold', { conditions: 5 })).map((i) => i.message).join()).toContain('条件列表')
  })
})

describe('Start 变量表', () => {
  it('一行一个变量，名字/标签/类型/必填都能改，其余键原样保留', async () => {
    const wrapper = mount(StartVariablesEditor, {
      props: {
        modelValue: [{ name: 'region_code', type: 'string', required: true, default: '540121' }],
        disabled: false,
      },
    })
    expect(wrapper.find<HTMLInputElement>('[data-testid="var-name-0"]').element.value).toBe('region_code')
    expect(wrapper.find<HTMLInputElement>('[data-testid="var-required-0"]').element.checked).toBe(true)
    await wrapper.find('[data-testid="var-type-0"]').setValue('number')
    const emitted = wrapper.emitted('update:modelValue')?.at(-1)?.[0] as Record<string, JsonValue>[]
    expect(emitted[0]).toEqual({ name: 'region_code', type: 'number', required: true, default: '540121' })
  })

  it('取消必填是把键删掉，而不是写一个 false 进去', async () => {
    const wrapper = mount(StartVariablesEditor, {
      props: { modelValue: [{ name: 'a', type: 'string', required: true }], disabled: false },
    })
    await wrapper.find('[data-testid="var-required-0"]').setValue(false)
    const emitted = wrapper.emitted('update:modelValue')?.at(-1)?.[0] as Record<string, JsonValue>[]
    expect(emitted[0]).toEqual({ name: 'a', type: 'string' })
  })

  it('新增一行从空名开始，删除一行只删那一行', async () => {
    const wrapper = mount(StartVariablesEditor, { props: { modelValue: [{ name: 'a', type: 'string' }], disabled: false } })
    await wrapper.find('[data-testid="var-add"]').trigger('click')
    const added = wrapper.emitted('update:modelValue')?.at(-1)?.[0] as Record<string, JsonValue>[]
    expect(added).toEqual([{ name: 'a', type: 'string' }, { name: '', type: 'string' }])

    await wrapper.setProps({ modelValue: added })
    await wrapper.find('[data-testid="var-remove-1"]').trigger('click')
    const removed = wrapper.emitted('update:modelValue')?.at(-1)?.[0] as Record<string, JsonValue>[]
    expect(removed).toEqual([{ name: 'a', type: 'string' }])
  })

  it('名字形状、原型链名、重名、超上限、默认值与类型不符，逐条点名', () => {
    const messages = (variables: Record<string, JsonValue>[]): string =>
      nodeConfigIssues(bareDef('start', { variables })).map((issue) => issue.message).join('；')
    expect(messages([{ name: '2bad', type: 'string' }])).toContain('名字不合法')
    expect(messages([{ name: '__proto__', type: 'string' }])).toContain('原型链')
    expect(messages([{ name: 'ok', type: 'string' }, { name: 'ok', type: 'string' }])).toContain('重复')
    expect(messages([{ name: 'ok', type: 'date' }])).toContain('类型只能是')
    expect(messages([{ name: 'ok', type: 'number', default: '540121' }])).toContain('默认值要写成数字')
    const many = Array.from({ length: MAX_START_VARIABLES + 1 }, (_, index) => ({ name: `v${index}`, type: 'string' }))
    expect(messages(many)).toContain(`不得超过 ${MAX_START_VARIABLES} 项`)
  })

  it('变量表不是列表时给出人话，不崩面板', () => {
    expect(nodeConfigIssues(bareDef('start', { variables: 'x' })).map((i) => i.message).join()).toContain('变量列表')
  })
})

describe('列表格与变量选择器', () => {
  it('发布通道一行一项：加、改、删都只动那一行', async () => {
    const wrapper = mount(VariableListEditor, { props: { placeholder: '', modelValue: ['sms'], disabled: false } })
    await wrapper.find('[data-testid="list-add"]').trigger('click')
    await wrapper.find('[data-testid="list-draft"]').setValue('broadcast')
    await wrapper.find('[data-testid="list-draft"]').trigger('keyup', { key: 'Enter' })
    const added = wrapper.emitted('update:modelValue')?.at(-1)?.[0] as string[]
    expect(added).toEqual(['sms', 'broadcast'])

    await wrapper.setProps({ modelValue: added })
    await wrapper.find('[data-testid="list-item-0"]').setValue('beidou')
    const edited = wrapper.emitted('update:modelValue')?.at(-1)?.[0] as string[]
    expect(edited).toEqual(['beidou', 'broadcast'])

    await wrapper.setProps({ modelValue: edited })
    await wrapper.find('[data-testid="list-remove-0"]').trigger('click')
    expect(wrapper.emitted('update:modelValue')?.at(-1)?.[0]).toEqual(['broadcast'])
  })

  it('列表里留了空项就说出来（运行期只认原值，空项等于少一个通道）', () => {
    const issues = nodeConfigIssues(bareDef('human_review', { options: ['approve', ' '] })).map((i) => i.message)
    expect(issues.join('；')).toContain('每一项都要填文字')
  })

  it('变量选择器按上游分组列出输出键，点一下给出完整引用', async () => {
    const wrapper = mount(VariablePicker, {
      props: { groups: [group('fetch_1', 'data_fetch', ['rows', 'count'])], disabled: false },
    })
    expect(wrapper.text()).toContain('fetch_1')
    await wrapper.find('[data-testid="var-fetch_1-rows"]').trigger('click')
    expect(wrapper.emitted('pick')?.at(-1)).toEqual(['{{#fetch_1.rows#}}'])
  })

  it('还没有祖先时说明白为什么是空的，而不是摆一个空框', () => {
    expect(
      mount(VariablePicker, { props: { groups: [], disabled: false } }).text(),
    ).toContain('先在画布上连一条指向它的进边')
  })

  it('搜一下就把不相关的上游收掉', async () => {
    const wrapper = mount(VariablePicker, {
      props: {
        groups: [group('fetch_1', 'data_fetch', ['rows']), group('judge_1', 'threshold', ['triggered'])],
        disabled: false,
      },
    })
    await wrapper.find('[data-testid="variable-search"]').setValue('triggered')
    expect(wrapper.text()).toContain('judge_1')
    expect(wrapper.text()).not.toContain('fetch_1')
  })

  it('引用插到光标处，不是永远追加到末尾', async () => {
    const wrapper = mount(TemplateField, {
      props: {
        label: '通报内容',
        placeholder: '',
        modelValue: '本轮共 条读数',
        disabled: false,
        variables: [group('fetch_1', 'data_fetch', ['count'])],
      },
    })
    const area = wrapper.find('textarea').element as HTMLTextAreaElement
    area.setSelectionRange(3, 3)
    await wrapper.find('[data-testid="variable-toggle"]').trigger('click')
    await wrapper.find('[data-testid="var-fetch_1-count"]').trigger('click')
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual(['本轮共{{#fetch_1.count#}} 条读数'])
    await wrapper.setProps({ modelValue: '本轮共{{#fetch_1.count#}} 条读数' })
    expect(wrapper.text()).toContain('已引用 1 处')
  })

  it('文本格改成空串要落成"删掉这一键"，与后端缺失判定对齐', async () => {
    const wrapper = mount(TemplateField, {
      props: { label: '通报内容', placeholder: '', modelValue: 'x', disabled: false, variables: [] },
    })
    await wrapper.find('textarea').setValue('')
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual([null])
  })
})

describe('变量引用体检', () => {
  it('把 config 里的引用逐只挑出来（同一只写两遍只算一次）', () => {
    expect(variableReferences({ text: '{{#a.rows#}} 与 {{#a.rows#}}', n: 3 })).toEqual(['{{#a.rows#}}'])
  })

  it('祖先引用与合法路径放行', () => {
    expect(inspectVariableReferences({ text: '{{#fetch_1.rows#}}' }, { ancestors: ['fetch_1'] })).toEqual([])
    expect(inspectVariableReferences({ text: '{{#a.b.c.d#}}' }, { ancestors: ['a'] })).toEqual([])
  })

  it('非祖先、原型链段、形状不合法、只点名节点、路径过深各给一句', () => {
    const reasons = (config: Record<string, JsonValue>, ancestors: string[]): string =>
      inspectVariableReferences(config, { ancestors })
        .map((problem) => problem.reason)
        .join('；')
    expect(reasons({ text: '{{#downstream.rows#}}' }, ['fetch_1'])).toContain('不是本节点的上游')
    expect(reasons({ text: '{{#fetch_1.__proto__#}}' }, ['fetch_1'])).toContain('原型链')
    expect(reasons({ text: '{{#fetch_1.constructor#}}' }, ['fetch_1'])).toContain('原型链')
    expect(reasons({ text: '{{#fetch_1.Bad Key#}}' }, ['fetch_1'])).toContain('不合法的段')
    expect(reasons({ text: '{{#fetch_1#}}' }, ['fetch_1'])).toContain('只点名节点取不到值')
    expect(reasons({ text: `{{#a.${Array.from({ length: 9 }, (_, i) => `k${i}`).join('.')}#}}` }, ['a'])).toContain('最多 8 段')
  })

  it('写成 {{#a.b}}（收尾少了 #）不会被当成引用：面板不替人猜', () => {
    expect(variableReferences({ text: '{{#fetch_1.rows}}' })).toEqual([])
  })

  it('引用问题挂到会显示它的那一格上', () => {
    const issues = referenceIssues(bareDef('notify', { text: '上游：{{#ghost.rows#}}' }), { ancestors: ['fetch_1'] })
    expect(issues).toHaveLength(1)
    expect(issues[0]?.key).toBe('text')
    expect(issues[0]?.message).toContain('ghost.rows')
  })
})

describe('参数体检清单本身', () => {
  it('有待修时报数量并走 alert，没有时换成一句"齐全"', () => {
    const dirty = mount(ValidationList, {
      props: { issues: [{ key: 'text', message: '「通报内容」是必填项' }] },
    })
    expect(dirty.find('[data-testid="node-issues"]').text()).toContain('1 处会被发布拦下')
    expect(dirty.find('[data-testid="node-issues"]').text()).toContain('通报内容')
    expect(dirty.find('[role="alert"]').exists()).toBe(true)

    const clean = mount(ValidationList, { props: { issues: [] } })
    expect(clean.find('[data-testid="node-issues"]').exists()).toBe(false)
    expect(clean.find('[data-testid="node-issues-clean"]').text()).toContain('参数齐全')
    expect(clean.find('[role="status"]').exists()).toBe(true)
  })
})

describe('检查器接线：参数体检与真实连线一起走', () => {
  let context: EditorContext

  beforeEach(() => {
    setActivePinia(createPinia())
    context = createEditorContext()
    context.document.autoSaveEnabled = false
  })

  function mountInspector() {
    return mount(NodeInspector, {
      global: {
        provide: { [EDITOR_CONTEXT_KEY]: context } as Record<string, unknown>,
        stubs: { RuntimeActions: true },
      },
    })
  }

  function twoNodeGraph(): void {
    context.document.resetDocument('链路')
    context.document.addNode(createDef('data_fetch', 'fetch_1'))
    context.document.addNode(createDef('notify', 'notify_1'))
    context.document.connect('fetch_1', 'notify_1')
    context.editor.select('notify_1')
  }

  it('变量选择器只给祖先：点一次插入，发布校验仍然通过', async () => {
    twoNodeGraph()
    const wrapper = mountInspector()
    await wrapper.find('[data-testid="variable-toggle"]').trigger('click')
    const picker = wrapper.find('[data-testid="variable-picker"]')
    expect(picker.exists()).toBe(true)
    // 自己不是自己的上游：只有 fetch_1 那一组
    expect(picker.text()).toContain('fetch_1')
    expect(picker.text()).not.toContain('notify_1')
    await wrapper.find('[data-testid="var-fetch_1-rows"]').trigger('click')
    const def = context.document.definition
    if (def === null) throw new Error('画布未初始化')
    expect(def.nodes.find((node) => node.node_id === 'notify_1')?.config.text).toContain('{{#fetch_1.rows#}}')
    expect(validateGraph(def)).toEqual([])
    expect(wrapper.find('[data-testid="node-issues-clean"]').exists()).toBe(true)
  })

  it('服务端目录补上的产出键与上次运行多出来的键都进得了选择器', async () => {
    twoNodeGraph()
    context.catalog.nodeTypes = [
      {
        type: 'data_fetch',
        description: '',
        category: 'io',
        required_config: [],
        optional_config: [],
        min_config: {},
        outputs: ['rows', 'count', 'fresh_extra'],
      } satisfies NodeTypeEntry,
    ]
    context.run.instance = {
      instance_id: 'wfi_0123456789ab',
      workflow_id: 'wf_0123456789ab',
      workflow_version: 1,
      trace_id: 't-1',
      status: 'succeeded',
      error: null,
      payload: {},
      nodes: [
        {
          node_id: 'fetch_1',
          type: 'data_fetch',
          state: 'succeeded',
          attempts: 1,
          schedule_latency_ms: 1,
          duration_ms: 2,
          output: { rows: [], ad_hoc: 1 },
          error: null,
          notes: [],
        },
      ],
    }
    const wrapper = mountInspector()
    await wrapper.find('[data-testid="variable-toggle"]').trigger('click')
    const testids = wrapper.findAll('[data-testid^="var-fetch_1-"]').map((node) => node.attributes('data-testid'))
    expect(testids).toEqual(
      expect.arrayContaining(['var-fetch_1-rows', 'var-fetch_1-count', 'var-fetch_1-fresh_extra', 'var-fetch_1-ad_hoc']),
    )
  })

  it('上游连线被删掉后，参数体检点名缺的是哪条边', async () => {
    context.document.resetDocument('链路')
    context.document.addNode(createDef('data_fetch', 'fetch_1'))
    const judge = createDef('threshold', 'judge_1')
    context.document.addNode({ ...judge, config: { ...judge.config, upstream: 'fetch_1' } })
    context.document.connect('fetch_1', 'judge_1')
    context.editor.select('judge_1')
    const wrapper = mountInspector()
    expect(wrapper.find('[data-testid="node-issues-clean"]').exists()).toBe(true)

    const def = context.document.definition
    if (def === null) throw new Error('画布未初始化')
    const edge = def.edges[0]
    if (edge === undefined) throw new Error('连线没建上')
    context.document.removeEdge(edgeId(0, edge))
    await wrapper.vm.$nextTick()
    const issues = wrapper.find('[data-testid="node-issues"]')
    expect(issues.exists()).toBe(true)
    expect(issues.text()).toContain('参数体检')
    expect(issues.text()).toContain('画布上没有 fetch_1 → judge_1 这条连线')
  })

  it('手敲的错误引用在面板上就说，不等发布', async () => {
    twoNodeGraph()
    context.document.patchNode('notify_1', { config: { text: '见 {{#nobody.rows#}}' } })
    const wrapper = mountInspector()
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[data-testid="error-text"]').text()).toContain('不是本节点的上游')
    expect(wrapper.find('[data-testid="node-issues"]').text()).toContain('nobody.rows')
  })

  it('未知类型仍然只读：一行编辑器都不铺', () => {
    context.document.resetDocument('链路')
    context.document.addNode({ ...createDef('notify', 'future_1'), type: 'future_type' as NodeType })
    context.editor.select('future_1')
    const wrapper = mountInspector()
    expect(wrapper.find('[data-testid="node-issues"]').exists()).toBe(false)
    expect(wrapper.findAll('input')).toHaveLength(0)
    expect(wrapper.text()).toContain('future_type')
  })
})
