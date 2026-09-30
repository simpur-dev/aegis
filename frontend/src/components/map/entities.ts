/**
 * 「一张图」纯逻辑层：后端 DTO → 地图要素（plain data）。
 *
 * 架构定位（本文件的唯一职责）：把 api 契约里的字段**映射**成可直接渲染的数据，
 * 并回答"这个要素该不该上图、画成什么颜色、在不在视野内、和谁合并成一簇"。
 * 这里**不**碰 Cesium、不碰 Vue、不碰 axios（只有 `import type`），因此可以在 jsdom 下全量单测。
 * 渲染发生在 viewer.ts：它把本文件产出的 plain data 变成 entity/imagery。
 *
 * 关键取舍 —— 不臆造坐标：后端 telemetry/warnings 只有 `region_code`，站点经纬度在未补齐
 * `GET /api/v1/stations` 之前不可得（见 api/map.ts 顶部说明）。所以坐标一律 `LngLat | null`，
 * 为 null 的要素**进入右侧「未定位」清单**，绝不落到 (0,0)（Null Island）——
 * 一个位于几内亚湾的假站点比一个空位危险得多：它会被误读成"高原没灾情"。
 */

import type { RegionAnchorDto, StationDto } from '@/api/map'
import type {
  ChainSummary,
  DeliveryAttempt,
  HazardType,
  RiskLevel,
  TelemetryReading,
  WarningRecord,
} from '@/api/types'
import { HAZARD_LABELS, RISK_COLORS, RISK_LABELS } from '@/api/types'

// ---------- 坐标：判定口径只此一处 ----------

export interface LngLat {
  lon: number
  lat: number
}

export const MAX_LON = 180
export const MAX_LAT = 90

/**
 * 坐标合法性判定：非数值 / NaN / 越界 / (0,0) 一律返回 null。
 * (0,0) 之所以算非法：本项目的地理范围是 78°–99°E、26°–37°N（`persistence/geo.py:3-5` 同一口径），
 * 落在几内亚湾的坐标只可能是上游忘了填默认值，不可能是真实站点。
 */
export function toLngLat(lon: unknown, lat: unknown): LngLat | null {
  if (typeof lon !== 'number' || typeof lat !== 'number') return null
  if (!Number.isFinite(lon) || !Number.isFinite(lat)) return null
  if (Math.abs(lon) > MAX_LON || Math.abs(lat) > MAX_LAT) return null
  if (lon === 0 && lat === 0) return null
  return { lon, lat }
}

const POINT_WKT_RE = /POINT\s*\(\s*(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*\)/i

/** `POINT(lon lat)` / `SRID=4326;POINT(...)`（经度在前，OGC 口径）→ LngLat | null。 */
export function pointFromWkt(raw: unknown): LngLat | null {
  if (typeof raw !== 'string') return null
  const matched = POINT_WKT_RE.exec(raw)
  if (!matched) return null
  return toLngLat(Number(matched[1]), Number(matched[2]))
}

/**
 * 站点坐标取用优先级：标量列 lon/lat → PostGIS geom 文本 → 区划锚点 → null。
 * 前两级来自后端表 monitoring_stations（尚未开放路由），锚点是离线烘焙资产。
 */
export function stationLngLat(station: StationDto | undefined, anchor: RegionAnchorDto | undefined): LngLat | null {
  const fromColumns = station ? toLngLat(station.lon ?? null, station.lat ?? null) : null
  const fromGeom = station ? pointFromWkt(station.geom) : null
  const fromAnchor = anchor ? toLngLat(anchor.lon, anchor.lat) : null
  return fromColumns ?? fromGeom ?? fromAnchor
}

export function indexAnchors(anchors: readonly RegionAnchorDto[]): Record<string, RegionAnchorDto> {
  const index: Record<string, RegionAnchorDto> = {}
  for (const anchor of anchors) index[anchor.code] = anchor
  return index
}

// ---------- 风险 → 颜色 / 标签 ----------

/** 与 5 级"无风险"灰（#8c8c8c）刻意区分：等级未知不等于判过是无风险。 */
export const UNKNOWN_RISK_COLOR = '#bfbfbf'
export const UNKNOWN_RISK_LABEL = '等级未知'

/** 容错解析风险等级：后端可能是数字、数字字符串、越界值或缺失。 */
export function toRiskLevel(raw: unknown): RiskLevel | null {
  const numeric = typeof raw === 'string' ? Number(raw) : raw
  if (typeof numeric !== 'number' || !Number.isInteger(numeric)) return null
  if (numeric < 1 || numeric > 5) return null
  return numeric as RiskLevel
}

/** 1 最高（红）… 5 无风险；null（未知）走独立灰，避免与 5 混淆。 */
export function riskColorCss(level: RiskLevel | null): string {
  return level === null ? UNKNOWN_RISK_COLOR : RISK_COLORS[level]
}

export function riskLabel(level: RiskLevel | null): string {
  return level === null ? UNKNOWN_RISK_LABEL : RISK_LABELS[level]
}

/** 风险等级越小越严重：聚合（一簇/一区域多条证据）时取"最坏"。 */
export function worstRisk(levels: readonly (RiskLevel | null)[]): RiskLevel | null {
  let worst: RiskLevel | null = null
  for (const level of levels) {
    if (level === null) continue
    if (worst === null || level < worst) worst = level
  }
  return worst
}

const HAZARD_TYPE_VALUES = Object.keys(HAZARD_LABELS) as HazardType[]

/** 未知灾种归一到 `unknown`（标签"未定灾种"）：宁可显式承认未知，也不静默丢图层。 */
export function toHazardType(raw: unknown): HazardType {
  if (typeof raw !== 'string') return 'unknown'
  return (HAZARD_TYPE_VALUES as readonly string[]).includes(raw) ? (raw as HazardType) : 'unknown'
}

export function hazardLabel(raw: unknown): string {
  return HAZARD_LABELS[toHazardType(raw)]
}

// ---------- 视野（bbox）----------

export interface BBox {
  west: number
  south: number
  east: number
  north: number
}

/** 首屏/回退视角：西藏全域 78°–99°E、26°–37°N（与后端 `persistence/geo.py:3-5` 同一口径）。 */
export const TIBET_RECTANGLE: BBox = { west: 78, south: 26, east: 99, north: 37 }

/** 视野外扩比例：弱网下避免每挪动一点就重新取数/重画。 */
export const VIEWPORT_PADDING_RATIO = 0.15

export function isUsableBBox(box: unknown): box is BBox {
  if (typeof box !== 'object' || box === null) return false
  const { west, south, east, north } = box as BBox
  if (![west, south, east, north].every((v) => typeof v === 'number' && Number.isFinite(v))) return false
  if (Math.abs(west) > MAX_LON || Math.abs(east) > MAX_LON) return false
  if (Math.abs(south) > MAX_LAT || Math.abs(north) > MAX_LAT) return false
  return south <= north && west <= east
}

/** 边界**闭区间**：恰好在边线上的点算"在视野内"（否则缩放时点会闪进闪出）。 */
export function pointInBBox(point: LngLat, box: BBox): boolean {
  return point.lon >= box.west && point.lon <= box.east && point.lat >= box.south && point.lat <= box.north
}

export function bboxIntersects(a: BBox, b: BBox): boolean {
  return !(a.east < b.west || a.west > b.east || a.north < b.south || a.south > b.north)
}

export function expandBBox(box: BBox, ratio = VIEWPORT_PADDING_RATIO): BBox {
  const lonSpan = (box.east - box.west) * ratio
  const latSpan = (box.north - box.south) * ratio
  return {
    west: Math.max(-MAX_LON, box.west - lonSpan),
    east: Math.min(MAX_LON, box.east + lonSpan),
    south: Math.max(-MAX_LAT, box.south - latSpan),
    north: Math.min(MAX_LAT, box.north + latSpan),
  }
}

export function bboxOfPoints(points: readonly LngLat[]): BBox | null {
  if (points.length === 0) return null
  let west = points[0].lon
  let east = points[0].lon
  let south = points[0].lat
  let north = points[0].lat
  for (const point of points) {
    west = Math.min(west, point.lon)
    east = Math.max(east, point.lon)
    south = Math.min(south, point.lat)
    north = Math.max(north, point.lat)
  }
  return { west, south, east, north }
}

/** 相机高度（米）→ 缩放级：经验式，用于决定聚合粒度，不做精确墨卡托换算。 */
export function zoomForCameraHeight(heightM: number): number {
  if (!Number.isFinite(heightM) || heightM <= 0) return 0
  const zoom = Math.log2(Math.max(1, 637_100_000 / heightM))
  return Math.min(21, Math.max(0, Math.round(zoom)))
}

/** 缩放级 → 聚合网格边长（度）：低缩放（看全域）粗，高缩放（看沟谷）细到不聚合。 */
export function clusterCellDegrees(zoom: number): number {
  if (zoom >= 12) return 0
  if (zoom >= 10) return 0.05
  if (zoom >= 8) return 0.25
  if (zoom >= 6) return 1
  if (zoom >= 4) return 3
  return 8
}

// ---------- 要素模型（plain data）----------

export type FeatureKind = 'station' | 'warning' | 'reach' | 'hazard-zone'

export type LayerId = 'stations' | 'warnings' | 'reach' | 'hazards'

export interface BaseFeature {
  id: string
  kind: FeatureKind
  title: string
  regionCode: string
  riskLevel: RiskLevel | null
  colorCss: string
  hazardType: HazardType
}

export interface PointFeature extends BaseFeature {
  lonlat: LngLat | null
}

export interface TelemetrySample {
  metric: string
  value: number
  unit: string
  observedAt: string
  qualityFlag: string
}

export interface StationFeature extends PointFeature {
  kind: 'station'
  stationId: string
  nameZh: string
  elevationM: number | null
  /** 质量劣化（suspect/missing/drift）的读数不参与配色，只计入条数提示。 */
  suspectCount: number
  samples: TelemetrySample[]
}

export interface ReachSummary {
  attempts: number
  delivered: number
  failed: number
  pending: number
  audienceCount: number
  deliveredChannels: string[]
  /** 有通道失败或一条都没送达 → 触达劣化，需要图上标红提示。 */
  degraded: boolean
}

export interface WarningFeature extends PointFeature {
  kind: 'warning'
  warningId: string
  channels: string[]
  generatedAt: string
  releasedAt: string | null
  translationPending: boolean
  reach: ReachSummary
}

export interface ReachFeature extends PointFeature {
  kind: 'reach'
  warningId: string
  audienceCount: number
  degraded: boolean
}

export interface HazardZoneFeature extends BaseFeature {
  kind: 'hazard-zone'
  outer: LngLat[]
  holes: LngLat[][]
  bbox: BBox
}

export type UnlocatedReason = 'station-without-coord' | 'region-without-anchor' | 'no-anchor'

export interface UnlocatedItem {
  id: string
  kind: FeatureKind
  title: string
  regionCode: string
  reason: UnlocatedReason
}

export type GeometryRejectReason =
  | 'not-a-feature-collection'
  | 'missing-geometry'
  | 'unsupported-geometry'
  | 'malformed-coordinates'
  | 'ring-too-short'
  | 'coord-out-of-range'

export interface RejectedGeometry {
  index: number
  id: string
  reason: GeometryRejectReason
  detail: string
}

export interface RegionRisk {
  regionCode: string
  riskLevel: RiskLevel | null
  hazardType: HazardType
  confidence: number | null
  rationale: string
  source: 'chain' | 'warning' | 'none'
  at: string | null
}

/** 可独立显隐的图层级：三个点层 + 面层 + 聚合层（聚合是站点/预警的另一种画法，故单独可关）。 */
export type PlanLayer = LayerId | 'clusters'

export const PLAN_LAYERS: readonly PlanLayer[] = ['stations', 'warnings', 'reach', 'hazards', 'clusters']

export type LayerVisibility = Record<PlanLayer, boolean>

/** 触达层默认关：与预警层重叠，现场按需用。 */
export const DEFAULT_LAYER_VISIBILITY: LayerVisibility = {
  stations: true,
  warnings: true,
  reach: false,
  hazards: true,
  clusters: true,
}

export interface MapLayers {
  stations: Plotted<StationFeature>[]
  warnings: Plotted<WarningFeature>[]
  reach: Plotted<ReachFeature>[]
  hazards: HazardZoneFeature[]
  /**
   * 算出来但**没能上图**的完整要素（缺坐标）。清单必须能点开详情，
   * 所以这里保留完整要素而不是摘要——摘要由 `toUnlocatedItems` 现算。
   */
  unplaced: PointFeatureDetail[]
  rejected: RejectedGeometry[]
}

/** 三个点图层的要素联合（都有 lonlat，都可能"未定位"）。 */
export type PointFeatureDetail = StationFeature | WarningFeature | ReachFeature

export type FeatureDetail = PointFeatureDetail | HazardZoneFeature

export const EMPTY_LAYERS: MapLayers = {
  stations: [],
  warnings: [],
  reach: [],
  hazards: [],
  unplaced: [],
  rejected: [],
}

export function hasCoord<T extends PointFeature>(feature: T): feature is T & { lonlat: LngLat } {
  return feature.lonlat !== null
}

/** 有点的进图层，没点的进 unplaced：一次遍历同时产出两份，避免上层各写一遍过滤。 */
export function partitionPoints<T extends PointFeature>(
  features: readonly T[],
): { plotted: (T & { lonlat: LngLat })[]; unplaced: T[] } {
  const plotted: (T & { lonlat: LngLat })[] = []
  const unplaced: T[] = []
  for (const feature of features) {
    if (hasCoord(feature)) plotted.push(feature)
    else unplaced.push(feature)
  }
  return { plotted, unplaced }
}

/** 未定位清单条目：为什么没上图必须写在条目里，不能让用户自己猜。 */
export function toUnlocatedItems(features: readonly PointFeatureDetail[]): UnlocatedItem[] {
  return features.map((feature) => ({
    id: feature.id,
    kind: feature.kind,
    title: feature.title,
    regionCode: feature.regionCode,
    reason: unlocatedReasonOf(feature),
  }))
}

export const UNLOCATED_LABELS: Record<UnlocatedReason, string> = {
  'station-without-coord': '站点无经纬度（后端尚未开放 /api/v1/stations）',
  'region-without-anchor': '该区域无锚点（未烘焙 region-anchors.json）',
  'no-anchor': '后端记录里没有区域编码',
}

function unlocatedReasonOf(feature: PointFeature): UnlocatedReason {
  if (feature.kind === 'station') return feature.regionCode ? 'station-without-coord' : 'no-anchor'
  return feature.regionCode ? 'region-without-anchor' : 'no-anchor'
}

// ---------- 区域风险聚合 ----------

function isoAt(value: string | null | undefined): number {
  if (!value) return 0
  const parsed = Date.parse(value)
  return Number.isNaN(parsed) ? 0 : parsed
}

/**
 * 区域风险来源优先级：链路评估（`ChainSummary.risk`，规则引擎的直接产物）> 预警记录（发布态）。
 *
 * 顺序口径：`GET /api/v1/events` 走 `storage/store.py:45-49` 的 `latest()`，返回**追加序（旧→新）**，
 * 所以同一区域遍历到最后一条才是最新评估——这里刻意不排序，只按"后到覆盖先到"。
 */
export function riskByRegion(
  chains: readonly ChainSummary[] = [],
  warnings: readonly WarningRecord[] = [],
): Record<string, RegionRisk> {
  const index: Record<string, RegionRisk> = {}

  for (const chain of chains) {
    const risk = chain.risk
    if (!risk || !risk.region_code) continue
    index[risk.region_code] = {
      regionCode: risk.region_code,
      riskLevel: toRiskLevel(risk.risk_level),
      hazardType: toHazardType(risk.hazard_type),
      confidence: typeof risk.confidence === 'number' ? risk.confidence : null,
      rationale: risk.rationale ?? '',
      source: 'chain',
      at: chain.event_id,
    }
  }

  const warningSeenAt: Record<string, number> = {}
  for (const warning of warnings) {
    const at = isoAt(warning.generated_at)
    for (const code of warning.region_codes ?? []) {
      if (index[code]?.source === 'chain') continue
      if (code in warningSeenAt && warningSeenAt[code] >= at) continue
      warningSeenAt[code] = at
      index[code] = {
        regionCode: code,
        riskLevel: toRiskLevel(warning.risk_level),
        hazardType: toHazardType(warning.hazard_type),
        confidence: null,
        rationale: warning.title_zh,
        source: 'warning',
        at: warning.generated_at,
      }
    }
  }
  return index
}

// ---------- 图层 1：监测站点 ----------

/** 同一站点多条读数：按 metric 只留最新一条（图上一个点无法表达时序）。 */
export function latestSamples(readings: readonly TelemetryReading[]): { samples: TelemetrySample[]; suspectCount: number } {
  const newest: Record<string, TelemetryReading> = {}
  for (const reading of readings) {
    const holder = newest[reading.metric]
    if (!holder || isoAt(reading.observed_at) >= isoAt(holder.observed_at)) newest[reading.metric] = reading
  }
  let suspectCount = 0
  const samples: TelemetrySample[] = []
  for (const metric of Object.keys(newest).sort()) {
    const reading = newest[metric]
    if (reading.quality_flag !== 'ok') suspectCount += 1
    samples.push({
      metric: reading.metric,
      value: reading.value,
      unit: reading.unit,
      observedAt: reading.observed_at,
      qualityFlag: reading.quality_flag,
    })
  }
  return { samples, suspectCount }
}

export interface StationLayerInput {
  readings: readonly TelemetryReading[]
  stations: readonly StationDto[]
  anchors: Record<string, RegionAnchorDto>
  risks: Record<string, RegionRisk>
}

export interface PointLayerResult<T extends PointFeature> {
  plotted: (T & { lonlat: LngLat })[]
  /** 全部要素（含未定位）：详情索引要用它，不能只拿上图的那部分。 */
  all: T[]
  unplaced: T[]
}

/**
 * 站点图层：要素集合 = 遥测出现过的站点 ∪ 台账里的站点。
 * 台账为空（后端 404，当前实况）时仍能出图层，只是坐标全部依赖锚点，落不到锚点的进未定位清单。
 */
export function buildStationLayer(input: StationLayerInput): PointLayerResult<StationFeature> {
  const readingsByStation: Record<string, TelemetryReading[]> = {}
  const regionOfStation: Record<string, string> = {}
  for (const reading of input.readings) {
    const bucket = readingsByStation[reading.station_id]
    if (bucket) bucket.push(reading)
    else readingsByStation[reading.station_id] = [reading]
    if (!regionOfStation[reading.station_id]) regionOfStation[reading.station_id] = reading.region_code
  }

  const ledgerById: Record<string, StationDto> = {}
  for (const station of input.stations) ledgerById[station.station_id] = station

  const stationIds = [...new Set([...Object.keys(readingsByStation), ...Object.keys(ledgerById)])].sort()
  const features: StationFeature[] = []
  for (const stationId of stationIds) {
    const dto = ledgerById[stationId]
    const regionCode = dto?.region_code || regionOfStation[stationId] || ''
    const risk = regionCode ? input.risks[regionCode] : undefined
    const hazardType = risk && risk.hazardType !== 'unknown' ? risk.hazardType : toHazardType(dto?.hazard_focus?.[0])
    const { samples, suspectCount } = latestSamples(readingsByStation[stationId] ?? [])
    features.push({
      id: `station:${stationId}`,
      kind: 'station',
      stationId,
      nameZh: dto?.name_zh || stationId,
      title: dto?.name_zh || stationId,
      regionCode,
      elevationM: dto?.elevation_m ?? null,
      hazardType,
      riskLevel: risk?.riskLevel ?? null,
      colorCss: riskColorCss(risk?.riskLevel ?? null),
      suspectCount,
      samples,
      lonlat: stationLngLat(dto, regionCode ? input.anchors[regionCode] : undefined),
    })
  }
  return { ...partitionPoints(features), all: features }
}

// ---------- 图层 2：预警 ----------

/** 通道触达汇总：delivered/retried 算成功送达，failed 算失败，pending 算未回执。 */
export function summarizeReach(deliveries: readonly DeliveryAttempt[]): ReachSummary {
  let delivered = 0
  let failed = 0
  let pending = 0
  let audienceCount = 0
  const deliveredChannels: string[] = []
  for (const attempt of deliveries) {
    if (attempt.status === 'delivered' || attempt.status === 'retried') {
      delivered += 1
      deliveredChannels.push(attempt.channel)
      audienceCount += attempt.audience_count
    } else if (attempt.status === 'failed') {
      failed += 1
    } else {
      pending += 1
    }
  }
  const attempts = deliveries.length
  return {
    attempts,
    delivered,
    failed,
    pending,
    audienceCount,
    deliveredChannels,
    degraded: attempts > 0 && (failed > 0 || delivered === 0),
  }
}

export interface WarningLayerInput {
  warnings: readonly WarningRecord[]
  anchors: Record<string, RegionAnchorDto>
}

/**
 * 预警图层：一条预警覆盖 N 个 region_code 就产出 N 个 marker（id 带区域后缀）。
 * 后端 WarningRecord 无经纬度，故坐标只能来自锚点；无锚点的区域全部落进未定位清单。
 */
export function buildWarningLayer(input: WarningLayerInput): PointLayerResult<WarningFeature> {
  const features: WarningFeature[] = []
  for (const warning of input.warnings) {
    const reach = summarizeReach(warning.deliveries ?? [])
    const codes = warning.region_codes?.length ? warning.region_codes : ['']
    for (const code of codes) {
      features.push({
        id: `warning:${warning.warning_id}@${code}`,
        kind: 'warning',
        warningId: warning.warning_id,
        title: warning.title_zh,
        regionCode: code,
        hazardType: toHazardType(warning.hazard_type),
        riskLevel: toRiskLevel(warning.risk_level),
        colorCss: riskColorCss(toRiskLevel(warning.risk_level)),
        channels: [...(warning.channels ?? [])],
        generatedAt: warning.generated_at,
        releasedAt: warning.released_at ?? null,
        translationPending: Boolean(warning.translation_pending),
        reach,
        lonlat: toLngLatOrNull(input.anchors[code]),
      })
    }
  }
  return { ...partitionPoints(features), all: features }
}

function toLngLatOrNull(anchor: RegionAnchorDto | undefined): LngLat | null {
  return anchor ? toLngLat(anchor.lon, anchor.lat) : null
}

/**
 * 触达图层：只画"有投递尝试"的预警，图标语义是"触达是否成立"而非"风险多大"，
 * 因此配色固定：劣化（有失败/零送达）用红，其余用蓝。
 */
export const REACH_DEGRADED_COLOR = '#cf1322'
export const REACH_OK_COLOR = '#1677ff'

export function buildReachLayer(input: WarningLayerInput): PointLayerResult<ReachFeature> {
  const features: ReachFeature[] = []
  for (const warning of input.warnings) {
    const reach = summarizeReach(warning.deliveries ?? [])
    if (reach.attempts === 0) continue
    for (const code of warning.region_codes?.length ? warning.region_codes : ['']) {
      features.push({
        id: `reach:${warning.warning_id}@${code}`,
        kind: 'reach',
        warningId: warning.warning_id,
        title: `${warning.title_zh}｜触达 ${reach.delivered}/${reach.attempts}`,
        regionCode: code,
        hazardType: toHazardType(warning.hazard_type),
        riskLevel: null,
        colorCss: reach.degraded ? REACH_DEGRADED_COLOR : REACH_OK_COLOR,
        audienceCount: reach.audienceCount,
        degraded: reach.degraded,
        lonlat: toLngLatOrNull(input.anchors[code]),
      })
    }
  }
  return { ...partitionPoints(features), all: features }
}

// ---------- 图层 3：灾害分区面 ----------

function ringOf(raw: unknown): { ring: LngLat[] | null; reason: GeometryRejectReason | null } {
  if (!Array.isArray(raw) || raw.length < 4) return { ring: null, reason: 'ring-too-short' }
  const points: LngLat[] = []
  for (const position of raw) {
    if (!Array.isArray(position)) return { ring: null, reason: 'malformed-coordinates' }
    const point = toLngLat(position[0], position[1])
    if (!point) return { ring: null, reason: 'coord-out-of-range' }
    points.push(point)
  }
  // GeoJSON 规定首尾重复闭合：去掉重复的末点，避免渲染端多一个退化顶点。
  const first = points[0]
  const last = points[points.length - 1]
  const closed = first.lon === last.lon && first.lat === last.lat
  const ring = closed ? points.slice(0, -1) : points
  if (ring.length < 3) return { ring: null, reason: 'ring-too-short' }
  return { ring, reason: null }
}

interface RawProperties {
  [key: string]: unknown
}

function propertiesOf(feature: Record<string, unknown>): RawProperties {
  const raw = feature.properties
  return typeof raw === 'object' && raw !== null ? (raw as RawProperties) : {}
}

function zoneOf(
  rings: { outer: LngLat[]; holes: LngLat[][] },
  props: RawProperties,
  risks: Record<string, RegionRisk>,
  id: string,
): HazardZoneFeature {
  const regionCode = typeof props.region_code === 'string' ? props.region_code : ''
  const risk = regionCode ? risks[regionCode] : undefined
  const riskLevel = toRiskLevel(props.risk_level) ?? risk?.riskLevel ?? null
  const outer = rings.outer
  const bbox = bboxOfPoints(outer) ?? { west: 0, south: 0, east: 0, north: 0 }
  const name = typeof props.name === 'string' ? props.name : typeof props.title === 'string' ? props.title : id
  return {
    // id 由调用方给全（MultiPolygon 要带子面后缀）：一个面一个 entity，点击才不会有歧义。
    id,
    kind: 'hazard-zone',
    title: name,
    regionCode,
    hazardType: risk && risk.hazardType !== 'unknown' ? risk.hazardType : toHazardType(props.hazard_type),
    riskLevel,
    colorCss: riskColorCss(riskLevel),
    outer,
    holes: rings.holes,
    bbox,
  }
}

function isPolygonGeometry(geometry: Record<string, unknown>): boolean {
  return geometry.type === 'Polygon' || geometry.type === 'MultiPolygon'
}

/**
 * 灾害面图层：解析并**校验** GeoJSON（Polygon / MultiPolygon 之外的几何一律记为 rejected）。
 * 校验失败不抛错、不静默丢：产出 `rejected[]` 让视图显式告诉用户"有几条几何没画出来、为什么"，
 * 这是离线出图场景最基本的自证——图上少画一块，必须看得见原因。
 */
export function buildHazardZoneLayer(
  document: unknown,
  options: { risks?: Record<string, RegionRisk> } = {},
): { polygons: HazardZoneFeature[]; rejected: RejectedGeometry[] } {
  const risks = options.risks ?? {}
  const polygons: HazardZoneFeature[] = []
  const rejected: RejectedGeometry[] = []

  const source = document as { type?: unknown; features?: unknown; geometry?: unknown; properties?: unknown } | null
  if (!source || typeof source !== 'object') {
    return { polygons, rejected: [{ index: -1, id: '', reason: 'not-a-feature-collection', detail: '文档为空或非对象' }] }
  }

  const rawFeatures: unknown[] = Array.isArray(source.features)
    ? source.features
    : source.type === 'Feature' || isGeometryLike(source)
      ? [{ type: 'Feature', geometry: source.geometry ?? source, properties: source.properties ?? {} }]
      : []
  if (!Array.isArray(source.features) && rawFeatures.length === 0) {
    return {
      polygons,
      rejected: [{ index: -1, id: '', reason: 'not-a-feature-collection', detail: `type=${String(source.type)}` }],
    }
  }

  rawFeatures.forEach((entry, index) => {
    const feature = (entry ?? {}) as Record<string, unknown>
    const props = propertiesOf(feature)
    const id = typeof props.id === 'string' ? props.id : `feature#${index}`
    const geometry = feature.geometry
    if (typeof geometry !== 'object' || geometry === null) {
      rejected.push({ index, id, reason: 'missing-geometry', detail: 'feature.geometry 缺失' })
      return
    }
    const geometryRecord = geometry as { type?: unknown; coordinates?: unknown }
    if (!isPolygonGeometry(geometryRecord)) {
      rejected.push({ index, id, reason: 'unsupported-geometry', detail: `type=${String(geometryRecord.type)}` })
      return
    }
    if (geometryRecord.type === 'Polygon') {
      const parsed = parsePolygon(geometryRecord.coordinates, id)
      if (parsed.reason) {
        rejected.push({ index, id, reason: parsed.reason, detail: parsed.detail })
        return
      }
      polygons.push(zoneOf({ outer: parsed.outer, holes: parsed.holes }, props, risks, id))
      return
    }
    // MultiPolygon：每个子面单独成一个要素，id 带 #序号后缀（否则两块面共用一个 id，点选会串）
    const parts = Array.isArray(geometryRecord.coordinates) ? geometryRecord.coordinates : []
    let pushed = 0
    for (const [partIndex, part] of parts.entries()) {
      const parsed = parsePolygon(part, `${id}#${partIndex}`)
      if (parsed.reason) {
        rejected.push({ index, id: `${id}#${partIndex}`, reason: parsed.reason, detail: parsed.detail })
        continue
      }
      polygons.push(zoneOf({ outer: parsed.outer, holes: parsed.holes }, props, risks, `${id}#${partIndex}`))
      pushed += 1
    }
    if (pushed === 0 && parts.length === 0) {
      rejected.push({ index, id, reason: 'malformed-coordinates', detail: 'MultiPolygon 坐标为空' })
    }
  })

  return { polygons, rejected }
}

function isGeometryLike(source: { type?: unknown }): boolean {
  return source.type === 'Polygon' || source.type === 'MultiPolygon'
}

function parsePolygon(
  coordinates: unknown,
  id: string,
): { outer: LngLat[]; holes: LngLat[][]; reason: GeometryRejectReason | null; detail: string } {
  if (!Array.isArray(coordinates) || coordinates.length === 0) {
    return { outer: [], holes: [], reason: 'malformed-coordinates', detail: `${id}: coordinates 非数组或为空` }
  }
  const rings: LngLat[][] = []
  for (const ringRaw of coordinates) {
    const parsed = ringOf(ringRaw)
    if (!parsed.ring) {
      return { outer: [], holes: [], reason: parsed.reason ?? 'malformed-coordinates', detail: `${id}: 环不合法` }
    }
    rings.push(parsed.ring)
  }
  return { outer: rings[0], holes: rings.slice(1), reason: null, detail: '' }
}

// ---------- 视野过滤 ----------

export interface ViewportFilterInput {
  bbox: BBox | null
  layers: MapLayers
}

/**
 * 视野过滤：bbox 为 null（相机尚未就绪 / 2D 全景）表示不过滤。
 * 面用 bbox 相交（粗筛足够，精确的"点是否在面内"交给后端 geo.py 的 geography 口径），
 * 「未定位」清单不受视野影响——否则用户转开视角就以为数据没了。
 */
export function filterLayersByViewport(input: ViewportFilterInput): MapLayers {
  const { bbox, layers } = input
  if (!bbox || !isUsableBBox(bbox)) return layers
  const inPoint = (point: LngLat | null): boolean => point !== null && pointInBBox(point, bbox)
  return {
    stations: layers.stations.filter((feature) => inPoint(feature.lonlat)),
    warnings: layers.warnings.filter((feature) => inPoint(feature.lonlat)),
    reach: layers.reach.filter((feature) => inPoint(feature.lonlat)),
    hazards: layers.hazards.filter((feature) => bboxIntersects(feature.bbox, bbox)),
    unplaced: layers.unplaced,
    rejected: layers.rejected,
  }
}

// ---------- 聚合 ----------

export interface PointCluster {
  id: string
  lonlat: LngLat
  count: number
  colorCss: string
  worstRisk: RiskLevel | null
  memberIds: string[]
}

/** 少于该数量就不聚合（点少时聚合反而遮挡信息）。 */
export const CLUSTER_MIN_POINTS = 24

/** 粗到一定程度 + 点多到一定程度才聚合：两个条件缺一不可，便于按缩放分档。 */
export function shouldCluster(pointCount: number, zoom: number): boolean {
  return clusterCellDegrees(zoom) > 0 && pointCount >= CLUSTER_MIN_POINTS
}

/** 网格键：负经度同样用 floor（-0.1/1 → -1，与 0.1/1 → 0 分属相邻格，边界点不会重复计数）。 */
export function clusterCellKey(point: LngLat, cellDeg: number): string {
  return `${Math.floor(point.lon / cellDeg)}:${Math.floor(point.lat / cellDeg)}`
}

/**
 * 等经纬网格聚合：质心取成员均值，颜色取成员里最坏的风险等级。
 * 输出按格键排序，保证同一批数据每次都得到同样的簇序列（快照/回归测试可断言）。
 */
export function clusterPoints<T extends PointFeature>(
  points: readonly T[],
  cellDeg: number,
): PointCluster[] {
  if (cellDeg <= 0) return []
  const buckets: Record<string, (T & { lonlat: LngLat })[]> = {}
  const plotted = points.filter(hasCoord)
  for (const point of plotted) {
    const key = clusterCellKey(point.lonlat, cellDeg)
    const bucket = buckets[key]
    if (bucket) bucket.push(point)
    else buckets[key] = [point]
  }
  return Object.keys(buckets)
    .sort()
    .map((key) => {
      const members = buckets[key]
      const centroid = bboxOfPoints(members.map((member) => member.lonlat))
      const worst = worstRisk(members.map((member) => member.riskLevel))
      return {
        id: `cluster:${key}`,
        lonlat: centroid ? { lon: (centroid.west + centroid.east) / 2, lat: (centroid.south + centroid.north) / 2 } : members[0].lonlat,
        count: members.length,
        colorCss: riskColorCss(worst),
        worstRisk: worst,
        memberIds: members.map((member) => member.id).sort(),
      }
    })
}

// ---------- 组装 / 索引 ----------

export interface BuildLayersInput {
  readings: readonly TelemetryReading[]
  stations: readonly StationDto[]
  warnings: readonly WarningRecord[]
  chains: readonly ChainSummary[]
  anchors: readonly RegionAnchorDto[]
  hazardZones: unknown
}

/** 单一入口：视图只调这一个函数拿到全部图层数据，装配顺序集中在此，避免各页各拼一套。 */
export function buildLayers(input: BuildLayersInput): MapLayers {
  const anchors = indexAnchors(input.anchors)
  const risks = riskByRegion(input.chains, input.warnings)
  const stationLayer = buildStationLayer({ readings: input.readings, stations: input.stations, anchors, risks })
  const warningLayer = buildWarningLayer({ warnings: input.warnings, anchors })
  const reachLayer = buildReachLayer({ warnings: input.warnings, anchors })
  const zones = buildHazardZoneLayer(input.hazardZones, { risks })
  return {
    stations: stationLayer.plotted,
    warnings: warningLayer.plotted,
    reach: reachLayer.plotted,
    hazards: zones.polygons,
    unplaced: [...stationLayer.unplaced, ...warningLayer.unplaced, ...reachLayer.unplaced],
    rejected: zones.rejected,
  }
}

export function layerCounts(layers: MapLayers): Record<LayerId, number> {
  return {
    stations: layers.stations.length,
    warnings: layers.warnings.length,
    reach: layers.reach.length,
    hazards: layers.hazards.length,
  }
}

/** 点击→详情：要素 id 全局唯一（`station:` / `warning:` / `reach:` / 面 id），建一次索引给面板查。 */
export function featureIndex(layers: MapLayers): Record<string, FeatureDetail> {
  const index: Record<string, FeatureDetail> = {}
  for (const feature of layers.stations) index[feature.id] = feature
  for (const feature of layers.warnings) index[feature.id] = feature
  for (const feature of layers.reach) index[feature.id] = feature
  for (const feature of layers.hazards) index[feature.id] = feature
  // 未上图的要素同样可点选（清单里有"详情"），所以索引必须覆盖它们。
  for (const feature of layers.unplaced) index[feature.id] = feature
  return index
}

export function detailOf(layers: MapLayers, id: string | null): FeatureDetail | null {
  if (!id) return null
  return featureIndex(layers)[id] ?? null
}

/** 图例：等级 → 颜色 → 中文，顺序即严重度顺序（1 最重），供面板直接渲染。 */
export interface LegendEntry {
  level: RiskLevel
  label: string
  colorCss: string
}

export function riskLegend(): LegendEntry[] {
  return ([1, 2, 3, 4, 5] as RiskLevel[]).map((level) => ({
    level,
    label: RISK_LABELS[level],
    colorCss: RISK_COLORS[level],
  }))
}

// ---------- 渲染计划（视图与 viewer 之间的唯一契约）----------

/** 已定位要素：坐标在类型层面收紧为非空，渲染端因此不需要再判空。 */
export type Plotted<T extends PointFeature> = T & { lonlat: LngLat }

export interface RenderPlan {
  stations: Plotted<StationFeature>[]
  warnings: Plotted<WarningFeature>[]
  reach: Plotted<ReachFeature>[]
  hazards: HazardZoneFeature[]
  clusters: PointCluster[]
  /** 本次计划里是否走了聚合（面板据此解释"为什么点变成了数字"）。 */
  clustered: boolean
}

export interface RenderPlanInput {
  layers: MapLayers
  /** null 表示相机未就绪或全球视野：不过滤。 */
  bbox: BBox | null
  /** 由 viewer 回吐的相机高度换算出的缩放级（`zoomForCameraHeight`）。 */
  zoom: number
}

/**
 * 视野过滤 + 聚合分档，产出渲染计划。
 * 聚合只作用于站点与预警两类风险点（触达 marker 语义独立、数量也少，聚了反而读不出是哪条通道）。
 */
export function buildRenderPlan(input: RenderPlanInput): RenderPlan {
  const visible = filterLayersByViewport({ bbox: input.bbox, layers: input.layers })
  const points: (StationFeature | WarningFeature)[] = [...visible.stations, ...visible.warnings]
  const cellDeg = clusterCellDegrees(input.zoom)
  const clustered = shouldCluster(points.length, input.zoom)
  if (!clustered) {
    return {
      stations: visible.stations,
      warnings: visible.warnings,
      reach: visible.reach,
      hazards: visible.hazards,
      clusters: [],
      clustered: false,
    }
  }
  return {
    stations: [],
    warnings: [],
    reach: visible.reach,
    hazards: visible.hazards,
    clusters: clusterPoints(points, cellDeg),
    clustered: true,
  }
}
