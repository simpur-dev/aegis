/**
 * api/map.ts 单测：只断言"方法 + 路径 + 查询参数 + 错误包装"，不依赖真实后端。
 *
 * 手法与 `src/api/client.spec.ts`、`components/workflow/__tests__/api.spec.ts` 一致：
 * 注入桩适配器（axios adapter），因此这条链路完全离线可跑。
 */

import type { AxiosRequestConfig } from 'axios'
import axios, { AxiosError } from 'axios'
import { describe, expect, it } from 'vitest'

import type { StationDto } from '@/api/map'
import { createMapApiClient, isMissingResource, MAP_ASSET_PATHS, MapApiError, normalizeAnchors } from '@/api/map'

interface Recorded {
  method?: string
  url?: string
  params: Record<string, unknown>
}

function clientWith(handler: (config: AxiosRequestConfig) => { status: number; data: unknown }) {
  const seen: Recorded[] = []
  const instance = axios.create({
    baseURL: '/',
    adapter: async (config) => {
      seen.push({ method: config.method, url: config.url, params: (config.params ?? {}) as Record<string, unknown> })
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
  return { client: createMapApiClient(instance), seen }
}

describe('地图客户端：路径与参数', () => {
  it('遥测/预警/事件都打到后端真实存在的路由', async () => {
    const { client, seen } = clientWith(() => ({ status: 200, data: { count: 0, items: [] } }))
    await client.telemetry({ region_code: '540121', limit: 500 })
    await client.warnings({ limit: 20, region_code: '540121' })
    await client.events(50)
    expect(seen.map((entry) => entry.url)).toEqual(['/api/v1/telemetry', '/api/v1/warnings', '/api/v1/events'])
    expect(seen[0]?.params).toMatchObject({ region_code: '540121', limit: 500 })
    expect(seen[2]?.params).toEqual({ limit: 50 })
  })

  it('站点台账与静态资产路径集中在常量里（同源，无外链）', async () => {
    const { client, seen } = clientWith(() => ({ status: 200, data: { count: 0, items: [] } }))
    await client.stations({ limit: 500 })
    await client.regionAnchors()
    await client.hazardZones()
    expect(seen.map((entry) => entry.url)).toEqual(['/api/v1/stations', MAP_ASSET_PATHS.regionAnchors, MAP_ASSET_PATHS.hazardZones])
    for (const entry of seen) expect(entry.url).toMatch(/^\//)
    expect(MAP_ASSET_PATHS.terrain).toBe('/terrain')
    expect(MAP_ASSET_PATHS.basemapTemplate).toContain('{z}/{x}/{y}')
  })

  it('路由缺失时（旧版后端或未开放该路由的部署）404 识别为"资源缺失"而非故障', async () => {
    const { client } = clientWith((config) => (String(config.url).endsWith('/stations') ? { status: 404, data: { detail: 'Not Found' } } : { status: 200, data: {} }))
    const error = await client.stations().catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(MapApiError)
    expect(isMissingResource(error)).toBe(true)
    expect(isMissingResource(new MapApiError(500, 'boom'))).toBe(false)
    expect(isMissingResource(new Error('plain'))).toBe(false)
  })

  it('网络不可达时状态码为 0（离线时上层据此降级）', async () => {
    const instance = axios.create({
      adapter: async () => {
        throw new AxiosError('Network Error', 'ERR_NETWORK')
      },
    })
    const error = await createMapApiClient(instance).telemetry().catch((caught: unknown) => caught)
    expect((error as MapApiError).status).toBe(0)
  })

  it('detail 原样带出（供面板显示后端中文校验信息）', async () => {
    const { client } = clientWith(() => ({ status: 422, data: { detail: 'limit 必须为正' } }))
    const error = (await client.telemetry({ limit: 0 }).catch((caught: unknown) => caught)) as MapApiError
    expect(error.status).toBe(422)
    expect(error.detail).toEqual({ detail: 'limit 必须为正' })
  })
})

describe('锚点文件容错读取', () => {
  it('数组形态与 {items:[...]} 形态都吃', () => {
    expect(normalizeAnchors([{ code: '540121', lon: 91.2, lat: 30.0, name_zh: '林周县' }])).toEqual([
      { code: '540121', name_zh: '林周县', lon: 91.2, lat: 30.0 },
    ])
    expect(normalizeAnchors({ items: [{ code: '540221', lon: 89.1, lat: 29.6 }] })[0].name_zh).toBe('540221')
  })

  it('坏条目逐条丢弃，不整体失败', () => {
    const result = normalizeAnchors([
      { code: 'ok', lon: 91, lat: 30 },
      { code: '', lon: 91, lat: 30 },
      { code: 'str', lon: '91', lat: 30 },
      { lon: 91, lat: 30 },
      null,
      'string',
    ])
    expect(result.map((anchor) => anchor.code)).toEqual(['ok'])
  })

  it('(0,0) 这类形状合法但地理可疑的条目先留下，由 entities.toLngLat 统一裁决', () => {
    expect(normalizeAnchors([{ code: 'zero', lon: 0, lat: 0 }])).toHaveLength(1)
  })

  it('非 JSON 顶层（字符串/null）返回空表', () => {
    expect(normalizeAnchors('nope')).toEqual([])
    expect(normalizeAnchors(null)).toEqual([])
    expect(normalizeAnchors(undefined)).toEqual([])
  })
})

describe('站点 DTO：坐标一律可选（后端实况）', () => {
  it('lon/lat/geom 全缺也满足类型（不臆造字段）', () => {
    const bare: StationDto = { station_id: 'RG-540121-01' }
    expect(bare.lon ?? null).toBeNull()
    expect(bare.geom ?? null).toBeNull()
  })
})
