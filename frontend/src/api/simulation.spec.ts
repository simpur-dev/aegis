/**
 * 推演沙盒契约层（`api/simulation.ts`）的用例。
 *
 * 三条主线：
 * ① **取值域不许抄**：`SIMULATION_LIMITS` / `SIMULATION_DEFAULTS` / 预案白名单都现读后端
 *    `SimulationRequest` 比对——抄一份清单只能证明两份抄写一致，证明不了后端没改；
 * ② 报告进渲染层前的边界校验：缺键/空时间线/没票的时间线一律判读失败，
 *    因为"显示成空表"正是多角色推演退化成四个字符串却看不出来的形状；
 * ③ 凭据与错误面：POST 带 `X-Aegis-Token`，422 说中文并带页面叫法。
 */

import type { AxiosRequestConfig } from 'axios'
import axios, { AxiosError } from 'axios'
import { describe, expect, it } from 'vitest'

import {
  PLAYBOOK_ACTIONS,
  SIMULATION_DEFAULTS,
  SIMULATION_ENDPOINT,
  SIMULATION_LIMITS,
  SimulationApiError,
  createSimulationApiClient,
  describeSimulationError,
  preflightSimulation,
  requireSimulationReport,
  roleLabel,
  toSimulationBody,
  type SimulationBody,
} from './simulation'
import { installServiceToken, clearServiceToken, setServiceToken, TOKEN_HEADER } from '@/api/serviceToken'
import { readRepoFile } from '@/testing/repoSource'

/** 后端 `SimulationRequest` 的每一格：default / ge / le / pattern。 */
interface BackendField {
  name: string
  default: number | string
  ge?: number
  le?: number
  pattern?: string
}

function backendSimulationFields(): BackendField[] {
  const text = readRepoFile('backend', 'src', 'aegis', 'api', 'routers', 'drill.py')
  const start = text.indexOf('class SimulationRequest(BaseModel):')
  if (start < 0) throw new Error('routers/drill.py 里找不到 SimulationRequest：类名变了就改门禁，别把断言删掉')
  const body = text.slice(start, text.indexOf('\nclass ', start + 10))
  // Python 允许 `10_000_000` 这种下划线分隔写法，`Number()` 读不出来：先剥掉。
  const num = (raw: string | undefined): number | undefined => (raw === undefined ? undefined : Number(raw.replace(/_/g, '')))
  const found: BackendField[] = []
  // 贪心吃到本行最后一个 `)`：`pattern="^(转移|封路|不行动)$"` 里面就带括号，
  // 用 `[^)]*` 会在第一个括号处断开，那格直接被解析式吞掉（这条是靠"应 ≥ 7"的守卫抓出来的）。
  for (const match of body.matchAll(/^ {4}(\w+): \w+ = Field\((.*)\)$/gm)) {
    const name = match[1] as string
    const args = match[2] as string
    const defaultRaw = /default=([^,\n]+)/.exec(args)?.[1]?.trim()
    if (defaultRaw === undefined) throw new Error(`${name} 这一格没有 default：门禁止步，需要人工确认后端形状`)
    const numeric = num(defaultRaw)
    found.push({
      name,
      default: numeric === undefined || Number.isNaN(numeric) ? defaultRaw.replace(/^"|"$/g, '') : numeric,
      ge: num(/ge=([-\d._]+)/.exec(args)?.[1]),
      le: num(/le=([-\d._]+)/.exec(args)?.[1]),
      pattern: /pattern="([^"]+)"/.exec(args)?.[1],
    })
  }
  if (found.length < 7) throw new Error(`只从 SimulationRequest 解析出 ${found.length} 格（应 ≥ 7）：解析式已失效`)
  return found
}

function validBody(overrides: Partial<SimulationBody> = {}): SimulationBody {
  return { ...SIMULATION_DEFAULTS, ...overrides }
}

function reportFixture() {
  return {
    mode: 'demo_sandbox',
    note: '概率/暴露/缓解系数为未实测初值。',
    cost: { requested_scenarios: 8, effective_scenarios: 8, downscaled: false },
    probability_matrix: [{ rain_bucket_mm: '40-50', scenarios: 8, mean_secondary_rate: 0.41, max_secondary_rate: 0.55 }],
    timeline: [
      {
        round: 0,
        rain_mm: 42,
        mudlevel_cm: 3.3,
        actions: { commander: '转移', rescue: '转移', residents: '转移', monitor: '转移' },
        applied_playbook: '转移',
        mitigation: 0.6,
        trigger_probability: 0.132,
      },
    ],
    playbook_comparison: [{ playbook: '转移', mitigation: 0.6, mean_secondary_rate: 0.13, exposure_proxy: 26 }],
    roles: {
      policy: 'rule',
      roles: [
        { key: 'commander', title: '指挥员', concern: '取舍' },
        { key: 'rescue', title: '救援队', concern: '通路' },
        { key: 'residents', title: '受影响群众', concern: '预警' },
        { key: 'monitor', title: '监测员', concern: '趋势' },
      ],
      rounds: 1,
      asked: 4,
      calls: 4,
      fallbacks: 0,
      distinct_actions: ['转移'],
    },
  }
}

function clientWith(handler: (config: AxiosRequestConfig) => { status: number; data: unknown }) {
  const seen: AxiosRequestConfig[] = []
  const instance = axios.create({
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
  installServiceToken(instance)
  return { client: createSimulationApiClient(instance), seen }
}

describe('取值域与后端同源', () => {
  const fields = backendSimulationFields()

  it('每个有 ge/le 的数值格，前端上限逐格等于后端', () => {
    for (const field of fields) {
      if (field.ge === undefined || field.le === undefined) continue
      const local = SIMULATION_LIMITS[field.name as keyof typeof SIMULATION_LIMITS]
      expect(local, `后端有 ${field.name} 的区间，前端却没登记`).toBeDefined()
      expect(local.min, `${field.name} 下限漂移`).toBe(field.ge)
      expect(local.max, `${field.name} 上限漂移`).toBe(field.le)
    }
  })

  it('表单初值就是后端 Field 的 default（前端不许自估一个"看起来合理"的默认）', () => {
    for (const field of fields) {
      expect((SIMULATION_DEFAULTS as unknown as Record<string, number | string>)[field.name], `${field.name} 默认值漂移`).toBe(field.default)
    }
  })

  it('预案白名单与后端 pattern 逐字一致', () => {
    const pattern = fields.find((field) => field.name === 'playbook')?.pattern
    expect(pattern, '后端 playbook 不再是字面白名单：这一格要重新想清楚前端该怎么拦').toBeDefined()
    const accepted = (pattern as string).replace(/^\^/, '').replace(/\$$/, '').replace(/^\(/, '').replace(/\)$/, '').split('|')
    expect(accepted).toEqual([...PLAYBOOK_ACTIONS])
  })
})

describe('表单收敛与预检', () => {
  it('清空（null）回落到后端默认值，不是 0 也不是空串', () => {
    const body = toSimulationBody({ scenarios: null, rounds: undefined, rain_base_mm: null })
    expect(body.scenarios).toBe(SIMULATION_DEFAULTS.scenarios)
    expect(body.rounds).toBe(SIMULATION_DEFAULTS.rounds)
    expect(body.rain_base_mm).toBe(SIMULATION_DEFAULTS.rain_base_mm)
  })

  it('预案留空回落到"转移"，与后端 default 同格', () => {
    expect(toSimulationBody({ playbook: '   ' }).playbook).toBe('转移')
  })

  it('越界在发出前就拦住，并说清是哪一格、要求多少', () => {
    expect(preflightSimulation(validBody({ scenarios: 65 }))).toContain('情景条数 65 超出后端的 1..64')
    expect(preflightSimulation(validBody({ rain_base_mm: -1 }))).toContain('基准降雨量')
    expect(preflightSimulation(validBody({ rounds: 25 }))).toContain('推演轮数')
    expect(preflightSimulation(validBody({ token_budget: 0 }))).toContain('令牌预算')
    expect(preflightSimulation(validBody({ scenarios: Number.NaN }))).toContain('不是一个数')
  })

  it('同义改写不是白名单：这一条不发出去', () => {
    const reason = preflightSimulation(validBody({ playbook: '组织群众转移' }))
    expect(reason).toContain('不在白名单里')
    expect(reason).toContain('转移 / 封路 / 不行动')
  })

  it('种子必须整数（后端 int，给 1.5 会吃 422）', () => {
    expect(preflightSimulation(validBody({ seed: 1.5 }))).toContain('必须是整数')
  })

  it('合规形状返回空串', () => {
    expect(preflightSimulation(validBody())).toBe('')
  })
})

describe('报告边界校验', () => {
  it('完整报告原样交出，且角色名用后端给的名字', () => {
    const report = requireSimulationReport(reportFixture())
    expect(report.roles.policy).toBe('rule')
    expect(report.timeline[0]?.applied_playbook).toBe('转移')
    expect(roleLabel({ key: 'commander', title: '', concern: '' })).toBe('commander')
    const first = report.roles.roles[0]
    expect(first && roleLabel(first)).toBe('指挥员')
  })

  it('缺键就判读失败并点名缺哪几格（不画空表）', () => {
    const broken = { ...reportFixture() } as Record<string, unknown>
    delete broken.roles
    delete broken.note
    let message = ''
    try {
      requireSimulationReport(broken)
    } catch (error) {
      message = (error as SimulationApiError).message
    }
    expect(message).toContain('推演报告缺键')
    expect(message).toContain('roles')
    expect(message).toContain('note')
  })

  it('时间线为空 = 一票都没有，不能算跑过', () => {
    const noRounds = { ...reportFixture(), timeline: [] }
    expect(() => requireSimulationReport(noRounds)).toThrow(/时间线是空的/)
  })

  it('某一轮没带角色票就拒绝渲染', () => {
    const rowless = { ...reportFixture(), timeline: [{ ...reportFixture().timeline[0], actions: {} }] }
    expect(() => requireSimulationReport(rowless)).toThrow(/没带角色票/)
  })

  it('角色档列不出在场角色就拒绝（"是谁选的"说不清）', () => {
    const noRoles = { ...reportFixture(), roles: { ...reportFixture().roles, roles: [] } }
    expect(() => requireSimulationReport(noRoles)).toThrow(/没列出在场角色/)
  })

  it('不是对象（null / 字符串）也判读失败', () => {
    expect(() => requireSimulationReport(null)).toThrow(/缺键/)
    expect(() => requireSimulationReport('x')).toThrow(/缺键/)
  })
})

describe('请求与错误面', () => {
  it('POST 到沙盒出口，body 是七格齐全的形状', async () => {
    const { client, seen } = clientWith(() => ({ status: 200, data: reportFixture() }))
    await client.run(validBody())
    expect(seen[0]?.url).toBe(SIMULATION_ENDPOINT)
    expect(seen[0]?.method).toBe('post')
    // adapter 拿到的是 transformRequest 之后的 JSON 串，所以这里先解回来再比键
    const sent = JSON.parse(String(seen[0]?.data)) as Record<string, unknown>
    expect(Object.keys(sent).sort()).toEqual(
      ['playbook', 'rain_base_mm', 'rain_spread_mm', 'rounds', 'scenarios', 'seed', 'token_budget'].sort(),
    )
    expect(sent.scenarios).toBe(SIMULATION_DEFAULTS.scenarios)
    expect(sent.playbook).toBe('转移')
  })

  it('开了服务令牌时这一页带凭据（§4.8.1：漏一处的症状是整页 401）', async () => {
    const token = 'ci-only-token-value'
    const { client, seen } = clientWith(() => ({ status: 200, data: reportFixture() }))
    setServiceToken(token)
    try {
      await client.run(validBody())
      expect(JSON.stringify(seen[0]?.headers ?? {}), '请求头里没有凭据：这一页在开令牌的部署里必 401').toContain(token)
    } finally {
      clearServiceToken()
    }
  })

  it('没配令牌时不带空头：不把 `X-Aegis-Token: ""` 发出去让人误判成"带了但错了"', async () => {
    const { client, seen } = clientWith(() => ({ status: 200, data: reportFixture() }))
    clearServiceToken()
    await client.run(validBody())
    expect(JSON.stringify(seen[0]?.headers ?? {})).not.toContain(TOKEN_HEADER)
  })

  it('响应形状不合契约时报读失败，而不是把残缺交给渲染层', async () => {
    const { client } = clientWith(() => ({ status: 200, data: { mode: 'demo_sandbox' } }))
    await expect(client.run(validBody())).rejects.toThrow(/缺键/)
  })

  it('422 用页面叫法说清哪一格不合格', async () => {
    const detail = { detail: [{ loc: ['body', 'scenarios'], msg: 'Input should be less than or equal to 64', type: 'less_than_equal' }] }
    const { client } = clientWith(() => ({ status: 422, data: detail }))
    const error = await client.run(validBody({ scenarios: 999 })).catch((raised: unknown) => raised)
    expect(error).toBeInstanceOf(SimulationApiError)
    expect(describeSimulationError(error)).toContain('情景条数')
    expect(describeSimulationError(error)).toContain('less than or equal to 64')
  })

  it('非 422 保留接口原文（超时/连不上时那句才是事实）', async () => {
    const { client } = clientWith(() => ({ status: 503, data: 'unavailable' }))
    const error = await client.run(validBody()).catch((raised: unknown) => raised)
    expect(describeSimulationError(error)).toContain('HTTP 503')
  })
})

describe('生产实例本身带凭据', () => {
  /**
   * 上面那两条用的是测试注入的实例，装拦截器的是测试自己——**这证明不了模块里那个 `http` 装了**。
   * 变异实测过这个空档：把 `installServiceToken(http)` 从模块里删掉，注入实例的两条照绿。
   * 所以这里对源码打一条（手法同 `appWiring.spec.ts` 对 `main.ts` 只做装配对账）：
   * 建实例那一行之后紧跟着一行装拦截器。全局的"凡自建实例必装拦截器"另有 `serviceToken.spec.ts` 扫。
   */
  it('模块级实例创建后立刻装拦截器', () => {
    const source = readRepoFile('frontend', 'src', 'api', 'simulation.ts')
    expect(source).toMatch(/const http = axios\.create\(\{[^}]*\}\)\ninstallServiceToken\(http\)/)
  })
})
