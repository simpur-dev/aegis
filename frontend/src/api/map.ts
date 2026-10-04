/**
 * 「一张图」地图页专用契约层（后端 `backend/src/aegis/api/app.py` 的前端视图）。
 *
 * 与 `@/api/workflow.ts` 同一隔离边界：自带 axios 实例与错误包装，**不**从 `@/api/client` 导入符号，
 * 这样其他 agent 改动通用客户端时不会波及这里（并行开发的隔离边界）。
 *
 * 后端事实（决定本模块能声明什么字段，不臆造）：
 * - 带坐标的实体只存在于 Postgres 表 `monitoring_stations`
 *   （`persistence/geo.py:31` 的 STATION_FIELDS + `geom`，geom 文本口径见 `geo.py:71` point_text），
 *   经 `GET /api/v1/stations`（`api/app.py:171`）开放：只有维表真的写了经纬度才返回坐标，
 *   报过数但未登记的站返回 `lon/lat = null`。区划与灾面**没有**后端路由，只能取离线静态资产；
 *   坐标缺失时上层降级为「未定位」，而不是按区划中心猜一个经纬度；
 * - `GET /api/v1/telemetry` 的 `TelemetryReading` 只有 `station_id`/`region_code`，无经纬度；
 * - `GET /api/v1/warnings` 的 `WarningRecord` 只有 `region_codes`，无经纬度；
 * - `GET /api/v1/events` 的 `ChainSummary.risk` 提供 `region_code` 级 `risk_level`（区域风险的唯一来源）。
 *   注意 `storage/store.py:45-49` 的 `latest()` 返回追加序（旧→新），故「同区域最后一条」才是最新。
 *
 * 结论：本模块所有坐标字段都可为 null，由 `components/map/entities.ts` 决定「有则落点、无则入未定位清单」。
 */

import axios, { AxiosError, type AxiosInstance, type AxiosRequestConfig } from 'axios'

import type { ChainSummary, TelemetryReading, WarningRecord } from './types'
import { describeValidationDetail, type FieldLabels } from '@/utils/validationDetail'

/** 一张图只发查询参数，这一层的字段叫法与通用客户端一致。 */
const MAP_QUERY_LABELS: FieldLabels = {
  region_code: '区划代码',
  station_id: '站点号',
  metric: '指标',
  limit: '条数上限',
}

const http = axios.create({ baseURL: '/', timeout: 20_000 })

export class MapApiError extends Error {
  readonly status: number
  readonly detail: unknown

  constructor(status: number, message: string, detail?: unknown) {
    super(message)
    this.name = 'MapApiError'
    this.status = status
    this.detail = detail
  }
}

/** 资源不存在（后端尚未开放该路由）——上层据此走降级分支，不当作故障。 */
export function isMissingResource(error: unknown): boolean {
  return error instanceof MapApiError && error.status === 404
}

async function dispatch<T>(instance: AxiosInstance, config: AxiosRequestConfig): Promise<T> {
  try {
    const response = await instance.request<T>(config)
    return response.data
  } catch (error) {
    if (error instanceof AxiosError) {
      const status = error.response?.status ?? 0
      const detail = error.response?.data
      // 与通用客户端、上报、助手、编排四份同一口径：界面不替人保留英文原话。
      // 真机八张页扫下来只剩这一张还在说 "Request failed with status code 422"。
      const prefix = `接口调用失败（HTTP ${status}）`
      if (detail === undefined) throw new MapApiError(status, `${prefix}：${error.message}`, undefined)
      const reason = describeValidationDetail(detail, MAP_QUERY_LABELS)
      throw new MapApiError(status, reason === '' ? `${prefix}：${error.message}` : `${prefix}：${reason}`, detail)
    }
    throw error
  }
}

// ---------- 请求参数 ----------

export interface StationQuery {
  region_code?: string
  limit?: number
}

export interface TelemetryQuery {
  station_id?: string
  metric?: string
  region_code?: string
  limit?: number
}

export interface WarningQuery {
  limit?: number
  region_code?: string
}

// ---------- 响应体 ----------

export interface Counted<T> {
  count: number
  items: T[]
}

/**
 * `monitoring_stations` 行的期望形状（后端有表无路由，字段照 `geo.py:31` 抄，不增不减）。
 * 坐标三种可能来源全部允许缺失：标量列 lon/lat、PostGIS 文本 geom、或都没有。
 */
export interface StationDto {
  station_id: string
  name_zh?: string | null
  region_code?: string | null
  hazard_focus?: string[] | null
  elevation_m?: number | null
  /** `SRID=4326;POINT(lon lat)`（经度在前，OGC 口径），见 `geo.py:71`。 */
  geom?: string | null
  lon?: number | null
  lat?: number | null
}

/** 区划锚点：离线烘焙在 public/ 下的静态文件（见 public/basemaps/README.md）。 */
export interface RegionAnchorDto {
  code: string
  name_zh: string
  lon: number
  lat: number
}

export interface RegionAnchorFile {
  items?: unknown
}

export interface TelemetryResponse extends Counted<TelemetryReading> {}

export interface WarningsResponse extends Counted<WarningRecord> {}

export interface StationsResponse extends Counted<StationDto> {}

/** `GET /api/v1/events`：`{ items: ChainSummary[] }`（app.py:159-164）。 */
export interface EventsResponse {
  items: ChainSummary[]
}

/** GeoJSON 灾面文档：结构在 `entities.ts` 里做校验，此处刻意保持 unknown，避免把假设写进类型。 */
export type GeoJsonDocument = unknown

// ---------- 纯解析 ----------

/**
 * 锚点文件容错读取：数组或 `{items:[...]}` 两种烘焙形态都接受，坏条目逐条丢弃。
 * 这里只判"形状"，坐标是否**可用**（范围、(0,0) 缺省值）统一由 `entities.toLngLat` 裁决，
 * 判定口径只有一处。
 */
export function normalizeAnchors(raw: unknown): RegionAnchorDto[] {
  const list = Array.isArray(raw)
    ? raw
    : Array.isArray((raw as RegionAnchorFile | null)?.items)
      ? ((raw as RegionAnchorFile).items as unknown[])
      : []
  const anchors: RegionAnchorDto[] = []
  for (const entry of list) {
    const record = (entry ?? {}) as Record<string, unknown>
    const code = typeof record.code === 'string' ? record.code : ''
    const lon = typeof record.lon === 'number' ? record.lon : Number.NaN
    const lat = typeof record.lat === 'number' ? record.lat : Number.NaN
    if (!code || !Number.isFinite(lon) || !Number.isFinite(lat)) continue
    anchors.push({
      code,
      name_zh: typeof record.name_zh === 'string' ? record.name_zh : code,
      lon,
      lat,
    })
  }
  return anchors
}

// ---------- 客户端 ----------

/** 静态烘焙资产的路径常量：全部同源相对路径，杜绝外链（离线/弱网可用）。 */
export const MAP_ASSET_PATHS = {
  regionAnchors: '/basemaps/region-anchors.json',
  hazardZones: '/basemaps/hazard-zones.geojson',
  terrain: '/terrain',
  basemapTemplate: '/basemaps/{z}/{x}/{y}.png',
  pmtiles: '/basemaps/aegis.pmtiles',
} as const

/**
 * 地图客户端工厂：生产用模块级默认实例，测试注入桩适配器实例（见 components/map/api.spec.ts）。
 */
export function createMapApiClient(instance: AxiosInstance) {
  const request = <T>(config: AxiosRequestConfig) => dispatch<T>(instance, config)

  return {
    /** 遥测读数（后端有路由）：站点唯一可靠的现存来源。 */
    telemetry: (query: TelemetryQuery = {}) =>
      request<TelemetryResponse>({ url: '/api/v1/telemetry', params: query }),

    /** 预警记录（后端有路由）：区域 + 等级 + 通道触达。 */
    warnings: (query: WarningQuery = {}) =>
      request<WarningsResponse>({ url: '/api/v1/warnings', params: query }),

    /** 链路快照（后端有路由）：`risk.region_code/risk_level` 是区域风险的唯一来源。 */
    events: (limit = 100) =>
      request<EventsResponse>({ url: '/api/v1/events', params: { limit } }),

    /**
     * 站点台账（后端 `GET /api/v1/stations`）：维表登记的站带名称与经纬度，
     * 只报过数、尚未登记的站只有 station_id + region_code，坐标为 null。
     * 后者由上层归入「未定位」列表——这是真实数据缺口，不能拿区划中心点补一个坐标上去。
     */
    stations: (query: StationQuery = {}) =>
      request<StationsResponse>({ url: '/api/v1/stations', params: query }),

    /** 区划锚点：离线静态资产（public/basemaps/），未烘焙时 404 → 上层按「无锚点」渲染。 */
    regionAnchors: (url: string = MAP_ASSET_PATHS.regionAnchors) =>
      request<unknown>({ url }),

    /** 灾害分区面：离线静态 GeoJSON（或后端未来的 `/api/v1/gis/hazard-zones`）。 */
    hazardZones: (url: string = MAP_ASSET_PATHS.hazardZones) =>
      request<GeoJsonDocument>({ url }),
  }
}

export type MapApiClient = ReturnType<typeof createMapApiClient>

export const mapApi = createMapApiClient(http)

export default mapApi
