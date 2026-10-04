/**
 * api/reports.ts 单测：路径、线上格式收敛、以及 422 那条"后端原文不许改写"的路。
 *
 * 手法与 `components/map/api.spec.ts` 一致（注入 axios 桩适配器，全程离线）。
 * 另外把 `ParseLeg` 的取值集合钉在后端源上：三路融合的腿名是解析器的对外语义
 * （`services/semantic_parser.py` 的 `LEG_*` 常量），前端抄一份改名的清单就等于把
 * "等级从哪一路来"这件事又翻译了一次。
 */

import type { AxiosRequestConfig } from 'axios'
import axios, { AxiosError } from 'axios'
import { describe, expect, it } from 'vitest'

import type { ParseLeg, ReportOutcomeDto } from './reports'
import {
  createReportsApiClient,
  describeValidationError,
  isParserUnavailable,
  legSourceLabel,
  LEG_SOURCE_LABELS,
  REPORT_ENDPOINT,
  REPORT_FIELD_LABELS,
  ReportApiError,
  toReportBody,
} from './reports'
import { readRepoFile } from '@/testing/repoSource'

function clientWith(handler: (config: AxiosRequestConfig) => { status: number; data: unknown }) {
  const seen: AxiosRequestConfig[] = []
  const instance = axios.create({
    baseURL: '/',
    adapter: async (config) => {
      seen.push(config)
      const result = handler(config)
      if (result.status >= 400) {
        throw new AxiosError('request failed', 'ERR_BAD_REQUEST', config, {}, {
          status: result.status,
          statusText: 'error',
          headers: {},
          data: result.data,
          config,
        })
      }
      return { data: result.data, status: result.status, statusText: 'OK', headers: {}, config }
    },
  })
  return { client: createReportsApiClient(instance), seen }
}

/** 从装配面源码里解析三路融合的腿名集合：真源在后端，不在这里抄。 */
function backendLegNames(): string[] {
  const text = readRepoFile('backend', 'src', 'aegis', 'services', 'semantic_parser.py')
  const declared = [...text.matchAll(/^LEG_[A-Z_]+ = "([a-z_]+)"$/gm)].map((match) => match[1] as string)
  if (declared.length === 0) {
    throw new Error('semantic_parser.py 里一个 LEG_ 常量都没解析到：写法变了，门禁得跟着改（不是把断言删掉）')
  }
  // 无阈值命中时 decided_by 兜底成字面量 "none"（semantic_parser.py:576），它不在 LEG_ 常量里。
  if (!/decided_by = None, "none"/.test(text)) {
    throw new Error('semantic_parser.py 里找不到 `"none"` 这条兜底：定级来源集合变了，这里要跟着改')
  }
  return [...new Set([...declared, 'none'])].sort()
}

function outcome(overrides: Partial<ReportOutcomeDto> = {}): ReportOutcomeDto {
  return {
    report: { reporter: '巡护员扎西', region_code: '540121', location: null },
    parse: {
      text: '24小时累计降雨95毫米，沟道泥位抬升1.2米',
      hazard_type: 'debris_flow',
      region_code: '540121',
      risk_level: 1,
      confidence: 0.95,
      decided_by: 'rule',
      needs_review: false,
      entities: ['沟道'],
      metrics: [{ metric: 'rain_24h', value: 95, unit: 'mm' }],
      reported_people: 300,
      trigger_hits: [
        { rule_id: 'R-RAIN-1', hazard_type: 'debris_flow', region_code: '540121', evidence_refs: ['note=…'], score: 0.9, observed_at: '2026-10-03T04:00:00Z' },
      ],
      legs: [
        { leg: 'rule', hazard_type: 'debris_flow', risk_level: 1, confidence: 0.95, rationale: '降雨阈值命中', refs: ['R-RAIN-1'], used: true },
        { leg: 'rule_declared', hazard_type: 'debris_flow', risk_level: null, confidence: 0, rationale: '上报文本未写明等级', refs: [], used: false },
        { leg: 'retrieval', hazard_type: 'debris_flow', risk_level: null, confidence: 0, rationale: '佐证 2 条；同灾种佐证 2 条', refs: [], used: false },
        { leg: 'llm', hazard_type: null, risk_level: null, confidence: 0, rationale: 'LLM 未配置，裁决腿缺席', refs: [], used: false },
      ],
      conflicts: [],
      degradations: [],
      evidence: [{ source: 'case-1', hazard_type: 'debris_flow', typical_level: 2 }],
    },
    human_review_required: false,
    review: null,
    intake_seconds: 0.123,
    chain: {
      trace_id: 'tr_1',
      event_id: 'ev_1',
      ok: true,
      acted: true,
      stages: [{ name: 'perceive', mode: 'local', ok: true, latency_ms: 1, note: '上报文本判定' }],
      risk: null,
      task_units: ['stu_1'],
      warning_id: 'wrn_1',
      errors: [],
      degradations: [],
      reference_cases: ['case-1'],
      context_docs: [],
    },
    ...overrides,
  }
}

describe('toReportBody：线上格式只有一处收敛点', () => {
  it('空白提示与半边坐标都不带键：后端把"只有一个坐标"整段读成未定位', () => {
    expect(toReportBody({ reporter: '村民', region_code: '540121', note: '坡面有裂缝', hazard_hint: '', lat: 29.65, lon: null })).toEqual({
      reporter: '村民',
      region_code: '540121',
      note: '坡面有裂缝',
    })
  })

  it('经纬度成对时才带上，且保持 lat/lon 两个独立字段', () => {
    expect(toReportBody({ reporter: '村民', region_code: '540121', note: '发生泥石流', hazard_hint: '泥石流', lat: 29.65, lon: 91.13 })).toEqual({
      reporter: '村民',
      region_code: '540121',
      note: '发生泥石流',
      hazard_hint: '泥石流',
      lat: 29.65,
      lon: 91.13,
    })
  })
})

describe('提交路径与回执', () => {
  it('POST 到 /api/v1/reports，body 就是收敛后的线上格式', async () => {
    const { client, seen } = clientWith(() => ({ status: 200, data: outcome() }))
    await client.submit(toReportBody({ reporter: '村民', region_code: '540121', note: '沟道泥位抬升', hazard_hint: null, lat: null, lon: null }))
    expect(seen[0]?.method).toBe('post')
    expect(seen[0]?.url).toBe(REPORT_ENDPOINT)
    expect(JSON.parse(String(seen[0]?.data))).toEqual({ reporter: '村民', region_code: '540121', note: '沟道泥位抬升' })
  })

  it('回执里的 legs / conflicts / degradations 原样到前端，不裁字段', async () => {
    const seeded = outcome()
    seeded.parse.conflicts = ['等级分歧：采纳 rule 的 1 级 vs LLM 3 级']
    seeded.parse.degradations = ['等级仅由 LLM 建议得出，转人工核签']
    const { client } = clientWith(() => ({ status: 200, data: seeded }))
    const result = await client.submit(toReportBody({ reporter: '村民', region_code: '540121', note: '我要红色预警' }))
    expect(result.parse.legs.map((leg) => leg.leg)).toEqual(['rule', 'rule_declared', 'retrieval', 'llm'])
    expect(result.parse.conflicts).toEqual(['等级分歧：采纳 rule 的 1 级 vs LLM 3 级'])
    expect(result.parse.degradations).toEqual(['等级仅由 LLM 建议得出，转人工核签'])
    expect(result.chain.warning_id).toBe('wrn_1')
    expect(result.chain.reference_cases).toEqual(['case-1'])
  })

  it('未定级是一条真实结果：risk_level 为 null 且没有 warning_id', async () => {
    const seeded = outcome()
    seeded.parse.risk_level = null
    seeded.parse.decided_by = 'none'
    seeded.parse.confidence = 0
    seeded.human_review_required = true
    seeded.chain.warning_id = null
    const { client } = clientWith(() => ({ status: 200, data: seeded }))
    const result = await client.submit(toReportBody({ reporter: '村民', region_code: '540121', note: '坡面有裂缝' }))
    expect(result.parse.risk_level).toBe(null)
    expect(result.parse.decided_by).toBe('none')
    expect(result.human_review_required).toBe(true)
    expect(result.chain.warning_id).toBe(null)
  })
})

describe('错误外显：后端原文不许被翻译', () => {
  it('422 的校验数组拼成一行，带上后端点名的字段', async () => {
    const detail = {
      detail: [
        { loc: ['body', 'region_code'], msg: "String does match '^[0-9A-Z]{6,24}$'", type: 'string_pattern_mismatch' },
        { loc: ['body', 'note'], msg: 'String should have at least 4 characters', type: 'string_too_short' },
      ],
    }
    const { client } = clientWith(() => ({ status: 422, data: detail }))
    const error = await client.submit(toReportBody({ reporter: '村民', region_code: '5401', note: '短' })).catch((reason: unknown) => reason)
    expect(error).toBeInstanceOf(ReportApiError)
    expect((error as ReportApiError).status).toBe(422)
    expect((error as ReportApiError).detail).toEqual(detail)
    expect(describeValidationError(error)).toBe(
      "区划代码（region_code）：String does match '^[0-9A-Z]{6,24}$'；险情描述（note）：String should have at least 4 characters",
    )
  })

  /**
   * 报错里的字段名必须是页面上那个中文名。
   *
   * 真机在表单里敲四个空格、填五位区划码，界面回的是
   * `reporter：String should have at least 2 characters；region_code：…` ——
   * 规则原文照抄是对的（那是后端口径），但字段名得用页面自己的叫法，
   * 否则值班员要先做一次中英对照才知道刚才动的是哪一格。
   */
  it('后端 ReportIn 的每个字段都有中文标签（新增字段不许漏）', () => {
    const text = readRepoFile('backend', 'src', 'aegis', 'api', 'app.py')
    const block = text.slice(text.indexOf('class ReportIn'), text.indexOf('def get_container'))
    const fields = [...block.matchAll(/^\s{4}([a-z_]+)\s*:\s/gm)].map((m) => m[1] as string)
    expect(fields.length, '解析不到 ReportIn 的字段：写法变了要一起改这条门禁').toBeGreaterThanOrEqual(6)
    for (const field of fields) {
      expect(REPORT_FIELD_LABELS[field], `ReportIn.${field} 没有中文名，报错行会露出裸键名`).toBeTruthy()
    }
  })

  it('嵌套 loc 保留路径形状，只给首段加中文', () => {
    const error = new ReportApiError(422, 'Request failed', {
      detail: [{ loc: ['body', 'note'], msg: 'String should have at most 2000 characters', type: 'string_too_long' }],
    })
    expect(describeValidationError(error)).toBe('险情描述（note）：String should have at most 2000 characters')
  })

  it('认不出的字段名原样给出，不编一个中文标签', () => {
    const error = new ReportApiError(422, 'Request failed', {
      detail: [{ loc: ['body', 'brand_new_field'], msg: 'Field required', type: 'missing' }],
    })
    expect(describeValidationError(error)).toBe('brand_new_field：Field required')
  })

  it('422 的 detail 是字符串时也照原样给出去', () => {
    expect(describeValidationError(new ReportApiError(422, 'msg', { detail: 'limit 必须为正' }))).toBe('limit 必须为正')
  })

  it('非 422 不编造校验文案：返回空串，由上层显示原始错误', () => {
    expect(describeValidationError(new ReportApiError(500, '服务器内部错误', 'boom'))).toBe('')
    expect(describeValidationError(new Error('不是上报错误'))).toBe('')
  })

  it('解析腿未装配（503 + E_PARSER_UNAVAILABLE）能被上层单独区分', async () => {
    const { client } = clientWith(() => ({ status: 503, data: { detail: { code: 'E_PARSER_UNAVAILABLE', message: '任务解析服务未装配' } } }))
    const error = await client.submit(toReportBody({ reporter: '村民', region_code: '540121', note: '沟道泥位抬升' })).catch((reason: unknown) => reason)
    expect(isParserUnavailable(error)).toBe(true)
    expect((error as ReportApiError).code).toBe('E_PARSER_UNAVAILABLE')
    expect(describeValidationError(error)).toBe('')
  })

  it('后端不可达时状态码记 0，不冒充 422', async () => {
    const instance = axios.create({
      adapter: async () => {
        throw new AxiosError('connect ECONNREFUSED', 'ERR_NETWORK')
      },
    })
    const error = await createReportsApiClient(instance)
      .submit(toReportBody({ reporter: '村民', region_code: '540121', note: '沟道泥位抬升' }))
      .catch((reason: unknown) => reason)
    expect((error as ReportApiError).status).toBe(0)
    expect(isParserUnavailable(error)).toBe(false)
  })
})

describe('定级来源标签', () => {
  it('五个来源各有中文名，且互不相同（把"申报值"和"阈值命中"混成一个词就等于改了口径）', () => {
    const labels = (Object.keys(LEG_SOURCE_LABELS) as ParseLeg[]).map(legSourceLabel)
    expect(new Set(labels).size).toBe(labels.length)
    expect(legSourceLabel('rule_declared')).toContain('申报')
    expect(legSourceLabel('none')).toContain('未')
  })

  it('后端将来加的腿名原样显示，不返回空串也不抛错', () => {
    expect(legSourceLabel('graph')).toBe('graph')
  })

  it('标签表与后端 LEG_ 常量严格同名（漂移门禁）', () => {
    expect(Object.keys(LEG_SOURCE_LABELS).sort()).toEqual(backendLegNames())
  })
})

/**
 * 核签工单的两端键名（跨端漂移门禁）。
 *
 * 真源是 `container._open_report_review()` 返回的那个字典：后端改键名而前端没跟上时，
 * 界面只会显示"未开出"，看上去像"这条不用核签"——把一个待办悄悄读成没有待办，
 * 是这张工单最坏的失效方式。所以这里比的是**两端的键集合**，不是两份抄写。
 */
function backendReviewTicketKeys(): string[] {
  const text = readRepoFile('backend', 'src', 'aegis', 'container.py')
  const start = text.indexOf('async def _open_report_review')
  if (start < 0) throw new Error('container.py 里找不到 _open_report_review：开单逻辑改名或搬走了，门禁要跟着改（不是把断言删掉）')
  const block = /\n        return \{([\s\S]*?)\n        \}/.exec(text.slice(start))
  if (!block) throw new Error('没解析到 _open_report_review 的返回字典：写法变了，这里要跟着改')
  const keys = [...block[1].matchAll(/^ {12}"([a-z_]+)":/gm)].map((match) => match[1] as string)
  if (keys.length < 4) throw new Error(`工单键数解析异常（只数到 ${keys.length} 个）：先修解析器再谈断言`)
  return [...new Set(keys)].sort()
}

function frontendReviewTicketKeys(): string[] {
  const text = readRepoFile('frontend', 'src', 'api', 'reports.ts')
  const block = /export interface ReportReviewDto \{([\s\S]*?)\n\}/.exec(text)
  if (!block) throw new Error('reports.ts 里没有 ReportReviewDto：前端把工单类型删了或改名了')
  const keys = [...block[1].matchAll(/^\s{2}(\w+)[?]?:/gm)].map((match) => match[1] as string)
  if (keys.length < 4) throw new Error(`前端工单键数解析异常（只数到 ${keys.length} 个）`)
  return [...new Set(keys)].sort()
}

describe('人工核签工单', () => {
  it('回执把工单号、待签节点与签核入口原样带到界面', async () => {
    const seeded = outcome({
      human_review_required: true,
      review: {
        workflow_id: 'wf_review_1',
        instance_id: 'wfi_01abc',
        status: 'waiting',
        pending_node: 'review',
        options: ['approve', 'adjust', 'reject'],
        decision_endpoint: '/api/v1/workflow/instances/{instance_id}/nodes/{node_id}/decision',
      },
    })
    const { client } = clientWith(() => ({ status: 200, data: seeded }))
    const result = await client.submit(toReportBody({ reporter: '村民', region_code: '540200', note: '发生泥石流，请求红色预警' }))
    expect(result.review?.instance_id).toBe('wfi_01abc')
    expect(result.review?.pending_node).toBe('review')
    // 三态不许在前端被裁成一态：签字人看到的是后端给的那三个选项
    expect(result.review?.options).toEqual(['approve', 'adjust', 'reject'])
  })

  it('核签流程没注册时后端给 null，前端据此显示"没开出工单"而不是空工单号', async () => {
    const seeded = outcome({ human_review_required: true, review: null })
    const { client } = clientWith(() => ({ status: 200, data: seeded }))
    const result = await client.submit(toReportBody({ reporter: '村民', region_code: '540200', note: '发生泥石流' }))
    expect(result.human_review_required).toBe(true)
    expect(result.review).toBe(null)
  })

  it('工单键集合与后端返回字典严格同名（漂移门禁）', () => {
    expect(frontendReviewTicketKeys()).toEqual(backendReviewTicketKeys())
  })
})
