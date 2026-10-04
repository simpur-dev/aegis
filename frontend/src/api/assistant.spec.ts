/**
 * 助手契约层的两类钉子。
 *
 * ① 能力面形状兜底：缺字段时页面读的是"读到了但这项没有"，不是白屏。
 *    这里钉的是 `example` 会不会在归一化时被丢掉——丢掉的话建议条全部禁用，
 *    页面看起来"有 12 个动作但一个都点不动"，而那正是本次要修的缺陷原样。
 * ② 非 2xx 的原因还原：`/chat`（fetch）与其余三条（axios）必须都翻成人话。
 */

import axios, { AxiosError } from 'axios'
import { describe, expect, it } from 'vitest'

import { readRepoFile } from '@/testing/repoSource'

import {
  ASSISTANT_ENDPOINTS,
  AssistantApiError,
  CHAT_LIMITS,
  capabilitiesShape,
  createAssistantApiClient,
  preflightChat,
  streamChat,
  type ChatResponseLike,
} from './assistant'

function rawAction(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    action: 'query.warnings',
    title: '查询已发布预警',
    requires_confirmation: false,
    needs: ['list_warnings'],
    available: true,
    missing: [],
    example: '最近发布了哪些预警',
    ...overrides,
  }
}

describe('capabilitiesShape 的 example', () => {
  it('原样带出后端给的示例句', () => {
    const caps = capabilitiesShape({ actions: [rawAction()] })
    expect(caps.actions[0]?.example).toBe('最近发布了哪些预警')
  })

  it('后端没给时兜底成空串而不是 undefined', () => {
    const caps = capabilitiesShape({ actions: [rawAction({ example: undefined })] })
    expect(caps.actions[0]?.example).toBe('')
  })

  it('示例句类型不对也不崩（非字符串按空串处理）', () => {
    const caps = capabilitiesShape({ actions: [rawAction({ example: 12 })] })
    expect(caps.actions[0]?.example).toBe('')
  })
})

/**
 * 第二类钉子：非 2xx 的**原因**必须到界面上。
 *
 * 真机测出来的原样是：贴一段 2001 字的文本发对话，页面只说
 * "对话中断：对话请求失败（HTTP 422）"——后端那句
 * `String should have at most 2000 characters` 整段被丢了。
 * `/chat`（fetch）与其余三条（axios）此前各有自己的写法，两边都得钉住。
 */

function responseOf(status: number, body: unknown): ChatResponseLike {
  return {
    ok: status < 400,
    status,
    body: null,
    json: async () => body,
    text: async () => JSON.stringify(body),
  }
}

async function firstError(status: number, body: unknown): Promise<AssistantApiError> {
  try {
    for await (const _ of streamChat({ message: 'x' }, { fetchImpl: async () => responseOf(status, body) })) {
      // 非 2xx 时不应该有帧；真出现就是契约变了
    }
  } catch (error) {
    expect(error).toBeInstanceOf(AssistantApiError)
    return error as AssistantApiError
  }
  throw new Error('预期抛出 AssistantApiError，实际流程走通了')
}

describe('非 2xx 的原因还原', () => {
  it('/chat 的 422 数组带出字段中文名与后端原文', async () => {
    const error = await firstError(422, {
      detail: [{ loc: ['body', 'message'], msg: 'String should have at most 2000 characters', type: 'string_too_long' }],
    })
    expect(error.status).toBe(422)
    expect(error.message).toBe('对话请求失败（HTTP 422）：对话内容（message）：String should have at most 2000 characters')
  })

  it('/chat 的 503 读的是后端那句装配事实，不是 HTTP 码', async () => {
    const error = await firstError(503, { detail: { code: 'E_ASSISTANT_UNAVAILABLE', message: '语义交互服务未装配（assistant）' } })
    expect(error.message).toBe('对话请求失败（HTTP 503）：语义交互服务未装配（assistant）')
    expect(error.code).toBe('E_ASSISTANT_UNAVAILABLE')
  })

  it('认不出形状的响应体回落到 HTTP 码那句，不显示空原因', async () => {
    const error = await firstError(400, '不是 JSON 也不像校验错误')
    expect(error.message).toBe('对话请求失败（HTTP 400）：不是 JSON 也不像校验错误')
  })

  it('axios 那三条同样翻原因（confirm 的 action_id 模式错）', async () => {
    const instance = axios.create({
      baseURL: '/',
      adapter: async (config) => {
        throw new AxiosError('request failed', 'ERR_BAD_REQUEST', config, {}, {
          status: 422,
          statusText: 'error',
          headers: {},
          data: { detail: [{ loc: ['body', 'action_id'], msg: "String does not match '^act_[0-9a-f]{6,24}$'" }] },
          config,
        })
      },
    })
    await expect(createAssistantApiClient(instance).confirm({ session_id: 'sess_0001', action_id: 'bad' })).rejects.toThrow(
      '助手接口调用失败（HTTP 422）：动作号（action_id）：String does not match \'^act_[0-9a-f]{6,24}$\'',
    )
  })

  it('会话 404 显示"会话不存在或已过期"，不是一句 Not Found', async () => {
    const instance = axios.create({
      baseURL: '/',
      adapter: async (config) => {
        throw new AxiosError('request failed', 'ERR_BAD_REQUEST', config, {}, {
          status: 404,
          statusText: 'error',
          headers: {},
          data: { detail: { code: 'E_SESSION_NOT_FOUND', message: '会话不存在或已过期', session_id: 'sess_0001' } },
          config,
        })
      },
    })
    const error = await createAssistantApiClient(instance)
      .session('sess_0001')
      .then(() => null)
      .catch((caught: unknown) => caught as AssistantApiError)
    expect(error?.message).toBe('助手接口调用失败（HTTP 404）：会话不存在或已过期')
    expect(error?.code).toBe('E_SESSION_NOT_FOUND')
  })

  it('没有响应体时保留 axios 自己的话（超时/连不上不能被翻成空原因）', async () => {
    const instance = axios.create({
      baseURL: '/',
      adapter: async (config) => {
        throw new AxiosError('timeout of 20000ms exceeded', 'ECONNABORTED', config)
      },
    })
    const error = await createAssistantApiClient(instance)
      .capabilities()
      .then(() => null)
      .catch((caught: unknown) => caught as AssistantApiError)
    expect(error?.status).toBe(0)
    expect(error?.message).toContain('timeout of 20000ms exceeded')
  })

  it('路由没挂载（404 且 detail 是纯文本）仍判为"出口未启用"', async () => {
    const instance = axios.create({
      baseURL: '/',
      adapter: async (config) => {
        throw new AxiosError('request failed', 'ERR_BAD_REQUEST', config, {}, {
          status: 404,
          statusText: 'error',
          headers: {},
          data: { detail: 'Not Found' },
          config,
        })
      },
    })
    const error = await createAssistantApiClient(instance)
      .capabilities()
      .then(() => null)
      .catch((caught: unknown) => caught as AssistantApiError)
    expect(error?.code).toBe('')
    expect(error?.status).toBe(404)
  })
})

describe('端点清单', () => {
  it('四条路径与后端路由一致', () => {
    expect(ASSISTANT_ENDPOINTS.capabilities).toBe('/api/v1/assistant/capabilities')
    expect(ASSISTANT_ENDPOINTS.chat).toBe('/api/v1/assistant/chat')
    expect(ASSISTANT_ENDPOINTS.confirm).toBe('/api/v1/assistant/confirm')
    expect(ASSISTANT_ENDPOINTS.session('sess_0001')).toBe('/api/v1/assistant/sessions/sess_0001')
  })
})

/**
 * 本地口径与后端源码对账（跨端漂移门禁）。
 *
 * 抄来的数字最坏的地方是"悄悄过期"：后端把上限从 2000 调到 800，前端还在按 2000 放行，
 * 症状是"贴着上限写的一句话莫名失败"。所以这里不写死期望值，直接读 `assistant_api.py` 比对；
 * 解析不到约束时抛错，不让门禁空跑成绿色。
 */
describe('CHAT_LIMITS 与后端 ChatRequest 对账', () => {
  const source = readRepoFile('backend', 'src', 'aegis', 'api', 'assistant_api.py')

  function fieldLine(name: string): string {
    const start = source.indexOf('class ChatRequest(')
    if (start < 0) throw new Error('assistant_api.py 里找不到 class ChatRequest：请求体重命名了，门禁要跟着改')
    const block = source.slice(start, source.indexOf('class ConfirmRequest', start))
    const match = block.match(new RegExp(`^[ \\t]+${name}: .*$`, 'm'))
    if (match === null) throw new Error(`后端 ChatRequest 里没有 ${name} 这一格：口径变了`)
    return match[0]
  }

  function intOf(line: string, key: 'min_length' | 'max_length'): number {
    const match = line.match(new RegExp(`${key}=(\\d[\\d_]*)`))
    if (match === null) throw new Error(`后端那行没写 ${key}：${line.trim()}`)
    return Number(match[1]?.replace(/_/g, ''))
  }

  function patternOf(line: string): string {
    const match = line.match(/pattern=(?:r)?"([^"]+)"/) ?? line.match(/pattern=(_[A-Z_]+)/)
    if (match === null) throw new Error(`后端那行没写 pattern：${line.trim()}`)
    if (match[1] !== undefined && match[1].startsWith('_')) {
      const constant = source.match(new RegExp(`${match[1]} = r?"([^"]+)"`))
      if (constant === null) throw new Error(`后端引用了 ${match[1]}，但源码里没找到它的定义`)
      return constant[1] as string
    }
    return match[1] as string
  }

  it('消息长度上下界与后端一致', () => {
    const line = fieldLine('message')
    expect(CHAT_LIMITS.message.min).toBe(intOf(line, 'min_length'))
    expect(CHAT_LIMITS.message.max).toBe(intOf(line, 'max_length'))
  })

  it('上报人名义上下界与后端一致', () => {
    const line = fieldLine('reporter')
    expect(CHAT_LIMITS.reporter.min).toBe(intOf(line, 'min_length'))
    expect(CHAT_LIMITS.reporter.max).toBe(intOf(line, 'max_length'))
  })

  it('区划代码的正则与后端同一条', () => {
    expect(CHAT_LIMITS.region_code.pattern.source).toBe(patternOf(fieldLine('region_code')))
  })

  it('灾种提示上界与后端一致', () => {
    expect(CHAT_LIMITS.hazard_hint.max).toBe(intOf(fieldLine('hazard_hint'), 'max_length'))
  })
})

/**
 * 发出前拦一次：这些形状在后端都是 422，差别只是"要不要等一个来回才知道"。
 */
describe('preflightChat', () => {
  it('合法的一条放行', () => {
    expect(preflightChat({ message: '最近发布了哪些预警', reporter: '值班员', region_code: '540121' })).toBe('')
  })

  it('空消息拦下，并带出后端那句 1..2000', () => {
    expect(preflightChat({ message: '' })).toBe(`对话内容为空：后端要求 ${CHAT_LIMITS.message.min}..${CHAT_LIMITS.message.max} 字，这一条没有发出去。`)
  })

  it('刚好到上限的放行，多一字拦下（边界不能靠感觉）', () => {
    const atLimit = '预'.repeat(CHAT_LIMITS.message.max)
    expect(preflightChat({ message: atLimit })).toBe('')
    expect(preflightChat({ message: `${atLimit}预` })).toContain(`${CHAT_LIMITS.message.max + 1} 字`)
  })

  it('上报人名义 1 字拦下、2 字放行', () => {
    expect(preflightChat({ message: '查预警', reporter: '李' })).toContain('上报人名义「李」')
    expect(preflightChat({ message: '查预警', reporter: '扎西' })).toBe('')
  })

  it('区划代码小写、过短、含连字符都拦下（后端要 6..24 位大写字母数字）', () => {
    for (const bad of ['54012a', '54012', '5401-21', '5401 21']) {
      expect(preflightChat({ message: '查预警', region_code: bad }), bad).toContain(`区划代码「${bad}」`)
    }
    expect(preflightChat({ message: '查预警', region_code: '540121' })).toBe('')
    expect(preflightChat({ message: '查预警', region_code: 'A'.repeat(24) })).toBe('')
  })

  it('可选键留空等于不填，不会被 min_length 判死', () => {
    expect(preflightChat({ message: '查预警', reporter: '', region_code: '' })).toBe('')
  })

  it('会话号不在这里挡：它是后端给的，本地拒绝会把整场对话锁死', () => {
    expect(preflightChat({ message: '查预警', session_id: 'not a valid id' })).toBe('')
  })
})
