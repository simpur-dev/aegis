/**
 * offline.ts 单测：源可用性分类、状态机转移、探针（假 fetch，绝不触网）。
 */

import { describe, expect, it, vi } from 'vitest'

import type { ProbeFetch, ProbeTargets, SourceFacts } from './offline'
import {
  classifyFacts,
  collectFacts,
  initialMachine,
  isHtmlResponse,
  isLocalAssetUrl,
  PROBE_TIMEOUT_MS,
  probeSource,
  reduceOffline,
  RECOVERY_SAMPLES,
  severityOf,
  tileUrlForTemplate,
} from './offline'

function facts(overrides: Partial<SourceFacts> = {}): SourceFacts {
  return {
    browserOnline: true,
    apiReachable: true,
    basemapReachable: true,
    terrainReachable: true,
    localBasemapAvailable: true,
    ...overrides,
  }
}

describe('源可用性分类', () => {
  it('三路齐备才是 online', () => {
    expect(classifyFacts(facts())).toBe('online')
  })

  it('地形缺位 = degraded（椭球面兜底，图照画）', () => {
    expect(classifyFacts(facts({ terrainReachable: false }))).toBe('degraded')
  })

  it('瓦片模板缺位但 PMTiles 在 = online（同源底图仍可用）', () => {
    expect(classifyFacts(facts({ basemapReachable: false, localBasemapAvailable: true }))).toBe('online')
  })

  it('两路底图都没有 = degraded（只画地形与矢量）', () => {
    expect(classifyFacts(facts({ basemapReachable: false, localBasemapAvailable: false }))).toBe('degraded')
  })

  it('后端断 + 无本地底图 = offline', () => {
    expect(classifyFacts(facts({ apiReachable: false, basemapReachable: false, localBasemapAvailable: false }))).toBe('offline')
  })

  it('浏览器报断网：还有本地底图算 degraded，什么都没有才算 offline', () => {
    expect(classifyFacts(facts({ browserOnline: false, basemapReachable: false, terrainReachable: false }))).toBe('degraded')
    expect(
      classifyFacts(
        facts({ browserOnline: false, apiReachable: false, basemapReachable: false, localBasemapAvailable: false, terrainReachable: false }),
      ),
    ).toBe('offline')
  })

  it('严重度可比较：offline > degraded > online', () => {
    expect(severityOf('offline')).toBeGreaterThan(severityOf('degraded'))
    expect(severityOf('degraded')).toBeGreaterThan(severityOf('online'))
  })
})

describe('状态机转移：降档立即、升档要连续确认', () => {
  it('变差立即生效', () => {
    const online = { ...initialMachine('online'), at: 1 }
    const next = reduceOffline(online, { type: 'probe', facts: facts({ terrainReachable: false }), at: 2 })
    expect(next.status).toBe('degraded')
    expect(next.pendingSamples).toBe(0)
    expect(next.at).toBe(2)
    expect(next.reason).toContain('地形')
  })

  it('变好需连续 RECOVERY_SAMPLES 次探针，避免标签在降级/正常间跳', () => {
    let machine = reduceOffline(initialMachine('offline'), { type: 'probe', facts: facts({ apiReachable: false, terrainReachable: false }) })
    expect(machine.status).toBe('offline')
    for (let step = 1; step < RECOVERY_SAMPLES; step += 1) {
      machine = reduceOffline(machine, { type: 'probe', facts: facts() })
      expect(machine.status).toBe('offline')
      expect(machine.pendingSamples).toBe(step)
    }
    machine = reduceOffline(machine, { type: 'probe', facts: facts() })
    expect(machine.status).toBe('online')
    expect(machine.pendingSamples).toBe(0)
  })

  it('恢复过程中一次失败探针会把确认计数清零', () => {
    let machine = initialMachine('degraded')
    machine = reduceOffline(machine, { type: 'probe', facts: facts() })
    expect(machine.pendingStatus).toBe('online')
    machine = reduceOffline(machine, { type: 'probe', facts: facts({ basemapReachable: false, localBasemapAvailable: false }) })
    expect(machine.status).toBe('degraded')
    expect(machine.pendingStatus).toBeNull()
  })

  it('browser_offline 是硬信号：不等探针直接 offline', () => {
    const machine = reduceOffline(initialMachine('online'), { type: 'browser_offline', at: 99 })
    expect(machine.status).toBe('offline')
    expect(machine.at).toBe(99)
    expect(machine.reason).toContain('断开')
  })

  it('browser_online 只解除硬断网，状态留给下一次探针', () => {
    const machine = reduceOffline(initialMachine('offline'), { type: 'browser_online' })
    expect(machine.status).toBe('offline')
    expect(machine.reason).toContain('探针')
  })

  it('同状态重复探针只刷新理由，不产生抖动', () => {
    const start = initialMachine('online')
    const next = reduceOffline(start, { type: 'probe', facts: facts(), at: 5 })
    expect(next.status).toBe('online')
    expect(next.at).toBe(5)
  })
})

describe('同源资产判定（无外链约束的可测形式）', () => {
  it('同源相对路径通过', () => {
    expect(isLocalAssetUrl('/basemaps/{z}/{x}/{y}.png')).toBe(true)
    expect(isLocalAssetUrl('/terrain')).toBe(true)
  })

  it('协议相对路径与第三方主机一律拒绝', () => {
    expect(isLocalAssetUrl('//tiles.example.com/{z}/{x}/{y}.png')).toBe(false)
    expect(isLocalAssetUrl('https://tiles.example.com/terrain')).toBe(false)
    expect(isLocalAssetUrl('http://cdn.example.org/a.pmtiles')).toBe(false)
    expect(isLocalAssetUrl('ftp://host/a')).toBe(false)
    expect(isLocalAssetUrl('')).toBe(false)
    expect(isLocalAssetUrl('not a url')).toBe(false)
  })

  it('同 host 的绝对 URL 通过（部署到子域名也算同源）', () => {
    expect(isLocalAssetUrl('https://aegis.local/terrain', 'https://aegis.local')).toBe(true)
    expect(isLocalAssetUrl('https://other.local/terrain', 'https://aegis.local')).toBe(false)
  })
})

describe('探针', () => {
  it('模板替换 z/x/y', () => {
    expect(tileUrlForTemplate('/basemaps/{z}/{x}/{y}.png')).toBe('/basemaps/0/0/0.png')
    expect(tileUrlForTemplate('/b/{z}/{x}/{y}.webp', 12, 3, 5)).toBe('/b/12/3/5.webp')
    expect(tileUrlForTemplate('/b/{z}/{x}/{y}.png').split('{')).toHaveLength(1)
  })

  it('2xx 与 405 算可用，404/异常/空地址算不可用（且永不抛错）', async () => {
    const ok: ProbeFetch = async () => ({ ok: true, status: 200 })
    expect(await probeSource(ok, '/terrain/layer.json')).toBe(true)
    expect(await probeSource(async () => ({ ok: false, status: 405 }), '/x')).toBe(true)
    expect(await probeSource(async () => ({ ok: false, status: 404 }), '/x')).toBe(false)
    expect(await probeSource(async () => Promise.reject(new Error('ECONNREFUSED')), '/x')).toBe(false)
    expect(await probeSource(ok, '')).toBe(false)
  })

  it('200 但是 HTML 不算可用：SPA 兜底页与 captive portal 都会把未知路径回成 200 text/html', async () => {
    // 浏览器实测到的口径：`vite preview` 对不存在的 /basemaps/0/0/0.png 回 200 + text/html，
    // 只看状态码就会把一个 HTML 页面当成瓦片源挂上影像层。
    const withType = (mime: string): ProbeFetch => async () => ({
      ok: true,
      status: 200,
      headers: { get: (name: string) => (name === 'content-type' ? mime : null) },
    })
    expect(await probeSource(withType('text/html; charset=utf-8'), '/basemaps/0/0/0.png')).toBe(false)
    expect(await probeSource(withType('application/xhtml+xml'), '/x')).toBe(false)
    expect(await probeSource(withType('image/png'), '/basemaps/0/0/0.png')).toBe(true)
    expect(await probeSource(withType('application/vnd.pmtiles'), '/basemaps/aegis.pmtiles')).toBe(true)
    // HEAD 常常不带 content-type：这种"不知道"按可用处理，不误杀真资产。
    expect(await probeSource(withType(''), '/terrain/layer.json')).toBe(true)
    expect(isHtmlResponse(undefined)).toBe(false)
    expect(isHtmlResponse({ get: () => null })).toBe(false)
  })

  it('超时被 abort 吞掉，返回 false 而不是挂住', async () => {
    const hanging: ProbeFetch = (_url, init) =>
      new Promise((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => reject(new Error('aborted')))
      })
    await expect(probeSource(hanging, '/slow', 5)).resolves.toBe(false)
    expect(PROBE_TIMEOUT_MS).toBeGreaterThan(0)
  })

  it('abort 信号确实传给了 fetch（弱网下必须能取消，否则探针会堆积）', async () => {
    let received: AbortSignal | undefined
    const spy: ProbeFetch = async (_url, init) => {
      received = init?.signal ?? undefined
      return { ok: true, status: 200 }
    }
    await probeSource(spy, '/x', 50)
    expect(received?.aborted).toBe(false)
  })

  it('四路探针并发出结果并映射为事实', async () => {
    const called: string[] = []
    const fake: ProbeFetch = async (url) => {
      called.push(url)
      const ok = url !== '/terrain/layer.json'
      return { ok, status: ok ? 200 : 404 }
    }
    const targets: ProbeTargets = {
      api: '/healthz',
      basemapTemplate: '/basemaps/{z}/{x}/{y}.png',
      terrain: '/terrain/layer.json',
      localBasemap: '/basemaps/aegis.pmtiles',
    }
    const result = await collectFacts({ fetch: fake, browserOnline: false, targets })
    expect(called).toHaveLength(4)
    expect(called).toContain('/basemaps/0/0/0.png')
    expect(result).toEqual({
      browserOnline: false,
      apiReachable: true,
      basemapReachable: true,
      terrainReachable: false,
      localBasemapAvailable: true,
    })
    expect(classifyFacts(result)).toBe('degraded')
  })
})

/** navigator.onLine 的读取口径由调用方注入，这里只确认探针本身不依赖全局状态。 */
describe('探针不依赖真实网络', () => {
  it('没有 fetch 时调用方给的假实现即可跑通', async () => {
    const fake = vi.fn<ProbeFetch>(async () => ({ ok: false, status: 0 }))
    const machine = reduceOffline(initialMachine(), {
      type: 'probe',
      facts: await collectFacts({
        fetch: fake,
        browserOnline: false,
        targets: { api: '/healthz', basemapTemplate: '/b/{z}/{x}/{y}.png', terrain: '/t/layer.json', localBasemap: '/b/x.pmtiles' },
      }),
    })
    expect(fake).toHaveBeenCalledTimes(4)
    expect(machine.status).toBe('offline')
  })
})
