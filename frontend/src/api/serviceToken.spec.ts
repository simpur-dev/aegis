/**
 * 服务令牌（§4.8.1 前端一侧）的两类判据：
 * ① 令牌本身——存哪儿、什么时候发出去、401 怎么记账；
 * ② **覆盖面**：每一个 axios 实例与每一处 `fetch` 都带上它。漏一处的症状是固定的：
 *    开了令牌的部署里那一条链永远 401，而其它页面看起来正常，于是没人怀疑是凭据。
 */

import { readdirSync } from 'node:fs'
import { join } from 'node:path'
import axios, { type AxiosInstance, type InternalAxiosRequestConfig } from 'axios'
import { beforeEach, describe, expect, it } from 'vitest'

import {
  TOKEN_HEADER,
  authRejected,
  clearServiceToken,
  installServiceToken,
  serviceToken,
  setServiceToken,
  tokenHeaders,
} from './serviceToken'
import { readRepoFile, repoRoot } from '@/testing/repoSource'

/** 记下最后一次真正发出去的请求头（不碰网络：adapter 直接回 200）。 */
function probeInstance(): { instance: AxiosInstance; sent: () => Record<string, string> } {
  let captured: Record<string, string> = {}
  const instance = axios.create({
    adapter: async (config: InternalAxiosRequestConfig) => {
      captured = Object.fromEntries(
        Object.entries(config.headers ?? {}).filter(([, value]) => value !== undefined) as [string, string][],
      )
      return { data: {}, status: 200, statusText: 'OK', headers: {}, config }
    },
  })
  installServiceToken(instance)
  return { instance, sent: () => captured }
}

describe('serviceToken 本身', () => {
  beforeEach(() => {
    clearServiceToken()
    globalThis.localStorage.clear()
  })

  it('未设置时不给任何凭据头（空串比空头更接近事实）', () => {
    expect(serviceToken.value).toBe('')
    expect(tokenHeaders()).toEqual({})
  })

  it('设置后写进 localStorage，并去掉首尾空白', () => {
    setServiceToken('  s3cr3t  ')
    expect(serviceToken.value).toBe('s3cr3t')
    expect(tokenHeaders()[TOKEN_HEADER]).toBe('s3cr3t')
    expect(globalThis.localStorage.getItem('aegis.serviceToken')).toBe('s3cr3t')
  })

  it('清除是把存储也删掉，不是留一个空串', () => {
    setServiceToken('s3cr3t')
    clearServiceToken()
    expect(serviceToken.value).toBe('')
    expect(globalThis.localStorage.getItem('aegis.serviceToken')).toBeNull()
  })

  it('存不下去时只在内存生效，不假装已经保存', () => {
    const broken = { getItem: () => '', removeItem: () => {}, setItem: () => { throw new Error('隐私模式') } }
    const original = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
    Object.defineProperty(globalThis, 'localStorage', { value: broken, configurable: true })
    try {
      setServiceToken('abc')
      expect(serviceToken.value).toBe('abc')
      expect(tokenHeaders()[TOKEN_HEADER]).toBe('abc')
    } finally {
      if (original) Object.defineProperty(globalThis, 'localStorage', original)
    }
  })
})

describe('axios 拦截器', () => {
  beforeEach(() => clearServiceToken())

  it('带着令牌时每个请求都发 X-Aegis-Token', async () => {
    const { instance, sent } = probeInstance()
    setServiceToken('s3cr3t')
    await instance.get('/api/v1/warnings')
    expect(sent()[TOKEN_HEADER]).toBe('s3cr3t')
  })

  it('没带令牌时不发这个头', async () => {
    const { instance, sent } = probeInstance()
    await instance.get('/api/v1/warnings')
    expect(sent()[TOKEN_HEADER]).toBeUndefined()
  })

  it('401 记一笔"被鉴权面拒了"，保存新令牌后清账', async () => {
    const { instance } = probeInstance()
    const failing = axios.create({
      adapter: async (config) => {
        throw new axios.AxiosError('nope', 'ERR_BAD_REQUEST', config, {}, { status: 401 } as never)
      },
    })
    installServiceToken(failing)
    await expect(failing.get('/api/v1/telemetry')).rejects.toThrow()
    expect(authRejected.value).toBe(true)

    setServiceToken('s3cr3t')
    expect(authRejected.value).toBe(false)
    void instance
  })
})

describe('覆盖面：谁发请求谁带凭据', () => {
  /**
   * 每个 api 模块自带 axios 实例是刻意的隔离边界，但"隔离"不该等于"漏了鉴权"。
   *
   * 这份名单原来是手抄的六个模块名——加一个模块（推演沙盒就是第七个）时没人回来加它，
   * 于是"覆盖面"退化成"抄写一致性"：新模块裸奔，门禁照样绿。改成扫目录：
   * 凡是自己 `axios.create(` 的模块都必须装拦截器，不认列表。
   */
  function instanceOwningModules(): string[] {
    const dir = join(repoRoot(), 'frontend', 'src', 'api')
    const owners: string[] = []
    const walk = (relative: string): void => {
      const absolute = relative === '' ? dir : join(dir, relative)
      for (const entry of readdirSync(absolute, { withFileTypes: true })) {
        const next = relative === '' ? entry.name : `${relative}/${entry.name}`
        if (entry.isDirectory()) {
          walk(next)
          continue
        }
        if (!entry.name.endsWith('.ts') || entry.name.endsWith('.spec.ts') || entry.name.endsWith('.d.ts')) continue
        if (readRepoFile('frontend', 'src', 'api', ...next.split('/')).includes('axios.create(')) {
          owners.push(next.replace(/\.ts$/, ''))
        }
      }
    }
    walk('')
    return owners
  }

  it('扫得到足够多的模块：扫描为空就是外壳坏了，不是"没有漏点"', () => {
    expect(instanceOwningModules().length).toBeGreaterThanOrEqual(7)
  })

  it('凡是自建 axios 实例的 api 模块都装了令牌拦截器', () => {
    for (const name of instanceOwningModules()) {
      const source = readRepoFile('frontend', 'src', 'api', ...`${name}.ts`.split('/'))
      expect(source, `${name}.ts 没装拦截器：开了令牌的部署里它那条链必 401`).toContain('installServiceToken(')
    }
  })

  /**
   * `EventSource` 发不了请求头——这条流的鉴权口径只能靠 fetch 拿回来。
   * 留着它就等于在"整个 /api/ 都要凭据"的面上留一条永久裸奔的通道。
   */
  it('全仓不再用 EventSource，事件流改走 fetch 并带凭据头', () => {
    const stream = readRepoFile('frontend', 'src', 'composables', 'useEventStream.ts')
    expect(stream).not.toContain('new EventSource(')
    expect(stream, '事件流没带凭据头：这条流在开了令牌的部署里连不上').toContain('tokenHeaders()')
  })

  it('助手的流式对话（POST + SSE，也用 fetch）带凭据头', () => {
    const assistant = readRepoFile('frontend', 'src', 'api', 'assistant.ts')
    expect(assistant).toContain('tokenHeaders()')
  })
})
