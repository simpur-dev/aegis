import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { AssistantFrame, CapabilitiesDto } from '@/api/assistant'
import { AssistantApiError, assistantApi, CHAT_LIMITS } from '@/api/assistant'
import { readRepoFile } from '@/testing/repoSource'
import AssistantView from '@/views/AssistantView.vue'

vi.mock('@/api/assistant', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/assistant')>()
  return {
    ...actual,
    assistantApi: { capabilities: vi.fn(), chat: vi.fn(), confirm: vi.fn(), session: vi.fn() },
  }
})

const mockedCapabilities = vi.mocked(assistantApi.capabilities)
const mockedChat = vi.mocked(assistantApi.chat)
const mockedConfirm = vi.mocked(assistantApi.confirm)

async function* framesOf(frames: AssistantFrame[]): AsyncGenerator<AssistantFrame> {
  for (const item of frames) yield item
}

/**
 * 替身只表达"本页对 antd 的三点依赖"：tag 的文案、alert 的消息、以及能双向绑定的输入框。
 * 与 `components/system/LegStatusPanel.spec.ts` 同一手法（不装整套组件库，也不测组件库自己）。
 */
const STUBS = {
  'a-card': {
    name: 'ACard',
    props: ['title', 'loading', 'size'],
    template: '<div class="card"><div class="extra"><slot name="extra" /></div><slot /></div>',
  },
  'a-space': { name: 'ASpace', props: ['size', 'wrap'], template: '<div class="space"><slot /></div>' },
  'a-tag': { name: 'ATag', props: ['color'], template: '<span class="tag" :data-color="color"><slot /></span>' },
  'a-button': {
    name: 'AButton',
    props: ['loading', 'size', 'type', 'disabled'],
    template: '<button class="button" :disabled="disabled"><slot /></button>',
  },
  'a-alert': {
    name: 'AAlert',
    props: ['message', 'description', 'type', 'showIcon', 'banner'],
    template: '<div class="alert">{{ message }}｜{{ description }}</div>',
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
  'a-modal': {
    name: 'AModal',
    props: ['open', 'title', 'footer', 'width'],
    emits: ['update:open'],
    template: '<div class="modal"><slot v-if="open" /></div>',
  },
  ReportForm: { name: 'ReportForm', props: ['initialRegionCode', 'initialReporter'], template: '<div class="report-form" />' },
}

function capabilities(overrides: Partial<CapabilitiesDto> = {}): CapabilitiesDto {
  return {
    llm_configured: true,
    semantic_parser: true,
    wired_actions: { list_warnings: true },
    actions: [
      { action: 'query.warnings', title: '查询已发布预警', requires_confirmation: false, needs: ['list_warnings'], available: true, missing: [], example: '最近发布了哪些预警' },
      { action: 'run.drill', title: '发起一次演练', requires_confirmation: true, needs: ['run_drill'], available: false, missing: ['run_drill'], example: '发起一次演练' },
    ],
    session_ttl_seconds: 900,
    max_pending_actions: 8,
    ...overrides,
  }
}

async function renderView(caps: CapabilitiesDto | null = capabilities()) {
  if (caps === null) mockedCapabilities.mockRejectedValue(new Error('connect ECONNREFUSED'))
  else mockedCapabilities.mockResolvedValue(caps)
  const wrapper = mount(AssistantView, { global: { stubs: STUBS } })
  await flushPromises()
  return wrapper
}

function byTestid(wrapper: ReturnType<typeof mount>, id: string) {
  return wrapper.find(`[data-testid="${id}"]`)
}

async function sendMessage(wrapper: ReturnType<typeof mount>, text: string) {
  await wrapper.find('[data-testid="field-message"]').setValue(text)
  await wrapper.find('[data-testid="send"]').trigger('click')
  await flushPromises()
}

beforeEach(() => {
  mockedCapabilities.mockReset()
  mockedChat.mockReset()
  mockedConfirm.mockReset()
})

/**
 * 建议条必须点得动。
 *
 * 此前这一排是 `<a-tag>`：写着"查询已发布预警"，点下去什么都没有，而本机没有 LLM 时
 * 意图只走后端词表——值班员得自己猜该打出哪个词才问得到它，猜错就收到一句"未能识别意图"。
 * 现在点一下把该动作自己的示例句填进输入框，发送仍由人决定。
 */
describe('建议条：点一下填进输入框，不代发', () => {
  it('点芯片把该动作的示例句填入消息框，且不发请求', async () => {
    const wrapper = await renderView()
    await wrapper.find('[data-testid="action-query.warnings"]').trigger('click')
    expect((wrapper.find('[data-testid="field-message"]').element as HTMLTextAreaElement).value).toBe('最近发布了哪些预警')
    expect(mockedChat).not.toHaveBeenCalled()
  })

  it('需确认的动作也照样填得出来，确认这一步留给后端提案', async () => {
    const wrapper = await renderView()
    await wrapper.find('[data-testid="action-run.drill"]').trigger('click')
    expect((wrapper.find('[data-testid="field-message"]').element as HTMLTextAreaElement).value).toBe('发起一次演练')
    expect(wrapper.text()).toContain('需确认')
  })

  it('能力面没给示例句时这条禁用，点了不改输入框', async () => {
    const caps = capabilities({
      actions: [
        { action: 'query.tasks', title: '查询任务单元', requires_confirmation: false, needs: [], available: true, missing: [], example: '' },
      ],
    })
    const wrapper = await renderView(caps)
    const chip = wrapper.find('[data-testid="action-query.tasks"]')
    expect(chip.attributes('disabled')).toBeDefined()
    await chip.trigger('click')
    expect((wrapper.find('[data-testid="field-message"]').element as HTMLTextAreaElement).value).toBe('')
  })

  it('填进去的句子随后能发出去（点芯片与发送是两步，不是一步）', async () => {
    const wrapper = await renderView()
    mockedChat.mockReturnValue(framesOf([{ type: 'meta', session_id: 'sess_0001', llm_configured: true, actions: [] }]))
    await wrapper.find('[data-testid="action-query.warnings"]').trigger('click')
    await wrapper.find('[data-testid="send"]').trigger('click')
    await flushPromises()
    expect(mockedChat.mock.calls[0]?.[0]?.message).toBe('最近发布了哪些预警')
  })
})

describe('能力面：读不到与没配置都不许显示成正常', () => {
  it('llm_configured=false 时明说"语义服务未配置：仅规则词表可用"', async () => {
    const wrapper = await renderView(capabilities({ llm_configured: false }))
    const banner = byTestid(wrapper, 'banner-no-llm')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain('语义服务未配置：仅规则词表可用')
    expect(byTestid(wrapper, 'caps-state').text()).toBe('语义服务未配置')
  })

  it('能力面失败时显示"读不到"，不显示"一切正常"', async () => {
    const wrapper = await renderView(null)
    expect(byTestid(wrapper, 'caps-state').text()).toBe('读不到能力面')
    expect(byTestid(wrapper, 'banner-caps-error').text()).toContain('读不到能力面')
    expect(byTestid(wrapper, 'banner-caps-error').text()).toContain('connect ECONNREFUSED')
    expect(wrapper.find('[data-testid="banner-no-llm"]').exists()).toBe(false)
  })

  it('503（腿没装配）与 404（出口未启用）是两句话', async () => {
    mockedCapabilities.mockRejectedValue(new AssistantApiError(503, '语义交互服务未装配（assistant）', { detail: { code: 'E_ASSISTANT_UNAVAILABLE' } }))
    const unavailable = mount(AssistantView, { global: { stubs: STUBS } })
    await flushPromises()
    expect(byTestid(unavailable, 'banner-unavailable').text()).toContain('E_ASSISTANT_UNAVAILABLE')

    mockedCapabilities.mockRejectedValue(new AssistantApiError(404, 'Not Found', { detail: 'Not Found' }))
    const disabled = mount(AssistantView, { global: { stubs: STUBS } })
    await flushPromises()
    expect(byTestid(disabled, 'banner-caps-error').text()).toContain('路由整段没挂载')
    expect(byTestid(disabled, 'banner-caps-error').text()).not.toContain('E_ASSISTANT_UNAVAILABLE')
  })

  it('缺依赖的动作点名缺了哪个，不显示成可用', async () => {
    const wrapper = await renderView()
    expect(byTestid(wrapper, 'action-run.drill').text()).toContain('缺 run_drill')
    expect(byTestid(wrapper, 'action-run.drill').classes()).toContain('is-missing')
    expect(byTestid(wrapper, 'action-query.warnings').classes()).toContain('is-available')
    expect(byTestid(wrapper, 'action-query.warnings').text()).toContain('查询已发布预警')
  })
})

describe('对话：每一帧都留在时间线上', () => {
  it('按后端帧序列渲染，会话号取后端给的', async () => {
    mockedChat.mockReturnValue(
      framesOf([
        { type: 'meta', session_id: 'sess_0001', llm_configured: false, actions: ['query.agents'] },
        { type: 'intent', action: 'query.agents', args: {}, confidence: 0.9, decided_by: 'rule', note: '', requires_confirmation: false },
        { type: 'status', text: '执行 query.agents' },
        { type: 'result', action: 'query.agents', online: 3 },
        { type: 'answer', text: '3 个在线智能体' },
        { type: 'done', session_id: 'sess_0001', latency_ms: 7.4, rejected_count: 0 },
      ]),
    )
    const wrapper = await renderView(capabilities({ llm_configured: false }))
    await sendMessage(wrapper, '在线智能体有几个')

    expect(byTestid(wrapper, 'session-id').text()).toContain('sess_0001')
    expect(byTestid(wrapper, 'frame-answer').text()).toContain('3 个在线智能体')
    expect(byTestid(wrapper, 'frame-status').text()).toContain('执行 query.agents')
    expect(byTestid(wrapper, 'frame-intent').text()).toContain('query.agents')
    expect(byTestid(wrapper, 'frame-intent').text()).not.toContain('需人工确认')
    expect(byTestid(wrapper, 'frame-result').text()).toContain('"online":3')
    expect(byTestid(wrapper, 'frame-done').text()).toContain('累计被拒 0 次')
    expect(wrapper.findAll('.frame')).toHaveLength(6)
  })

  it('发送的报文带上报人名义，区划代码留空时不发空串', async () => {
    mockedChat.mockReturnValue(framesOf([{ type: 'meta', session_id: 'sess_0001', llm_configured: true, actions: [] }]))
    const wrapper = await renderView()
    await wrapper.find('[data-testid="field-message"]').setValue('查预警')
    await wrapper.find('[data-testid="send"]').trigger('click')
    await flushPromises()
    const sent = mockedChat.mock.calls[0]?.[0] as unknown as Record<string, unknown>
    expect(sent.message).toBe('查预警')
    expect(sent.reporter).toBe('值班员')
    expect(sent.region_code).toBeUndefined()
    expect(sent.session_id).toBeUndefined()
  })

  it('第二轮把会话号带回给后端', async () => {
    mockedChat.mockReturnValue(framesOf([{ type: 'meta', session_id: 'sess_0001', llm_configured: true, actions: [] }]))
    const wrapper = await renderView()
    await sendMessage(wrapper, '查预警')
    await sendMessage(wrapper, '再看一次')
    expect(mockedChat.mock.calls[1]?.[0]).toMatchObject({ session_id: 'sess_0001' })
  })

  it('空消息不打后端：后端要求 1..2000 字，这里就地说明', async () => {
    const wrapper = await renderView()
    await sendMessage(wrapper, '   ')
    expect(mockedChat).not.toHaveBeenCalled()
    expect(byTestid(wrapper, 'stream-error').text()).toContain('对话内容为空')
  })

  /**
   * 上限要在**填之前**就看得见。
   *
   * 真机贴一段 2001 字的险情进去，页面以前要等到 422 回来才说"失败了"，
   * 而那一整段话还留在框里，没人知道自己写长了。现在计数条把余量摆出来，
   * 超出部分由 antd 在输入处截断（真机：填 2005 字，`textarea.value.length` 是 2000）。
   *
   * 这条读模板而不是读 DOM：antd 的 TextArea 把 `maxlength` 当 prop 吃掉，
   * 渲染出来的 `<textarea>` 上并没有 maxlength 属性（真机实测 `getAttribute('maxlength')` 为 null），
   * 断言挂在属性上就是在测替身，不是测组件。
   */
  it('消息框把后端那份上限交给输入框，并显示当前字数', async () => {
    const view = readRepoFile('frontend', 'src', 'views', 'AssistantView.vue')
    expect(view).toContain(':maxlength="CHAT_LIMITS.message.max"')
    const wrapper = await renderView()
    expect(byTestid(wrapper, 'char-count').text()).toContain(`0 / ${CHAT_LIMITS.message.max}`)
    await byTestid(wrapper, 'field-message').setValue('查预警')
    expect(byTestid(wrapper, 'char-count').text()).toContain(`3 / ${CHAT_LIMITS.message.max}`)
  })

  it('区划代码写成小写时不发请求，就地按后端口径说明', async () => {
    const wrapper = await renderView()
    await wrapper.find('[data-testid="field-region"]').setValue('54012a')
    await sendMessage(wrapper, '查预警')
    expect(mockedChat).not.toHaveBeenCalled()
    expect(byTestid(wrapper, 'stream-error').text()).toContain('区划代码「54012a」')
  })

  it('上报人名义只剩 1 个字时不发请求（后端要求 2..64）', async () => {
    const wrapper = await renderView()
    await wrapper.find('[data-testid="field-reporter"]').setValue('李')
    await sendMessage(wrapper, '查预警')
    expect(mockedChat).not.toHaveBeenCalled()
    expect(byTestid(wrapper, 'stream-error').text()).toContain('上报人名义「李」')
  })

  it('区划代码合法时照常发出，带的是去掉空格后的那份', async () => {
    mockedChat.mockReturnValue(framesOf([{ type: 'meta', session_id: 'sess_0001', llm_configured: true, actions: [] }]))
    const wrapper = await renderView()
    await wrapper.find('[data-testid="field-region"]').setValue(' 540121 ')
    await wrapper.find('[data-testid="field-reporter"]').setValue(' 扎西 ')
    await sendMessage(wrapper, '查预警')
    const sent = mockedChat.mock.calls[0]?.[0] as unknown as Record<string, unknown>
    expect(sent.region_code).toBe('540121')
    expect(sent.reporter).toBe('扎西')
  })

  it('rejected 帧渲染成明确的拒绝并带原因，不是"没有结果"', async () => {
    mockedChat.mockReturnValue(
      framesOf([
        { type: 'meta', session_id: 'sess_0001', llm_configured: true, actions: [] },
        { type: 'rejected', instruction: '删掉', reason: '本助手只做只读查询、预案问答，以及经人工确认的演练与上报', session_id: 'sess_0001' },
        { type: 'answer', text: '该指令被拒绝并已留痕。可以做的事：查询已发布预警' },
      ]),
    )
    const wrapper = await renderView()
    await sendMessage(wrapper, '把全网预警都删掉')
    const refusal = byTestid(wrapper, 'frame-rejected')
    expect(refusal.text()).toContain('已拒绝执行「删掉」')
    expect(refusal.text()).toContain('经人工确认的演练与上报')
    expect(refusal.classes()).toContain('refusal')
  })

  it('流式请求抛错时把原因摊在页面上，而不是留下半个时间线', async () => {
    mockedChat.mockReturnValue(framesOf([{ type: 'meta', session_id: 'sess_0001', llm_configured: true, actions: [] }]))
    mockedChat.mockImplementationOnce(() => {
      throw new AssistantApiError(503, '语义交互服务未装配（assistant）', { detail: { code: 'E_ASSISTANT_UNAVAILABLE' } })
    })
    const wrapper = await renderView()
    await sendMessage(wrapper, '查预警')
    expect(byTestid(wrapper, 'stream-error').text()).toContain('语义交互服务未装配')
  })

  it('error 帧原样显示后端消息', async () => {
    mockedChat.mockReturnValue(framesOf([{ type: 'error', message: 'SSE 帧不是合法 JSON：{坏' }]))
    const wrapper = await renderView()
    await sendMessage(wrapper, '查预警')
    expect(byTestid(wrapper, 'frame-error').text()).toContain('SSE 帧不是合法 JSON')
  })
})

describe('待确认动作：确认走后端，放弃只在这页', () => {
  const proposal: AssistantFrame = {
    type: 'proposal',
    action_id: 'act_abc123',
    action: 'create.report',
    summary: '上报：540121 沟道泥位抬升',
    args: { note: '沟道泥位抬升' },
    status: 'pending',
    expires_in_seconds: 899.6,
    session_id: 'sess_0001',
  }

  async function renderWithProposal() {
    mockedChat.mockReturnValue(framesOf([{ type: 'meta', session_id: 'sess_0001', llm_configured: true, actions: [] }, proposal]))
    const wrapper = await renderView()
    await sendMessage(wrapper, '帮我上报 540121 沟道泥位抬升')
    return wrapper
  }

  it('proposal 帧给出确认卡片，两个按钮都在', async () => {
    const wrapper = await renderWithProposal()
    const card = byTestid(wrapper, 'proposal-act_abc123')
    expect(card.exists()).toBe(true)
    expect(card.text()).toContain('create.report')
    expect(card.text()).toContain('上报：540121')
    expect(byTestid(wrapper, 'confirm-act_abc123').exists()).toBe(true)
    expect(byTestid(wrapper, 'dismiss-act_abc123').exists()).toBe(true)
  })

  it('确认按 session_id + action_id 打 /confirm，并把后端回执显示出来', async () => {
    const wrapper = await renderWithProposal()
    mockedConfirm.mockResolvedValue({ status: 'executed', action_id: 'act_abc123', action: 'create.report' })
    await wrapper.find('[data-testid="confirm-act_abc123"]').trigger('click')
    await flushPromises()
    expect(mockedConfirm).toHaveBeenCalledWith({ session_id: 'sess_0001', action_id: 'act_abc123', actor: '值班员' })
    expect(byTestid(wrapper, 'decision').text()).toContain('已执行')
    expect(byTestid(wrapper, 'decision').text()).toContain('create.report')
  })

  it.each([
    ['rejected', '动作已处置（executed），不重复执行', '已拒绝'],
    ['expired', '确认超时，动作已过期', '已过期'],
    ['failed', 'RuntimeError: 外呼失败', '执行失败'],
  ] as const)('%s 的回执原样落在这句话里（%s）', async (status, reason, label) => {
    const wrapper = await renderWithProposal()
    mockedConfirm.mockResolvedValue(
      status === 'failed'
        ? { status, action_id: 'act_abc123', action: 'create.report', error: reason }
        : { status, action_id: 'act_abc123', reason },
    )
    await wrapper.find('[data-testid="confirm-act_abc123"]').trigger('click')
    await flushPromises()
    const decision = byTestid(wrapper, 'decision').text()
    expect(decision).toContain(label)
    expect(decision).toContain(reason)
  })

  it('放弃不调后端，并明说后端没有取消接口', async () => {
    const wrapper = await renderWithProposal()
    await wrapper.find('[data-testid="dismiss-act_abc123"]').trigger('click')
    await flushPromises()
    expect(mockedConfirm).not.toHaveBeenCalled()
    const decision = byTestid(wrapper, 'decision').text()
    expect(decision).toContain('后端没有取消接口')
    expect(decision).not.toContain('已取消')
  })

  it('处置后的卡片不给第二次点击：确认是一次性的', async () => {
    const wrapper = await renderWithProposal()
    mockedConfirm.mockResolvedValue({ status: 'executed', action_id: 'act_abc123', action: 'create.report' })
    await wrapper.find('[data-testid="confirm-act_abc123"]').trigger('click')
    await flushPromises()
    expect(byTestid(wrapper, 'confirm-act_abc123').attributes('disabled')).toBeDefined()
    expect(byTestid(wrapper, 'dismiss-act_abc123').attributes('disabled')).toBeDefined()
  })

  it('确认请求本身失败时说的是失败，不是"已执行"', async () => {
    const wrapper = await renderWithProposal()
    mockedConfirm.mockRejectedValue(new AssistantApiError(503, '语义交互服务未装配（assistant）'))
    await wrapper.find('[data-testid="confirm-act_abc123"]').trigger('click')
    await flushPromises()
    expect(byTestid(wrapper, 'decision').text()).toContain('确认请求失败')
  })
})

describe('人工上报入口（B5）在助手页也够得着', () => {
  it('点「人工上报」打开表单，并把区划与名义带过去', async () => {
    const wrapper = await renderView()
    await wrapper.find('[data-testid="field-region"]').setValue('540121')
    expect(wrapper.find('.report-form').exists()).toBe(false)
    await wrapper.find('[data-testid="open-report"]').trigger('click')
    await flushPromises()
    const form = wrapper.findComponent({ name: 'ReportForm' })
    expect(form.exists()).toBe(true)
    expect(form.props('initialRegionCode')).toBe('540121')
    expect(form.props('initialReporter')).toBe('值班员')
  })
})
