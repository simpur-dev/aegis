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
      "region_code：String does match '^[0-9A-Z]{6,24}$'；note：String should have at least 4 characters",
    )
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
