import { existsSync, readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

import type { IntegrationRow } from './integrations'
import { LEG_LABELS, MAX_DETAIL_CHARS, fetchIntegrations, legLabel, legState, legStateLabel, visibleDetail } from './integrations'

function row(overrides: Partial<IntegrationRow> = {}): IntegrationRow {
  return { name: 'store', enabled: true, driver: 'postgres', detail: {}, ...overrides }
}

/**
 * 读后端源文件，路径按"从 cwd 往上找第一个含 backend/src/aegis 的仓库根"解析：
 * vitest 里 `import.meta.url` 是 `/@fs/` 虚拟路径，直接拼会读不到文件。
 */
function backendSource(...parts: string[]): string {
  for (let dir = process.cwd(); ; dir = join(dir, '..')) {
    const root = resolve(dir, 'backend', 'src', 'aegis')
    const target = join(root, ...parts)
    if (existsSync(target)) return readFileSync(target, 'utf8')
    if (dirname(dir) === dir) throw new Error(`找不到 backend/src/aegis/${parts.join('/')}：漂移门禁必须在仓库内运行`)
  }
}

/** 后端把腿名写死在 `IntegrationState(name=...)`：直接对源文件，不在前端抄一份常量。 */
function declaredLegs(): string[] {
  const found = new Set<string>()
  for (const file of ['integrations.py', 'container.py']) {
    for (const match of backendSource(file).matchAll(/IntegrationState\(\s*name="([a-z_]+)"/g)) found.add(match[1] as string)
  }
  return [...found]
}

/** 后端真实会挂进 detail 的那段数据集出处说明（`knowledge/cases.py` 的 DATASET_PROVENANCE）。 */
function backendProvenance(): string {
  const group = backendSource('knowledge', 'cases.py').match(/DATASET_PROVENANCE = \(([\s\S]*?)\n\)/)
  if (!group) throw new Error('没抓到 DATASET_PROVENANCE：cases.py 改写后这条测试要跟着改')
  return [...group[1].matchAll(/"([^"]*)"/g)].map((piece) => piece[1]).join('')
}

describe('legState：三态而不是两态', () => {
  it('未启用是 disabled，不看 driver', () => {
    expect(legState(row({ enabled: false, driver: 'off' }))).toBe('disabled')
  })

  it('启用且无降级痕迹是 enabled', () => {
    expect(legState(row({ detail: { target: '127.0.0.1:5432' } }))).toBe('enabled')
  })

  it.each([
    ['degraded', ['dense-embedder']],
    ['start_error', 'OSError: 连接被拒'],
    ['warm_error', 'RuntimeError'],
  ])(
    '启用了但带 %s 的腿必须显眼为降级',
    (key, value) => {
      expect(legState(row({ detail: { [key]: value } }))).toBe('degraded')
    },
  )

  it('标记键带空值不算降级：健康行的 detail 里 degraded 键存在但为空串', () => {
    // 后端 `build_retrieval` 健康时也写 degraded=""；按键存在与否判定会把每条正常腿染成橙色。
    expect(legState(row({ detail: { degraded: '', warm_error: '   ', index: { analyzer: 'ngram(2)' } } }))).toBe('enabled')
    expect(legState(row({ detail: { error: 'ClickHouse 认证失败' } }))).toBe('degraded')
  })

  it('三态标签各自不同：把"没启用"和"降级"混成一个词就等于藏起故障', () => {
    const labels = (['disabled', 'enabled', 'degraded'] as const).map(legStateLabel)
    expect(new Set(labels).size).toBe(3)
    expect(labels).toEqual(['未启用', '运行中', '降级运行'])
  })
})

describe('legLabel：后端加腿不许从 UI 消失', () => {
  it.each([
    ['store', '运行态存储'],
    ['weather', '气象拉取腿'],
    ['tracing', '链路追踪'],
  ])('%s 有中文标签', (name, label) => {
    expect(legLabel(name)).toBe(label)
  })

  it('未登记的腿名原样显示，不返回空串也不抛错', () => {
    expect(legLabel('kafka')).toBe('kafka')
  })

  it('标签表与后端声明的腿严格同名（漂移门禁）', () => {
    expect([...declaredLegs()].sort()).toEqual(Object.keys(LEG_LABELS).sort())
  })

  it('后端源里真的抓到了七条腿：改写导致抓取为空时这条先失败，不让上面的相等变成空对空', () => {
    expect(declaredLegs()).toHaveLength(7)
  })
})

describe('visibleDetail：凭据类键一律不给看', () => {
  it('password/token/dsn/uri 这类键被过滤，业务键照常展示', () => {
    const detail = visibleDetail(
      row({
        detail: {
          target: '127.0.0.1:5432',
          fallback_cases: 16,
          pg_dsn: 'postgresql://aegis:sup3rs3cr3t@127.0.0.1:1/aegis',
          neo4j_password: 'hunter2',
          otel_endpoint_token: 'abc',
          exporting: true,
        },
      }),
    )
    const keys = detail.map((entry) => entry.key)
    expect(keys).toEqual(['target', 'fallback_cases', 'exporting'])
    expect(JSON.stringify(detail)).not.toContain('sup3rs3cr3t')
    expect(JSON.stringify(detail)).not.toContain('hunter2')
  })

  it.each([
    [null, '—'],
    ['', '—'],
    [false, 'false'],
    [3, '3'],
    [['a', 'b'], 'a、b'],
    [{ legs: 2 }, '{"legs":2}'],
  ])('值 %j 渲染成 %s', (value, expected) => {
    expect(visibleDetail(row({ detail: { k: value } }))[0]?.value).toBe(expected)
  })

  it('后端那段数据集说明放不进一行：截断展示，完整串留在 hint 上', () => {
    const provenance = backendProvenance()
    expect(provenance.length).toBeGreaterThan(MAX_DETAIL_CHARS)
    const entry = visibleDetail(row({ detail: { dataset: provenance } }))[0]
    expect(entry?.value).toHaveLength(MAX_DETAIL_CHARS)
    expect(entry?.value.endsWith('…')).toBe(true)
    expect(entry?.hint).toBe(provenance)
  })

  it('上限按字符数计，不按字节：中文串不会被切出半个字', () => {
    const long = '内'.repeat(MAX_DETAIL_CHARS + 36)
    expect(visibleDetail(row({ detail: { k: long } }))[0]?.value).toHaveLength(MAX_DETAIL_CHARS)
  })

  it('未截断的值不带 hint，避免给每个短值挂一个空 title', () => {
    expect(visibleDetail(row({ detail: { target: '127.0.0.1:5432' } }))[0]?.hint).toBe('')
  })

  it('detail 缺失（后端旧版本）不炸，返回空清单', () => {
    expect(visibleDetail({ name: 'x', enabled: true, driver: '', detail: undefined as unknown as Record<string, unknown> })).toEqual([])
  })
})

describe('fetchIntegrations：只认后端事实，不补字段', () => {
  const instance = (payload: unknown) => ({ request: async () => ({ data: payload }) }) as never

  it('行与降级清单都按后端原样映射', async () => {
    const snapshot = await fetchIntegrations(
      instance({
        items: [row({ name: 'mqtt', enabled: false, driver: 'off' }), row({ name: 'weather', driver: 'http', detail: { rounds: 2 } })],
        degraded: ['knowledge'],
        all_enabled: false,
      }),
    )
    expect(snapshot.items.map((item) => item.name)).toEqual(['mqtt', 'weather'])
    expect(snapshot.items[1]?.detail).toEqual({ rounds: 2 })
    expect(snapshot.degraded).toEqual(['knowledge'])
    expect(snapshot.all_enabled).toBe(false)
  })

  it('items 缺失或类型错时降级为空清单，而不是抛给渲染层', async () => {
    const snapshot = await fetchIntegrations(instance({ items: null }))
    expect(snapshot.items).toEqual([])
    expect(snapshot.degraded).toEqual([])
    expect(snapshot.all_enabled).toBe(false)
  })

  it('degraded 里塞非字符串也会被规整成字符串', async () => {
    const snapshot = await fetchIntegrations(instance({ items: [], degraded: ['store', 7, null] }))
    expect(snapshot.degraded).toEqual(['store', '7', 'null'])
  })

  it('后端返回 503 时抛 IntegrationsApiError 并带状态码', async () => {
    const axiosError = Object.assign(new Error('Request failed with status code 503'), {
      isAxiosError: true,
      response: { status: 503 },
    })
    await expect(fetchIntegrations({ request: async () => Promise.reject(axiosError) } as never)).rejects.toMatchObject({
      name: 'IntegrationsApiError',
      status: 503,
    })
  })
})
