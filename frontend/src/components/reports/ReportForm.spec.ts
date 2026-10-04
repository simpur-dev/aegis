import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { ReportOutcomeDto } from '@/api/reports'
import { REPORT_LIMITS, ReportApiError, reportsApi } from '@/api/reports'
import ReportForm from '@/components/reports/ReportForm.vue'

vi.mock('@/api/reports', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/reports')>()
  return { ...actual, reportsApi: { submit: vi.fn() } }
})

const mockedSubmit = vi.mocked(reportsApi.submit)

/** 只替到"表单用到什么"的程度：双向绑定的输入、标签的文案、警示的有无。 */
const STUBS = {
  'a-alert': {
    name: 'AAlert',
    props: ['message', 'description', 'type', 'showIcon', 'banner'],
    template: '<div class="alert">{{ message }}｜{{ description }}</div>',
  },
  'a-form': { name: 'AForm', props: ['layout'], template: '<form class="form"><slot /></form>' },
  'a-form-item': { name: 'AFormItem', props: ['label'], template: '<div class="form-item"><span class="label">{{ label }}</span><slot /></div>' },
  'a-row': { name: 'ARow', props: ['gutter'], template: '<div class="row"><slot /></div>' },
  'a-col': { name: 'ACol', props: ['span'], template: '<div class="col"><slot /></div>' },
  'a-space': { name: 'ASpace', props: ['size', 'wrap'], template: '<div class="space"><slot /></div>' },
  'a-tag': { name: 'ATag', props: ['color'], template: '<span class="tag" :data-color="color"><slot /></span>' },
  'a-button': {
    name: 'AButton',
    props: ['loading', 'type', 'disabled'],
    template: '<button class="button" :disabled="disabled"><slot /></button>',
  },
  'a-input': {
    name: 'AInput',
    props: ['value', 'placeholder'],
    emits: ['update:value'],
    template: '<input class="input" :value="value" @input="$emit(\'update:value\', $event.target.value)" />',
  },
  'a-textarea': {
    name: 'ATextarea',
    props: ['value', 'rows'],
    emits: ['update:value'],
    template: '<textarea class="textarea" :value="value" @input="$emit(\'update:value\', $event.target.value)"></textarea>',
  },
  'a-input-number': {
    name: 'AInputNumber',
    props: ['value', 'step'],
    emits: ['update:value'],
    template:
      '<input class="number" type="number" :value="value" @input="$emit(\'update:value\', $event.target.value === \'\' ? null : Number($event.target.value))" />',
  },
}

function outcome(overrides: Partial<ReportOutcomeDto> = {}): ReportOutcomeDto {
  return {
    report: { reporter: '巡护员扎西', region_code: '540121', location: null },
    parse: {
      text: '24小时累计降雨95毫米，沟道泥位抬升1.2米',
      hazard_type: 'debris_flow',
      region_code: '540121',
      risk_level: 1,
      confidence: 0.93,
      decided_by: 'rule',
      needs_review: false,
      entities: [],
      metrics: [],
      reported_people: 300,
      trigger_hits: [],
      legs: [
        { leg: 'rule', hazard_type: 'debris_flow', risk_level: 1, confidence: 0.93, rationale: '降雨阈值命中 R-RAIN-1', refs: [], used: true },
        { leg: 'rule_declared', hazard_type: 'debris_flow', risk_level: null, confidence: 0, rationale: '上报文本未写明等级', refs: [], used: false },
        { leg: 'retrieval', hazard_type: 'debris_flow', risk_level: null, confidence: 0.2, rationale: '同灾种佐证 2 条', refs: [], used: false },
        { leg: 'llm', hazard_type: null, risk_level: null, confidence: 0, rationale: 'LLM 未配置，裁决腿缺席', refs: [], used: false },
      ],
      conflicts: [],
      degradations: [],
      evidence: [],
    },
    human_review_required: false,
    review: null,
    intake_seconds: 0.412,
    chain: {
      trace_id: 'tr_1',
      event_id: 'ev_1',
      ok: true,
      acted: true,
      stages: [{ name: 'perceive', mode: 'local', ok: true, latency_ms: 1, note: '上报文本判定（非遥测读数）' }],
      risk: null,
      task_units: ['stu_1', 'stu_2'],
      warning_id: 'wrn_1',
      errors: [],
      degradations: [],
      reference_cases: [],
      context_docs: [],
    },
    ...overrides,
  }
}

async function renderForm(props: Record<string, string> = {}) {
  const wrapper = mount(ReportForm, { props, global: { stubs: STUBS } })
  await flushPromises()
  return wrapper
}

function byTestid(wrapper: ReturnType<typeof mount>, id: string) {
  return wrapper.find(`[data-testid="${id}"]`)
}

async function fill(wrapper: ReturnType<typeof mount>, values: Record<string, string>) {
  for (const [testid, value] of Object.entries(values)) {
    await wrapper.find(`[data-testid="${testid}"]`).setValue(value)
  }
}

beforeEach(() => {
  mockedSubmit.mockReset()
})

describe('上报表单发出去的就是后端声明过的那几个键', () => {
  it('空白提示与空坐标不带键', async () => {
    const wrapper = await renderForm()
    await fill(wrapper, { 'field-reporter': '巡护员扎西', 'field-region': '540121', 'field-note': '沟道泥位抬升 1.2 米' })
    mockedSubmit.mockResolvedValue(outcome())
    await byTestid(wrapper, 'report-submit').trigger('click')
    await flushPromises()
    expect(mockedSubmit).toHaveBeenCalledWith({ reporter: '巡护员扎西', region_code: '540121', note: '沟道泥位抬升 1.2 米' })
  })

  it('灾种提示有值才带，经纬度成对才带', async () => {
    const wrapper = await renderForm()
    await fill(wrapper, {
      'field-reporter': '村民',
      'field-region': '540200',
      'field-note': '发生泥石流，请求红色预警',
      'field-hazard-hint': '泥石流',
      'field-lat': '29.65',
      'field-lon': '91.13',
    })
    mockedSubmit.mockResolvedValue(outcome())
    await byTestid(wrapper, 'report-submit').trigger('click')
    await flushPromises()
    expect(mockedSubmit).toHaveBeenCalledWith({
      reporter: '村民',
      region_code: '540200',
      note: '发生泥石流，请求红色预警',
      hazard_hint: '泥石流',
      lat: 29.65,
      lon: 91.13,
    })
  })

  it('只填半边坐标时如实说明"后端按未定位处理"，而不是悄悄丢掉', async () => {
    const wrapper = await renderForm()
    await fill(wrapper, { 'field-reporter': '村民', 'field-region': '540200', 'field-note': '发生泥石流迹象', 'field-lat': '29.65' })
    mockedSubmit.mockResolvedValue(outcome())
    expect(byTestid(wrapper, 'coordinate-warning').text()).toContain('按「未定位」处理')
    await byTestid(wrapper, 'report-submit').trigger('click')
    await flushPromises()
    const sent = mockedSubmit.mock.calls[0]?.[0] as unknown as Record<string, unknown>
    expect(sent).not.toHaveProperty('lat')
    expect(sent).not.toHaveProperty('lon')
  })

  it('初始区划与名义由外层页面带进来（监测页当前筛选、助手页当前名义）', async () => {
    const wrapper = await renderForm({ initialRegionCode: '540121', initialReporter: '值班员' })
    expect((byTestid(wrapper, 'field-region').element as HTMLInputElement).value).toBe('540121')
    expect((byTestid(wrapper, 'field-reporter').element as HTMLInputElement).value).toBe('值班员')
  })

  it('提交成功后把回执抛给上层，并留下链路号', async () => {
    const wrapper = await renderForm()
    await fill(wrapper, { 'field-reporter': '村民', 'field-region': '540121', 'field-note': '坡面裂缝加宽' })
    mockedSubmit.mockResolvedValue(outcome())
    await byTestid(wrapper, 'report-submit').trigger('click')
    await flushPromises()
    expect(wrapper.emitted('submitted')?.[0]?.[0]).toMatchObject({ intake_seconds: 0.412 })
    expect(byTestid(wrapper, 'outcome-chain').text()).toContain('tr_1')
  })
})

describe('回执摊开三路融合的事实', () => {
  async function rendered(seeded: ReportOutcomeDto) {
    const wrapper = await renderForm()
    await fill(wrapper, { 'field-reporter': '村民', 'field-region': '540121', 'field-note': '沟道泥位抬升' })
    mockedSubmit.mockResolvedValue(seeded)
    await byTestid(wrapper, 'report-submit').trigger('click')
    await flushPromises()
    return wrapper
  }

  it('等级 + 置信 + 定级来源 + 核签徽标都在', async () => {
    const wrapper = await rendered(outcome())
    expect(byTestid(wrapper, 'outcome-level').text()).toContain('红色')
    expect(byTestid(wrapper, 'outcome-level').text()).toContain('1 级')
    expect(byTestid(wrapper, 'outcome-confidence').text()).toContain('93%')
    expect(byTestid(wrapper, 'outcome-decided-by').text()).toContain('规则词表（阈值命中）')
    expect(byTestid(wrapper, 'outcome-review').text()).toBe('无需人工核签')
    expect(byTestid(wrapper, 'outcome-hazard').text()).toBe('泥石流')
  })

  it('每一路各自的结论都在，采纳与未采纳分得开', async () => {
    const wrapper = await rendered(outcome())
    expect(wrapper.findAll('[data-testid^="leg-"]')).toHaveLength(4)
    expect(byTestid(wrapper, 'leg-rule').text()).toContain('采纳')
    expect(byTestid(wrapper, 'leg-rule').text()).toContain('降雨阈值命中 R-RAIN-1')
    expect(byTestid(wrapper, 'leg-llm').text()).toContain('未采纳')
    expect(byTestid(wrapper, 'leg-llm').text()).toContain('LLM 未配置，裁决腿缺席')
  })

  it('申报值定级时：来源写"申报"，核签徽标转红，降级说明照后端原文列出来', async () => {
    const seeded = outcome()
    seeded.parse.decided_by = 'rule_declared'
    seeded.parse.confidence = 0.5
    seeded.human_review_required = true
    seeded.parse.degradations = ['等级来自上报人申报值而非阈值命中，转人工核签']
    const wrapper = await rendered(seeded)
    expect(byTestid(wrapper, 'outcome-decided-by').text()).toContain('申报')
    expect(byTestid(wrapper, 'outcome-review').text()).toBe('需人工核签')
    expect(byTestid(wrapper, 'outcome-review').attributes('data-color')).toBe('red')
    expect(byTestid(wrapper, 'outcome-degradations').text()).toContain('等级来自上报人申报值而非阈值命中，转人工核签')
  })

  it('开出了工单就把工单号、待签节点与三态选项显示出来（签字人得知道去哪儿签）', async () => {
    const seeded = outcome()
    seeded.human_review_required = true
    seeded.review = {
      workflow_id: 'wf_review_1',
      instance_id: 'wfi_01abc',
      status: 'waiting',
      pending_node: 'review',
      options: ['approve', 'adjust', 'reject'],
      decision_endpoint: '/api/v1/workflow/instances/{instance_id}/nodes/{node_id}/decision',
    }
    const wrapper = await rendered(seeded)
    const ticket = byTestid(wrapper, 'outcome-review-ticket').text()
    expect(ticket).toContain('wfi_01abc')
    expect(ticket).toContain('待签节点 review')
    expect(ticket).toContain('approve / adjust / reject')
    expect(ticket).toContain('/api/v1/workflow/instances/')
  })

  it('该核签却没开出工单时，明说"没有工单"而不是留一行空白', async () => {
    const seeded = outcome()
    seeded.human_review_required = true
    seeded.review = null
    const wrapper = await rendered(seeded)
    expect(byTestid(wrapper, 'outcome-review-ticket').text()).toContain('没有开出工单')
  })

  it('无需核签时这一行整个不出现（不给值班员制造假待办）', async () => {
    const wrapper = await rendered(outcome())
    expect(wrapper.find('[data-testid="outcome-review-ticket"]').exists()).toBe(false)
  })

  it('三路都没给出等级时显示"未定级"，不显示成 0 级', async () => {
    const seeded = outcome()
    seeded.parse.risk_level = null
    seeded.parse.decided_by = 'none'
    seeded.parse.confidence = 0
    seeded.parse.degradations = ['三路均未产出等级']
    seeded.human_review_required = true
    seeded.chain.warning_id = null
    const wrapper = await rendered(seeded)
    expect(byTestid(wrapper, 'outcome-level').text()).toBe('未定级')
    expect(byTestid(wrapper, 'outcome-confidence').text()).toContain('0%')
    expect(byTestid(wrapper, 'outcome-chain').text()).toContain('预警 未生成')
  })

  it('规则腿与 LLM 腿的分歧原样摊出，不替后端裁决', async () => {
    const seeded = outcome()
    seeded.parse.conflicts = ['等级分歧：采纳 rule 的 1 级 vs LLM 3 级']
    const wrapper = await rendered(seeded)
    expect(byTestid(wrapper, 'outcome-conflicts').text()).toContain('等级分歧：采纳 rule 的 1 级 vs LLM 3 级')
  })
})

describe('后端拒绝时读后端的话', () => {
  /**
   * 填的是本地放行、后端仍可能判死的形状：本地口径只挡"抄得到的那几条"（长度、模式、区间），
   * 枚举与在册清单这类判据在后端，422 照样会回来——这条测的就是回来之后怎么说。
   */
  it('422 显示后端点名的字段与原文，不换成前端自己的措辞', async () => {
    const wrapper = await renderForm()
    await fill(wrapper, { 'field-reporter': '村民', 'field-region': '540121', 'field-hazard-hint': '泥石流', 'field-note': '沟道泥位抬升 1.2 米' })
    mockedSubmit.mockRejectedValue(
      new ReportApiError(422, 'Request failed with status code 422', {
        detail: [{ loc: ['body', 'hazard_hint'], msg: "Input should be 'debris_flow' or 'flood'", type: 'enum' }],
      }),
    )
    await byTestid(wrapper, 'report-submit').trigger('click')
    await flushPromises()
    const shown = byTestid(wrapper, 'report-error').text()
    // 规则原文照抄（那是后端口径），字段名换成页面上的叫法：值班员不必先做一次中英对照
    expect(shown).toContain("灾种提示（hazard_hint）：Input should be 'debris_flow' or 'flood'")
    expect(byTestid(wrapper, 'report-outcome').exists()).toBe(false)
  })

  it('解析腿未装配（503）明说这条腿没接上，而不是"提交失败"', async () => {
    const wrapper = await renderForm()
    await fill(wrapper, { 'field-reporter': '村民', 'field-region': '540121', 'field-note': '沟道泥位抬升' })
    mockedSubmit.mockRejectedValue(new ReportApiError(503, '任务解析服务未装配', { detail: { code: 'E_PARSER_UNAVAILABLE', message: '任务解析服务未装配' } }))
    await byTestid(wrapper, 'report-submit').trigger('click')
    await flushPromises()
    expect(byTestid(wrapper, 'report-unavailable').text()).toContain('解析腿未装配')
    expect(byTestid(wrapper, 'report-error').exists()).toBe(false)
  })

  /**
   * 一句错误里出现两个 HTTP 码，读的人会以为是两次故障。
   *
   * 客户端构造期已经把码与原因写进 message（`上报接口调用失败（HTTP 0）：timeout…`），
   * 表单再冠一句"上报失败："就重复了。超时这一头尤其常见——它连响应体都没有。
   */
  it('非 422 的失败不套两层前缀', async () => {
    const wrapper = await renderForm()
    await fill(wrapper, { 'field-reporter': '村民', 'field-region': '540121', 'field-note': '沟道泥位抬升' })
    mockedSubmit.mockRejectedValue(new ReportApiError(0, '上报接口调用失败（HTTP 0）：timeout of 20000ms exceeded', undefined))
    await byTestid(wrapper, 'report-submit').trigger('click')
    await flushPromises()
    const shown = byTestid(wrapper, 'report-error').text()
    expect(shown).toBe('上报接口调用失败（HTTP 0）：timeout of 20000ms exceeded')
    expect(shown.match(/HTTP 0/g)).toHaveLength(1)
  })

  /**
   * 四个空格不是"一条正文"。
   *
   * 真机在正文里敲空格：`min_length=4` 放它过去，一路走到解析腿才被
   * `ValueError("待解析文本为空")` 拦下——那是个裸 ValueError，界面上就是
   * HTTP 500 + "Internal Server Error"。现在前端不发这一条，后端也同步裁剪。
   */
  it('正文只有空格时不发请求，并把原因说在错误行里', async () => {
    const wrapper = await renderForm()
    await fill(wrapper, { 'field-reporter': '村民', 'field-region': '540121', 'field-note': '    ' })
    await byTestid(wrapper, 'report-submit').trigger('click')
    await flushPromises()
    expect(mockedSubmit).not.toHaveBeenCalled()
    expect(byTestid(wrapper, 'report-error').text()).toContain('没有发出去')
  })

  it('区划代码写成小写时不发请求，就地按后端口径说明', async () => {
    const wrapper = await renderForm()
    await fill(wrapper, { 'field-reporter': '村民', 'field-region': '54012a', 'field-note': '沟道泥位抬升' })
    await byTestid(wrapper, 'report-submit').trigger('click')
    await flushPromises()
    expect(mockedSubmit).not.toHaveBeenCalled()
    expect(byTestid(wrapper, 'report-error').text()).toContain('区划代码「54012a」')
  })

  /** 正文是唯一可能写很长的格子：标题里那句"4..2000 字"看不见余量，撞线要自己说。 */
  it('正文字数条随输入变化，撞满上限时改口', async () => {
    const wrapper = await renderForm()
    expect(byTestid(wrapper, 'note-count').text()).toContain(`0 / ${REPORT_LIMITS.note.max}`)
    await byTestid(wrapper, 'field-note').setValue('沟道泥位抬升')
    expect(byTestid(wrapper, 'note-count').text()).toContain(`6 / ${REPORT_LIMITS.note.max}`)
    await byTestid(wrapper, 'field-note').setValue('泥'.repeat(REPORT_LIMITS.note.max))
    const full = byTestid(wrapper, 'note-count')
    expect(full.text()).toContain('（再长就不收了）')
    expect(full.classes()).toContain('report-note__count--full')
  })

  it('发出去的是裁过首尾空白的文本', async () => {
    const wrapper = await renderForm()
    await fill(wrapper, { 'field-reporter': '  村民  ', 'field-region': ' 540121 ', 'field-note': '\n 沟道泥位抬升 \n' })
    mockedSubmit.mockResolvedValue(outcome())
    await byTestid(wrapper, 'report-submit').trigger('click')
    await flushPromises()
    expect(mockedSubmit.mock.calls[0]?.[0]).toMatchObject({
      reporter: '村民',
      region_code: '540121',
      note: '沟道泥位抬升',
    })
  })

  it('灾种提示只有空格时不带键（后端按"未填"处理）', async () => {
    const wrapper = await renderForm()
    await fill(wrapper, { 'field-reporter': '村民', 'field-region': '540121', 'field-note': '沟道泥位抬升', 'field-hazard-hint': '   ' })
    mockedSubmit.mockResolvedValue(outcome())
    await byTestid(wrapper, 'report-submit').trigger('click')
    await flushPromises()
    expect(Object.keys(mockedSubmit.mock.calls[0]?.[0] ?? {})).not.toContain('hazard_hint')
  })

  it('后端不可达时给状态说明，不假装已提交', async () => {
    const wrapper = await renderForm()
    await fill(wrapper, { 'field-reporter': '村民', 'field-region': '540121', 'field-note': '沟道泥位抬升' })
    mockedSubmit.mockRejectedValue(new ReportApiError(0, 'connect ECONNREFUSED'))
    await byTestid(wrapper, 'report-submit').trigger('click')
    await flushPromises()
    expect(byTestid(wrapper, 'report-error').text()).toContain('connect ECONNREFUSED')
    expect(byTestid(wrapper, 'report-outcome').exists()).toBe(false)
  })

  it('清空回执后不留上一次的错误痕迹', async () => {
    const wrapper = await renderForm()
    await fill(wrapper, { 'field-reporter': '村民', 'field-region': '540121', 'field-note': '沟道泥位抬升' })
    mockedSubmit.mockRejectedValueOnce(new ReportApiError(0, 'connect ECONNREFUSED')).mockResolvedValue(outcome())
    await byTestid(wrapper, 'report-submit').trigger('click')
    await flushPromises()
    expect(byTestid(wrapper, 'report-error').exists()).toBe(true)
    await byTestid(wrapper, 'report-reset').trigger('click')
    await flushPromises()
    expect(byTestid(wrapper, 'report-error').exists()).toBe(false)
    await byTestid(wrapper, 'report-submit').trigger('click')
    await flushPromises()
    expect(byTestid(wrapper, 'report-outcome').exists()).toBe(true)
    expect(byTestid(wrapper, 'report-error').exists()).toBe(false)
  })
})
