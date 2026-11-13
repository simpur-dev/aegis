import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest'

import type { ActionSpecDto, AssistantFrame, CapabilitiesDto } from '@/api/assistant'
import { AssistantApiError, assistantApi, CHAT_LIMITS } from '@/api/assistant'
import { readRepoFile } from '@/testing/repoSource'
import { reloadAssistantSession, resetAssistantSession } from '@/composables/useAssistantSession'
import { resetAssistantCapabilities } from '@/composables/useAssistantCapabilities'
import AssistantPanel from '@/components/assistant/AssistantPanel.vue'
import AssistantView from '@/views/AssistantView.vue'

vi.mock('@/api/assistant', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/assistant')>()
  return {
    ...actual,
    assistantApi: { capabilities: vi.fn(), chat: vi.fn(), confirm: vi.fn() },
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

function spec(overrides: Partial<ActionSpecDto> & { action: string; title: string }): ActionSpecDto {
  return {
    domain: 'query',
    required_role: 'duty',
    needs_confirm: false,
    reversible: false,
    example: '',
    allowed: true,
    denied_reason: null,
    ...overrides,
  }
}

function capabilities(overrides: Partial<CapabilitiesDto> = {}): CapabilitiesDto {
  return {
    llm_configured: true,
    actions: [
      spec({ action: 'warning.list', title: '查询预警', example: '最近发布了哪些预警' }),
      spec({
        action: 'drill.start',
        title: '发起一次演练',
        domain: 'drill',
        needs_confirm: true,
        allowed: false,
        denied_reason: '需要 lead 及以上角色',
        example: '发起一次演练',
      }),
    ],
    session_ttl_seconds: 900,
    confirm_ttl_seconds: 300,
    control_actions_persistent: true,
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
  // 时间线、会话号与能力面都是模块级状态（换页接得上），用例之间必须从头开始
  resetAssistantSession()
  resetAssistantCapabilities()
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
    await wrapper.find('[data-testid="action-warning.list"]').trigger('click')
    expect((wrapper.find('[data-testid="field-message"]').element as HTMLTextAreaElement).value).toBe('最近发布了哪些预警')
    expect(mockedChat).not.toHaveBeenCalled()
  })

  it('需确认的动作也照样填得出来，确认这一步留给后端提案', async () => {
    const wrapper = await renderView()
    await wrapper.find('[data-testid="action-drill.start"]').trigger('click')
    expect((wrapper.find('[data-testid="field-message"]').element as HTMLTextAreaElement).value).toBe('发起一次演练')
    expect(wrapper.text()).toContain('需确认')
  })

  it('能力面没给示例句时这条禁用，点了不改输入框', async () => {
    const caps = capabilities({
      actions: [spec({ action: 'task.list', title: '查询任务单元', domain: 'task', example: '' })],
    })
    const wrapper = await renderView(caps)
    const chip = wrapper.find('[data-testid="action-task.list"]')
    expect(chip.attributes('disabled')).toBeDefined()
    await chip.trigger('click')
    expect((wrapper.find('[data-testid="field-message"]').element as HTMLTextAreaElement).value).toBe('')
  })

  it('填进去的句子随后能发出去（点芯片与发送是两步，不是一步）', async () => {
    const wrapper = await renderView()
    mockedChat.mockReturnValue(framesOf([{ type: 'meta', session_id: 'sess_0001', llm_configured: true, actions: [] }]))
    await wrapper.find('[data-testid="action-warning.list"]').trigger('click')
    await wrapper.find('[data-testid="send"]').trigger('click')
    await flushPromises()
    expect(mockedChat.mock.calls[0]?.[0]?.message).toBe('最近发布了哪些预警')
  })
})

describe('能力面：读不到与没配置都不许显示成正常', () => {
  it('llm_configured=false 时页头明说"语义服务未配置"，黄色横幅已移除', async () => {
    const wrapper = await renderView(capabilities({ llm_configured: false }))
    expect(byTestid(wrapper, 'caps-state').text()).toBe('语义服务未配置')
    expect(wrapper.find('[data-testid="banner-no-llm"]').exists()).toBe(false)
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

  it('角色不够的动作照样列出，但标出缺什么并置灰', async () => {
    const wrapper = await renderView()
    expect(byTestid(wrapper, 'action-drill.start').text()).toContain('需要 lead')
    expect(byTestid(wrapper, 'action-drill.start').classes()).toContain('is-denied')
    expect(byTestid(wrapper, 'action-warning.list').classes()).toContain('is-available')
    expect(byTestid(wrapper, 'action-warning.list').text()).toContain('查询预警')
  })
})

describe('对话：每一帧都留在时间线上', () => {
  it('按后端帧序列渲染，会话号取后端给的', async () => {
    mockedChat.mockReturnValue(
      framesOf([
        { type: 'meta', session_id: 'sess_0001', llm_configured: false, actions: ['agent.list'] },
        { type: 'intent', action: 'agent.list', args: {}, confidence: 0.9, decided_by: 'rule', note: '', requires_confirmation: false },
        { type: 'status', text: '执行 agent.list' },
        { type: 'result', action: 'agent.list', online: 3 },
        { type: 'answer', text: '3 个在线智能体' },
        { type: 'done', session_id: 'sess_0001', latency_ms: 7.4, rejected_count: 0 },
      ]),
    )
    const wrapper = await renderView(capabilities({ llm_configured: false }))
    await sendMessage(wrapper, '在线智能体有几个')

    expect(byTestid(wrapper, 'session-id').text()).toContain('sess_0001')
    expect(byTestid(wrapper, 'frame-answer').text()).toContain('3 个在线智能体')
    expect(byTestid(wrapper, 'frame-status').text()).toContain('执行 agent.list')
    expect(byTestid(wrapper, 'frame-intent').text()).toContain('agent.list')
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

  /**
   * 幂等键跟着"这一句草稿"走：失败重发要命中同一行动作台账，
   * 换一句新话则必须换键，否则后端的幂等重放会把新意图当成旧的重复提交。
   */
  it('同一句草稿失败重发时复用同一个幂等键', async () => {
    mockedChat.mockImplementationOnce(() => {
      throw new AssistantApiError(503, '语义交互服务未装配（assistant）')
    })
    mockedChat.mockReturnValueOnce(framesOf([{ type: 'meta', session_id: 'sess_0001', llm_configured: true, actions: [] }]))
    const wrapper = await renderView()
    await sendMessage(wrapper, '同一句重试用例：查预警')
    expect(byTestid(wrapper, 'stream-error').text()).toContain('语义交互服务未装配')
    // 失败后草稿留在框里，再点一次发送就是"重试同一句"
    await wrapper.find('[data-testid="send"]').trigger('click')
    await flushPromises()
    const first = mockedChat.mock.calls[0]?.[0] as unknown as Record<string, unknown>
    const second = mockedChat.mock.calls[1]?.[0] as unknown as Record<string, unknown>
    expect(first.idempotency_key).toBe(second.idempotency_key)
    expect(String(first.idempotency_key)).toMatch(/^chat_[0-9a-z]+$/)
    expect(String(first.idempotency_key).length).toBeGreaterThanOrEqual(CHAT_LIMITS.idempotency_key.min)
    expect(String(first.idempotency_key).length).toBeLessThanOrEqual(CHAT_LIMITS.idempotency_key.max)
  })

  it('换一句新草稿时换一个新的幂等键', async () => {
    mockedChat.mockReturnValue(framesOf([{ type: 'meta', session_id: 'sess_0001', llm_configured: true, actions: [] }]))
    const wrapper = await renderView()
    await sendMessage(wrapper, '换键用例：查预警')
    await sendMessage(wrapper, '换键用例：再看一次任务')
    const first = mockedChat.mock.calls[0]?.[0] as unknown as Record<string, unknown>
    const second = mockedChat.mock.calls[1]?.[0] as unknown as Record<string, unknown>
    expect(first.idempotency_key).toBeDefined()
    expect(second.idempotency_key).toBeDefined()
    expect(first.idempotency_key).not.toBe(second.idempotency_key)
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
    const panel = readRepoFile('frontend', 'src', 'components', 'assistant', 'AssistantPanel.vue')
    expect(panel).toContain(':maxlength="CHAT_LIMITS.message.max"')
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

  it('区划代码合法时照常发出，带的是去掉空格后的那份', async () => {    mockedChat.mockReturnValue(framesOf([{ type: 'meta', session_id: 'sess_0001', llm_configured: true, actions: [] }]))
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
    action: 'report.submit',
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
    expect(card.text()).toContain('report.submit')
    expect(card.text()).toContain('上报：540121')
    expect(byTestid(wrapper, 'confirm-act_abc123').exists()).toBe(true)
    expect(byTestid(wrapper, 'dismiss-act_abc123').exists()).toBe(true)
  })

  it('确认按 session_id + action_id 打 /confirm，并把后端回执显示出来', async () => {
    const wrapper = await renderWithProposal()
    mockedConfirm.mockResolvedValue({ status: 'executed', action_id: 'act_abc123', action: 'report.submit' })
    await wrapper.find('[data-testid="confirm-act_abc123"]').trigger('click')
    await flushPromises()
    expect(mockedConfirm).toHaveBeenCalledWith('act_abc123', { actor: '值班员' })
    expect(byTestid(wrapper, 'decision').text()).toContain('已执行')
    expect(byTestid(wrapper, 'decision').text()).toContain('report.submit')
  })

  /**
   * 后端 /confirm 把整份执行结果都带回来了（上报回执里有链路、可能还有预警与工单），
   * 页面上此前只剩"已执行 report.submit"——经助手报上去一条险情，手里一个号都没有。
   */
  it('执行成功要把能跟进的号带回来', async () => {
    const wrapper = await renderWithProposal()
    mockedConfirm.mockResolvedValue({
      status: 'executed',
      action_id: 'act_abc123',
      action: 'report.submit',
      result: {
        report: {
          trace_id: 'trc_0123456789abcdef',
          warning: { warning_id: 'wrn_fedcba9876543210' },
          review: { instance_id: 'wfi_abcdef012345' },
        },
      },
    })
    await wrapper.find('[data-testid="confirm-act_abc123"]').trigger('click')
    await flushPromises()
    const decision = byTestid(wrapper, 'decision').text()
    expect(decision).toContain('trc_0123456789abcdef')
    expect(decision).toContain('wrn_fedcba9876543210')
    expect(decision).toContain('wfi_abcdef012345')
  })

  it('后端没回号时明说没回，而不是只写"已执行"', async () => {
    const wrapper = await renderWithProposal()
    mockedConfirm.mockResolvedValue({ status: 'executed', action_id: 'act_abc123', action: 'drill.start', result: {} })
    await wrapper.find('[data-testid="confirm-act_abc123"]').trigger('click')
    await flushPromises()
    expect(byTestid(wrapper, 'decision').text()).toContain('没回可跟进的号')
  })

  it.each([
    ['rejected', '动作已处置（executed），不重复执行', '已拒绝'],
    ['expired', '确认超时，动作已过期', '已过期'],
    ['failed', 'RuntimeError: 外呼失败', '执行失败'],
  ] as const)('%s 的回执原样落在这句话里（%s）', async (status, reason, label) => {
    const wrapper = await renderWithProposal()
    mockedConfirm.mockResolvedValue(
      status === 'failed'
        ? { status, action_id: 'act_abc123', action: 'report.submit', error: reason }
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

  /**
   * 真机量过：放弃一张演练卡之后按 F5，卡片回到"待确认"、确认按钮又能点了——
   * 等于把刚被拒绝的演练重新递给值班员。时间线存了而决定没存，就是这个下场。
   */
  it('放弃之后刷新，这张卡不会被重新递一遍', async () => {
    const wrapper = await renderWithProposal()
    await wrapper.find('[data-testid="dismiss-act_abc123"]').trigger('click')
    await flushPromises()
    wrapper.unmount()

    reloadAssistantSession()
    const again = await renderView()
    expect(byTestid(again, 'decision').text()).toContain('后端没有取消接口')
    expect(byTestid(again, 'confirm-act_abc123').attributes('disabled'), '拒绝过的动作不能刷新后又变成可确认').toBeDefined()
  })

  it('处置后的卡片不给第二次点击：确认是一次性的', async () => {
    const wrapper = await renderWithProposal()
    mockedConfirm.mockResolvedValue({ status: 'executed', action_id: 'act_abc123', action: 'report.submit' })
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

/**
 * Enter 是发消息那颗键。
 *
 * 真机量过的原样：在对话框里敲完"最近发布了哪些预警"按 Enter，
 * 只是插了个换行——0 条请求、0 帧、页面上也没有任何一处说这里有"发送"这回事。
 * 一个多行输入框里 Enter 什么都不做，而且不自带说明，就会让人以为自己那一下没生效。
 */
describe('对话框的 Enter / Shift+Enter', () => {
  const keydown = (wrapper: Awaited<ReturnType<typeof renderView>>, shiftKey: boolean) =>
    wrapper.find('[data-testid="field-message"]').trigger('keydown', { key: 'Enter', shiftKey })

  it('Enter 直接发送这一条', async () => {
    mockedChat.mockReturnValue(framesOf([{ type: 'meta', session_id: 'sess_0001', llm_configured: true, actions: [] }]))
    const wrapper = await renderView()
    await wrapper.find('[data-testid="field-message"]').setValue('查预警')
    await keydown(wrapper, false)
    await flushPromises()
    expect(mockedChat.mock.calls[0]?.[0]).toMatchObject({ message: '查预警' })
  })

  it('Shift+Enter 只换行，不发', async () => {
    const wrapper = await renderView()
    await wrapper.find('[data-testid="field-message"]').setValue('查预警')
    await keydown(wrapper, true)
    await flushPromises()
    expect(mockedChat).not.toHaveBeenCalled()
  })

  /** 上一轮还在流式时，键盘不能开出第二条对话（按钮那条路 antd 已经让点击落空了）。 */
  it('流式进行中按 Enter 不并发第二条，并说清为什么', async () => {
    let release: (() => void) | null = null
    mockedChat.mockImplementation(() => (async function* () {
      await new Promise<void>((resolve) => {
        release = resolve
      })
      yield { type: 'done', session_id: 'sess_0001', latency_ms: 1, rejected_count: 0 }
    })())
    const wrapper = await renderView()
    await wrapper.find('[data-testid="field-message"]').setValue('第一条')
    await keydown(wrapper, false)
    await flushPromises()
    expect(mockedChat).toHaveBeenCalledTimes(1)

    await wrapper.find('[data-testid="field-message"]').setValue('第二条')
    await keydown(wrapper, false)
    await flushPromises()
    expect(mockedChat).toHaveBeenCalledTimes(1)
    expect(byTestid(wrapper, 'stream-error').text()).toContain('上一轮还在进行中')

    // 赋值发生在 Promise 执行器里，TS 的流分析看不到，只能显式放宽
    ;(release as (() => void) | null)?.()
    await flushPromises()
  })

  /**
   * 只有 Enter 才是发送。此前这里没有键名判断：普通字母、退格、方向键都会被
   * `preventDefault` 并顺带发起一轮对话——真机上表现为"敲第一个字就没反应"。
   */
  it('普通按键不是发送：不拦默认行为，也不发起对话', async () => {
    const wrapper = await renderView()
    for (const key of ['a', 'Backspace', 'ArrowLeft']) {
      const notCanceled = await dispatchKeydown(wrapper, { key })
      expect(notCanceled, `${key} 被 preventDefault 了`).toBe(true)
    }
    expect(mockedChat).not.toHaveBeenCalled()
    expect(byTestid(wrapper, 'stream-error').exists()).toBe(false)
  })

  it('输入法合成中的 Enter 不发送、也不拦截选词', async () => {
    const wrapper = await renderView()
    await wrapper.find('[data-testid="field-message"]').setValue('沟道泥位')
    expect(await dispatchKeydown(wrapper, { key: 'Enter', isComposing: true })).toBe(true)
    // 旧 Safari 在合成期只给 keyCode 229
    expect(await dispatchKeydown(wrapper, { key: 'Enter' }, 229)).toBe(true)
    expect(mockedChat).not.toHaveBeenCalled()
  })

  it('约定写在字数条旁边，不靠人猜', async () => {
    const wrapper = await renderView()
    expect(byTestid(wrapper, 'char-count').text()).toContain('Enter 发送')
    expect(byTestid(wrapper, 'char-count').text()).toContain('Shift+Enter 换行')
  })
})

/** 真按键要落在 textarea 上：trigger() 造的事件没有 cancelable，测不出"有没有被拦"。 */
async function dispatchKeydown(
  wrapper: Awaited<ReturnType<typeof renderView>>,
  init: KeyboardEventInit,
  keyCode?: number,
): Promise<boolean> {
  const el = wrapper.find('[data-testid="field-message"]').element as HTMLTextAreaElement
  const event = new KeyboardEvent('keydown', { bubbles: true, cancelable: true, ...init })
  if (keyCode !== undefined) Object.defineProperty(event, 'keyCode', { value: keyCode })
  const notCanceled = el.dispatchEvent(event)
  await flushPromises()
  return notCanceled
}

/**
 * 停止按钮：流式读取要能被主动掐断。
 * 真机场景是"问错了 / 发现这一轮要等很久"——没有停止，用户只能刷新整页，
 * 而刷新会把还没渲染完的时间线与会话号一起丢掉。
 */
describe('流式进行中的停止与草稿保护', () => {
  it('点「停止」真的掐断这一轮，并说清它不是网络故障', async () => {
    mockedChat.mockImplementation((_body, options = {}) => (async function* () {
      await new Promise<void>((_resolve, reject) => {
        options.signal?.addEventListener(
          'abort',
          () => reject(new DOMException('aborted', 'AbortError')),
          { once: true },
        )
      })
      yield { type: 'done', session_id: 'sess_0001', latency_ms: 1, rejected_count: 0 }
    })())
    const wrapper = await renderView()
    await wrapper.find('[data-testid="field-message"]').setValue('把这一轮停掉')
    await wrapper.find('[data-testid="send"]').trigger('click')
    await flushPromises()

    const signal = mockedChat.mock.calls[0]?.[1]?.signal
    expect(signal).toBeInstanceOf(AbortSignal)
    expect(signal?.aborted).toBe(false)

    await byTestid(wrapper, 'stop').trigger('click')
    await flushPromises()

    expect(signal?.aborted).toBe(true)
    expect(byTestid(wrapper, 'stream-error').text()).toContain('已停止接收这一轮')
    expect(byTestid(wrapper, 'stream-error').text()).not.toContain('对话中断')
  })

  it('流式期间新打的内容，不会在这一轮收尾时被清空', async () => {
    let release: (() => void) | null = null
    mockedChat.mockImplementation(() => (async function* () {
      await new Promise<void>((resolve) => {
        release = resolve
      })
      yield { type: 'meta', session_id: 'sess_0001', llm_configured: true, actions: [] }
      yield { type: 'done', session_id: 'sess_0001', latency_ms: 1, rejected_count: 0 }
    })())
    const wrapper = await renderView()
    const field = wrapper.find('[data-testid="field-message"]')
    await field.setValue('第一句')
    await wrapper.find('[data-testid="send"]').trigger('click')
    await flushPromises()

    await field.setValue('第二句：我在等结果的时候打的')
    ;(release as (() => void) | null)?.()
    await flushPromises()

    expect((field.element as HTMLTextAreaElement).value).toBe('第二句：我在等结果的时候打的')
  })
})

/**
 * 真机量过的一下：发一条消息拿到会话 `as_8bd49f9a44e2`，切到"一张图"看一眼再回来，
 * 时间线整段没了、会话号回到"未开始"，而这一页的能力条写着"会话保留 30 分钟"（后端给的 TTL）。
 * 值班员去图上确认那个沟在哪儿是再自然不过的动作，不该顺手把对话——以及没来得及签的
 * 待确认动作——一起丢掉，而且丢得不说一个字。
 */
describe('换页再回来：对话要接得上（后端会话还在时限内）', () => {
  const thread: AssistantFrame[] = [
    { type: 'meta', session_id: 'as_live_1', llm_configured: false, actions: ['warning.list'] },
    { type: 'intent', action: 'warning.list', args: {}, confidence: 0.8, decided_by: 'rule', note: '', requires_confirmation: false },
    { type: 'answer', text: '沟口那条村当前有 1 条红色预警' },
    { type: 'done', session_id: 'as_live_1', latency_ms: 5.1, rejected_count: 0 },
  ]

  it('卸载重挂之后，会话号与时间线都还在', async () => {
    mockedChat.mockReturnValue(framesOf(thread))
    const first = await renderView()
    await sendMessage(first, '沟口那个村现在什么状态')
    expect(byTestid(first, 'session-id').text()).toContain('as_live_1')
    expect(byTestid(first, 'frame-answer').text()).toContain('红色预警')
    first.unmount()

    const second = await renderView()
    expect(byTestid(second, 'session-id').text(), '换页回来该接上原来那段会话').toContain('as_live_1')
    expect(byTestid(second, 'frame-answer').text(), '原来那几帧还得在').toContain('红色预警')
  })

  /** F5 走的是同一条后端会话（30 分钟内），标签页还在，对话就该还在。 */
  it('整页刷新之后也接得上：状态在浏览器里存了一份', async () => {
    mockedChat.mockReturnValue(framesOf(thread))
    const first = await renderView()
    await sendMessage(first, '沟口那个村现在什么状态')
    first.unmount()

    reloadAssistantSession()
    const second = await renderView()
    expect(byTestid(second, 'session-id').text(), '刷新等于重新走一遍装载，该从浏览器存储接回来').toContain('as_live_1')
    expect(byTestid(second, 'frame-answer').text()).toContain('红色预警')
  })

  // 70 帧的渲染在 v8 覆盖率插桩下约为裸跑的 2.4 倍（本机实测 2.8s → 6.6s），
  // 5s 默认超时会把工具链开销误判成业务失败。只放宽这一条，不改断言。
  it('存回浏览器的只留最近 60 帧', async () => {
    const many: AssistantFrame[] = Array.from({ length: 70 }, (_unused, index) => ({ type: 'answer', text: `第 ${index} 句` }))
    mockedChat.mockReturnValue(framesOf(many))
    const wrapper = await renderView()
    await sendMessage(wrapper, '念一遍')
    await flushPromises()
    const raw = window.sessionStorage.getItem('aegis.assistant.session.v1')
    if (raw === null) throw new Error('没写进浏览器存储，刷新就接不上')
    expect((JSON.parse(raw) as { frames: AssistantFrame[] }).frames).toHaveLength(60)
  }, 15_000)

  /** 「清空时间线」是显式动作：清这一屏的记录，但会话号归会话号，后端那 30 分钟还接得上。 */
  it('清空时间线不顺手把会话号也丢了', async () => {
    mockedChat.mockReturnValue(framesOf(thread))
    const wrapper = await renderView()
    await sendMessage(wrapper, '沟口那个村现在什么状态')
    await wrapper.find('[data-testid="clear"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="frame-answer"]').exists()).toBe(false)
    expect(byTestid(wrapper, 'session-id').text()).toContain('as_live_1')
  })
})

/**
 * 值班台双栏与拉杆（page 模式专属）。
 *
 * 判据与驾驶舱那根拉杆同一份：多发 pointermove 只排一帧、帧落地前高度不动、
 * pointerup 才写记忆。竖排满栏里两板高度互补（拖一块另一块让位），夹在
 * [160, 栏高−缝−160]；键盘与双击复位走得通、刷新认记忆。
 * dock 模式不渲染右栏，告警与处置空间仍在聊天容器里。
 */
describe('值班台：左会话全高、右栏竖排互补拉杆', () => {
  const ALERTS_KEY = 'aegis.assistant.alerts-pane'
  const HANDLING_KEY = 'aegis.assistant.handling-pane'
  const DESK_KEY = 'aegis.assistant.desk-height'

  beforeEach(() => {
    localStorage.removeItem(ALERTS_KEY)
    localStorage.removeItem(HANDLING_KEY)
    localStorage.removeItem(DESK_KEY)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    localStorage.removeItem(ALERTS_KEY)
    localStorage.removeItem(HANDLING_KEY)
    localStorage.removeItem(DESK_KEY)
  })

  /** jsdom 没有布局：右栏全高与两板起始高度都得手动量给组件（800 栏 → 默认各 392）。 */
  function stubGeometry(wrapper: ReturnType<typeof mount>, column = 800, paneHeight = 392) {
    const side = wrapper.find('[data-testid="assistant-side"]')
    Object.defineProperty(side.element, 'clientHeight', { value: column, configurable: true })
    for (const id of ['alerts-pane', 'handling-pane']) {
      wrapper.find(`[data-testid="${id}"]`).element.getBoundingClientRect = () => ({ height: paneHeight }) as DOMRect
    }
    return side
  }

  function stubCapture(handle: { element: Element }) {
    const el = handle.element as HTMLElement
    el.setPointerCapture = vi.fn()
    el.hasPointerCapture = vi.fn(() => true)
    el.releasePointerCapture = vi.fn()
    return el
  }

  it('page 模式：会话在左栏全高，告警与处置空间竖排在右栏', async () => {
    const wrapper = await renderView()
    const desk = byTestid(wrapper, 'assistant-desk')
    expect(desk.exists()).toBe(true)
    expect(desk.find('.chat').exists()).toBe(true)
    const side = byTestid(wrapper, 'assistant-side')
    expect(side.find('[data-testid="alert-stack"]').exists()).toBe(true)
    expect(side.find('[data-testid="handling-bar"]').exists()).toBe(true)
    expect(wrapper.find('.chat [data-testid="alert-stack"]').exists()).toBe(false)
    expect(wrapper.find('.chat [data-testid="handling-bar"]').exists()).toBe(false)
    expect(wrapper.find('.chat [data-testid="timeline"]').exists()).toBe(true)
    expect(wrapper.find('.chat [data-testid="field-message"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('dock 模式不渲染右栏：告警与处置空间仍在聊天容器里', async () => {
    mockedCapabilities.mockResolvedValue(capabilities())
    const wrapper = mount(AssistantPanel, { props: { mode: 'dock' }, global: { stubs: STUBS } })
    await flushPromises()
    expect(wrapper.find('[data-testid="assistant-side"]').exists()).toBe(false)
    expect(wrapper.find('.chat [data-testid="alert-stack"]').exists()).toBe(true)
    expect(wrapper.find('.chat [data-testid="handling-bar"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('拉杆拖动合帧：pointermove 攒到 rAF 才写一次高度，pointerup 才提交记忆', async () => {
    const frames: FrameRequestCallback[] = []
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => frames.push(cb))
    vi.stubGlobal('cancelAnimationFrame', () => {})
    const wrapper = await renderView()
    stubGeometry(wrapper)
    const pane = byTestid(wrapper, 'alerts-pane')
    const other = byTestid(wrapper, 'handling-pane')
    const handle = byTestid(wrapper, 'alerts-pane-handle')
    stubCapture(handle)

    await handle.trigger('pointerdown', { clientY: 500, pointerId: 1 })
    await handle.trigger('pointermove', { clientY: 520, pointerId: 1 })
    await handle.trigger('pointermove', { clientY: 540, pointerId: 1 })
    await handle.trigger('pointermove', { clientY: 560, pointerId: 1 })
    // 帧回调落地前高度不动：多发事件合进同一帧，不许逐发回流
    expect(frames).toHaveLength(1)
    expect(pane.attributes('style') ?? '').not.toContain('height')
    frames.splice(0).forEach((cb) => cb(performance.now()))
    expect(pane.attributes('style')).toContain('height: 452px')
    // 互补：另一块当场让位到 800−16−452=332
    expect(other.attributes('style')).toContain('height: 332px')
    expect(handle.attributes('aria-valuenow')).toBe('452')
    expect(localStorage.getItem(ALERTS_KEY)).toBeNull()

    await handle.trigger('pointerup', { clientY: 560, pointerId: 1 })
    expect(localStorage.getItem(ALERTS_KEY)).toBe('452')
    expect(localStorage.getItem(HANDLING_KEY)).toBe('332')
    expect(pane.classes()).not.toContain('is-resizing')
    wrapper.unmount()
  })

  it('夹上限：拖到顶时另一块不许低于 160', async () => {
    const frames: FrameRequestCallback[] = []
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => frames.push(cb))
    vi.stubGlobal('cancelAnimationFrame', () => {})
    const wrapper = await renderView()
    stubGeometry(wrapper, 800, 392)
    const handle = byTestid(wrapper, 'alerts-pane-handle')
    stubCapture(handle)
    await handle.trigger('pointerdown', { clientY: 500, pointerId: 1 })
    await handle.trigger('pointermove', { clientY: 2000, pointerId: 1 })
    frames.splice(0).forEach((cb) => cb(performance.now()))
    expect(byTestid(wrapper, 'alerts-pane').attributes('style')).toContain('height: 624px')
    expect(byTestid(wrapper, 'handling-pane').attributes('style')).toContain('height: 160px')
    await handle.trigger('pointerup', { clientY: 2000, pointerId: 1 })
    expect(localStorage.getItem(ALERTS_KEY)).toBe('624')
    expect(localStorage.getItem(HANDLING_KEY)).toBe('160')
    wrapper.unmount()
  })

  it('键盘微调与 Home/End：每一步都落地并记住两块', async () => {
    const wrapper = await renderView()
    stubGeometry(wrapper, 800, 392)
    const handle = byTestid(wrapper, 'alerts-pane-handle')

    await handle.trigger('keydown', { key: 'ArrowDown' })
    expect(localStorage.getItem(ALERTS_KEY)).toBe('416')
    expect(localStorage.getItem(HANDLING_KEY)).toBe('368')
    await handle.trigger('keydown', { key: 'ArrowUp' })
    expect(localStorage.getItem(ALERTS_KEY)).toBe('392')
    await handle.trigger('keydown', { key: 'Home' })
    expect(localStorage.getItem(ALERTS_KEY)).toBe('160')
    expect(localStorage.getItem(HANDLING_KEY)).toBe('624')
    await handle.trigger('keydown', { key: 'End' })
    expect(localStorage.getItem(ALERTS_KEY)).toBe('624')
    // 无关按键不拦也不改
    await handle.trigger('keydown', { key: 'a' })
    expect(localStorage.getItem(ALERTS_KEY)).toBe('624')
    wrapper.unmount()
  })

  it('双击复位：两块一起回默认各半，两份记忆都清掉', async () => {
    const wrapper = await renderView()
    stubGeometry(wrapper, 800, 392)
    const handle = byTestid(wrapper, 'alerts-pane-handle')
    await handle.trigger('keydown', { key: 'ArrowDown' })
    expect(localStorage.getItem(ALERTS_KEY)).toBe('416')
    await handle.trigger('dblclick')
    expect(localStorage.getItem(ALERTS_KEY)).toBeNull()
    expect(localStorage.getItem(HANDLING_KEY)).toBeNull()
    expect(byTestid(wrapper, 'alerts-pane').attributes('style') ?? '').not.toContain('height')
    expect(byTestid(wrapper, 'handling-pane').attributes('style') ?? '').not.toContain('height')
    expect(handle.attributes('aria-valuenow')).toBe('392')
    wrapper.unmount()
  })

  it('刷新后按记忆恢复；存了坏值就当没存', async () => {
    localStorage.setItem(ALERTS_KEY, '452')
    localStorage.setItem(HANDLING_KEY, '332')
    const first = await renderView()
    expect(byTestid(first, 'alerts-pane').attributes('style')).toContain('height: 452px')
    expect(byTestid(first, 'handling-pane').attributes('style')).toContain('height: 332px')
    first.unmount()

    localStorage.setItem(ALERTS_KEY, '不是数字')
    const second = await renderView()
    expect(byTestid(second, 'alerts-pane').attributes('style') ?? '').not.toContain('height')
    second.unmount()
  })

  it('高屏存记忆、矮屏刷新：按当前栏高重新夹住，不让右栏溢出视口', async () => {
    localStorage.setItem(ALERTS_KEY, '452')
    localStorage.setItem(HANDLING_KEY, '332')
    // jsdom 没有 ResizeObserver：装一个能手动触发的假观察器，走真实"栏高变了"那条夹取路径
    let fireBudget: () => void = () => {}
    vi.stubGlobal(
      'ResizeObserver',
      class {
        constructor(cb: () => void) {
          fireBudget = cb
        }
        observe() {}
        disconnect() {}
      },
    )
    const wrapper = await renderView()
    stubGeometry(wrapper, 488, 236)
    fireBudget()
    await flushPromises()
    // 栏高 488：上限 488−16−160=312，另一块拿剩下的 160
    expect(byTestid(wrapper, 'alerts-pane').attributes('style')).toContain('height: 312px')
    expect(byTestid(wrapper, 'handling-pane').attributes('style')).toContain('height: 160px')
    expect(localStorage.getItem(ALERTS_KEY)).toBe('312')
    expect(localStorage.getItem(HANDLING_KEY)).toBe('160')
    wrapper.unmount()
  })

  /** jsdom 没有主内容盒：造一个量好尺寸的 .main-content，让 fitDesk 能算出"一屏适配"。 */
  function stubMain(fit: { client: number; scroll: number; top: number }) {
    const main = document.createElement('div')
    main.className = 'main-content'
    document.body.appendChild(main)
    Object.defineProperty(main, 'clientHeight', { value: fit.client, configurable: true })
    Object.defineProperty(main, 'scrollHeight', { value: fit.scroll, configurable: true })
    main.getBoundingClientRect = () => ({ top: fit.top }) as DOMRect
    return main
  }
  function stubBudgetObserver() {
    let fire: () => void = () => {}
    vi.stubGlobal(
      'ResizeObserver',
      class {
        constructor(cb: () => void) {
          fire = cb
        }
        observe() {}
        disconnect() {}
      },
    )
    return () => fire()
  }

  it('值班台默认高＝一屏适配×4/3：适配 488 就立 651 的最低高', async () => {
    const fire = stubBudgetObserver()
    const main = stubMain({ client: 700, scroll: 700, top: 100 })
    const wrapper = await renderView()
    const desk = byTestid(wrapper, 'assistant-desk')
    desk.element.getBoundingClientRect = () => ({ top: 214, bottom: 702, height: 488 }) as DOMRect
    fire()
    await flushPromises()
    // fit = 700 − (214−100) − (700−(702−100)) = 488；×4/3 ≈ 651
    expect(desk.attributes('style')).toContain('min-height: 651px')
    wrapper.unmount()
    main.remove()
  })

  it('整页拉杆：合帧写高、pointerup 才记忆、双击复位清记忆', async () => {
    const frames: FrameRequestCallback[] = []
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => frames.push(cb))
    vi.stubGlobal('cancelAnimationFrame', () => {})
    const fire = stubBudgetObserver()
    // desk 已高 651（比适配 488 多 163），主内容 scrollHeight 跟着长同样的量
    const main = stubMain({ client: 700, scroll: 863, top: 100 })
    const wrapper = await renderView()
    const desk = byTestid(wrapper, 'assistant-desk')
    desk.element.getBoundingClientRect = () => ({ top: 214, bottom: 865, height: 651 }) as DOMRect
    fire()
    await flushPromises()
    const handle = byTestid(wrapper, 'desk-handle')
    stubCapture(handle)
    await handle.trigger('pointerdown', { clientY: 500, pointerId: 1 })
    await handle.trigger('pointermove', { clientY: 520, pointerId: 1 })
    await handle.trigger('pointermove', { clientY: 540, pointerId: 1 })
    await handle.trigger('pointermove', { clientY: 560, pointerId: 1 })
    expect(frames.length, '三次 pointermove 只该攒出一帧').toBe(1)
    expect(desk.attributes('style') ?? '').not.toContain('height: 711px')
    frames.forEach((cb) => cb(0))
    expect(desk.attributes('style')).toContain('height: 711px')
    expect(localStorage.getItem(DESK_KEY)).toBe(null)
    await handle.trigger('pointerup', { clientY: 560, pointerId: 1 })
    expect(localStorage.getItem(DESK_KEY)).toBe('711')
    expect(handle.attributes('aria-valuenow')).toBe('711')
    await handle.trigger('dblclick')
    expect(localStorage.getItem(DESK_KEY)).toBe(null)
    expect(desk.attributes('style')).toContain('min-height: 651px')
    expect(desk.attributes('style')).not.toContain('height: 711px')
    wrapper.unmount()
    main.remove()
  })

  it('整页拉杆：没按下时 hover 的 pointermove/pointerup 不写高不记忆', async () => {
    const frames: FrameRequestCallback[] = []
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => frames.push(cb))
    vi.stubGlobal('cancelAnimationFrame', () => {})
    const fire = stubBudgetObserver()
    const main = stubMain({ client: 700, scroll: 863, top: 100 })
    const wrapper = await renderView()
    const desk = byTestid(wrapper, 'assistant-desk')
    desk.element.getBoundingClientRect = () => ({ top: 214, bottom: 865, height: 651 }) as DOMRect
    fire()
    await flushPromises()
    const handle = byTestid(wrapper, 'desk-handle')
    stubCapture(handle)
    await handle.trigger('pointermove', { clientY: 695, pointerId: 1 })
    await handle.trigger('pointerup', { clientY: 695, pointerId: 1 })
    expect(frames.length, '没按下不该攒帧').toBe(0)
    expect(desk.attributes('style') ?? '').not.toContain('height: 695px')
    expect(localStorage.getItem(DESK_KEY)).toBe(null)
    wrapper.unmount()
    main.remove()
  })

  it('整页拉杆：上限取适配×2 与内容自然高的较大者', async () => {
    const frames: FrameRequestCallback[] = []
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => frames.push(cb))
    vi.stubGlobal('cancelAnimationFrame', () => {})
    const fire = stubBudgetObserver()
    const main = stubMain({ client: 700, scroll: 863, top: 100 })
    const wrapper = await renderView()
    const desk = byTestid(wrapper, 'assistant-desk')
    desk.element.getBoundingClientRect = () => ({ top: 214, bottom: 865, height: 651 }) as DOMRect
    Object.defineProperty(desk.element, 'scrollHeight', { value: 951, configurable: true })
    fire()
    await flushPromises()
    const handle = byTestid(wrapper, 'desk-handle')
    stubCapture(handle)
    await handle.trigger('pointerdown', { clientY: 500, pointerId: 1 })
    await handle.trigger('pointermove', { clientY: 900, pointerId: 1 })
    frames.forEach((cb) => cb(0))
    // fit 488×2=976 > 自然高 951；651+400=1051 被夹到 976
    expect(desk.attributes('style')).toContain('height: 976px')
    wrapper.unmount()
    main.remove()
  })
})
