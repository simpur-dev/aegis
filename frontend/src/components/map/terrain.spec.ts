/**
 * terrain.ts 单测：quantized-mesh 与椭球面兜底的**决策**，以及"fromUrl 失败仍能出图"的装配链。
 * 这里给 createTerrainSetup 喂假 Cesium 命名空间（不 import cesium）。
 */

import { describe, expect, it } from 'vitest'

import type { TerrainCesium, TerrainChoice } from './terrain'
import { chooseTerrainMode, createTerrainSetup, DEFAULT_TERRAIN_URL, describeError, normalizeTerrainUrl, terrainProbeUrl } from './terrain'

function choice(overrides: Partial<TerrainChoice> = {}): TerrainChoice {
  return { mode: 'ellipsoid', reason: '测试', url: null, ...overrides }
}

describe('地形探针地址', () => {
  it('默认走同源 /terrain，元数据入口是 layer.json', () => {
    expect(DEFAULT_TERRAIN_URL).toBe('/terrain')
    expect(normalizeTerrainUrl('/terrain/')).toBe('/terrain')
    expect(normalizeTerrainUrl('terrain///')).toBe('terrain')
    expect(terrainProbeUrl('/terrain/')).toBe('/terrain/layer.json')
  })
})

describe('兜底决策', () => {
  it('没配地址 → 椭球面', () => {
    const result = chooseTerrainMode({ configuredUrl: null, layerJsonReachable: true, browserOnline: true })
    expect(result.mode).toBe('ellipsoid')
    expect(result.url).toBeNull()
    expect(result.reason).toContain('未配置')
  })

  it('空串地址 → 椭球面', () => {
    expect(chooseTerrainMode({ configuredUrl: '   ', layerJsonReachable: true, browserOnline: true }).mode).toBe('ellipsoid')
  })

  it('第三方地形主机被拒（即使它探得到），并说明理由', () => {
    const result = chooseTerrainMode({
      configuredUrl: 'https://host.example.com/terrain',
      layerJsonReachable: true,
      browserOnline: true,
      origin: 'http://localhost',
    })
    expect(result.mode).toBe('ellipsoid')
    expect(result.reason).toContain('非同源')
  })

  it('layer.json 取不到 → 椭球面，理由里点名 layer.json', () => {
    const result = chooseTerrainMode({ configuredUrl: '/terrain', layerJsonReachable: false, browserOnline: true })
    expect(result.mode).toBe('ellipsoid')
    expect(result.reason).toContain('/terrain/layer.json')
    expect(result.url).toBe('/terrain')
  })

  it('断网且未烘焙 → 椭球面（离线也能出图）', () => {
    const result = chooseTerrainMode({ configuredUrl: '/terrain', layerJsonReachable: false, browserOnline: false })
    expect(result.mode).toBe('ellipsoid')
    expect(result.reason).toContain('断网')
  })

  it('三项都满足才用 quantized-mesh', () => {
    expect(chooseTerrainMode({ configuredUrl: '/terrain/', layerJsonReachable: true, browserOnline: true })).toMatchObject({
      mode: 'quantized-mesh',
      url: '/terrain',
    })
  })
})

describe('装配与二次兜底', () => {
  /** 只实现用到的两个静态入口：地形失败也必须返回可用 provider，不能把异常抛到页面。 */
  function fakeCesium(behaviour: 'ok' | 'throw'): { calls: string[]; cesium: TerrainCesium } {
    const calls: string[] = []
    const provider = { kind: 'quantized-mesh' }
    const ellipsoid = { kind: 'ellipsoid' }
    const cesium = {
      CesiumTerrainProvider: {
        fromUrl: async (url: string) => {
          calls.push(url)
          if (behaviour === 'throw') throw new Error('layer.json 不是 quantized-mesh')
          return provider
        },
      },
      EllipsoidTerrainProvider: class {
        constructor() {
          calls.push('ellipsoid')
          return ellipsoid
        }
      },
    }
    return { calls, cesium: cesium as unknown as TerrainCesium }
  }

  it('quantized-mesh 成功：provider 与模式一致', async () => {
    const { calls, cesium } = fakeCesium('ok')
    const setup = await createTerrainSetup(cesium, choice({ mode: 'quantized-mesh', url: '/terrain', reason: 'ok' }))
    expect(setup.mode).toBe('quantized-mesh')
    expect(setup.provider).toEqual({ kind: 'quantized-mesh' })
    expect(calls).toEqual(['/terrain'])
    expect(setup.message).toBe('ok')
  })

  it('fromUrl 抛错 → 退椭球面并在 message 里留原因', async () => {
    const { calls, cesium } = fakeCesium('throw')
    const setup = await createTerrainSetup(cesium, choice({ mode: 'quantized-mesh', url: '/terrain', reason: '探针说可用' }))
    expect(setup.mode).toBe('ellipsoid')
    expect(setup.provider).toEqual({ kind: 'ellipsoid' })
    expect(calls).toContain('ellipsoid')
    expect(setup.message).toContain('已回退椭球面')
    expect(setup.message).toContain('layer.json')
  })

  it('决策为椭球面时不去请求地形服务', async () => {
    const { calls, cesium } = fakeCesium('ok')
    const setup = await createTerrainSetup(cesium, choice({ reason: '未配置自托管地形服务，使用椭球面' }))
    expect(setup.mode).toBe('ellipsoid')
    expect(calls).toEqual(['ellipsoid'])
    expect(setup.message).toContain('椭球面')
  })

  it('非 Error 抛掷也能被描述（不让日志格式化本身崩掉）', () => {
    expect(describeError(new Error('x'))).toBe('x')
    expect(describeError('字符串故障')).toBe('字符串故障')
    expect(describeError(undefined)).toBe('undefined')
  })
})
