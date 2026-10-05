/**
 * entities.ts 单测：坐标判定、风险配色、三个图层的映射、视野过滤、聚合与详情索引、定位提示。
 *
 * 约定：**不 import cesium、不 import vue、不触网**（jsdom 无 WebGL）。
 * 这里断言的是"映射结果"，不是渲染结果——渲染在 viewer.ts，被这条边界挡在测试之外。
 */

import { describe, expect, it } from 'vitest'

import type { RegionAnchorDto, StationDto } from '@/api/map'
import type { ChainSummary, DeliveryAttempt, RiskLevel, TelemetryReading, WarningRecord } from '@/api/types'
import {
  buildHazardZoneLayer,
  buildLayers,
  buildReachLayer,
  buildRenderPlan,
  buildStationLayer,
  buildWarningLayer,
  clusterCellDegrees,
  clusterCellKey,
  clusterPoints,
  detailOf,
  expandBBox,
  focusAllNote,
  groupUnlocatedByReason,
  hazardLabel,
  indexAnchors,
  isUsableBBox,
  layerCounts,
  latestSamples,
  pointFromWkt,
  pointInBBox,
  plottedCount,
  riskByRegion,
  riskColorCss,
  riskLabel,
  riskLegend,
  riskLevelCounts,
  shouldCluster,
  stationLngLat,
  summarizeReach,
  toHazardType,
  toLngLat,
  toRiskLevel,
  toUnlocatedItems,
  UNKNOWN_RISK_COLOR,
  worstRisk,
  zoomForCameraHeight,
  DEFAULT_LAYER_VISIBILITY,
  filterLayersByRisk,
  filterLayersByViewport,
  EMPTY_LAYERS,
} from './entities'
import { readRepoFile } from '@/testing/repoSource'

// ---------- 夹具：字段严格照后端 DTO，缺失就是缺失，不补不存在的列 ----------

function reading(
  stationId: string,
  regionCode: string,
  metric = 'rain_10min',
  value = 12,
  overrides: Partial<TelemetryReading> = {},
): TelemetryReading {
  return {
    station_id: stationId,
    metric,
    value,
    unit: 'mm',
    region_code: regionCode,
    observed_at: '2026-09-30T03:00:00.000Z',
    ingested_at: '2026-09-30T03:00:05.000Z',
    source: 'simulator',
    quality_flag: 'ok',
    ...overrides,
  }
}

function delivery(channel: string, status: DeliveryAttempt['status'], audienceCount = 10): DeliveryAttempt {
  return { channel, audience_count: audienceCount, status, attempted_at: '2026-09-30T03:05:00.000Z' }
}

function warning(overrides: Partial<WarningRecord> & { warning_id: string }): WarningRecord {
  return {
    event_id: 'evt_000000000001',
    trace_id: 'trc_0000000000000001',
    hazard_type: 'debris_flow',
    region_codes: ['540121'],
    risk_level: 2,
    title_zh: '林周县泥石流橙色预警',
    body_zh: '请撤离',
    body_bo: null,
    audiences: ['village'],
    channels: ['app'],
    translation_pending: false,
    generated_at: '2026-09-30T03:04:00.000Z',
    released_at: '2026-09-30T03:06:00.000Z',
    deliveries: [],
    ...overrides,
  }
}

function chain(regionCode: string | null, riskLevel: number | null = 1, eventId = 'evt_a'): ChainSummary {
  return {
    trace_id: 'trc_1',
    event_id: eventId,
    ok: true,
    acted: true,
    stages: [],
    risk:
      regionCode === null || riskLevel === null
        ? null
        : {
            hazard_type: 'landslide',
            region_code: regionCode,
            risk_level: riskLevel as 1,
            confidence: 0.8,
            rationale: '雨量超阈值',
          },
    task_units: [],
    errors: [],
    degradations: [],
  }
}

function station(overrides: Partial<StationDto> & { station_id: string }): StationDto {
  return { region_code: '540121', name_zh: '林周泥沟监测站', ...overrides }
}

const ANCHORS: RegionAnchorDto[] = [
  { code: '540121', name_zh: '林周县', lon: 91.287, lat: 30.041 },
  { code: '540221', name_zh: '南木林县', lon: 89.1, lat: 29.63 },
]

// ---------- 坐标 ----------

describe('坐标判定：缺失就是缺失，绝不落到 (0,0)', () => {
  it('正常经纬度通过', () => {
    expect(toLngLat(91.287, 30.041)).toEqual({ lon: 91.287, lat: 30.041 })
  })

  it('拒绝 (0,0)（Null Island）、NaN、越界、非数值', () => {
    expect(toLngLat(0, 0)).toBeNull()
    expect(toLngLat(Number.NaN, 30)).toBeNull()
    expect(toLngLat(91, Number.POSITIVE_INFINITY)).toBeNull()
    expect(toLngLat(181, 30)).toBeNull()
    expect(toLngLat(91, 91)).toBeNull()
    expect(toLngLat('91', '30')).toBeNull()
    expect(toLngLat(null, null)).toBeNull()
  })

  it('WKT 文本按"经度在前"解析，坏文本返回 null', () => {
    expect(pointFromWkt('SRID=4326;POINT(91.287 30.041)')).toEqual({ lon: 91.287, lat: 30.041 })
    expect(pointFromWkt('POINT (89.1 29.63)')).toEqual({ lon: 89.1, lat: 29.63 })
    expect(pointFromWkt('POLYGON((0 0,1 1,2 0,0 0))')).toBeNull()
    expect(pointFromWkt(null)).toBeNull()
    expect(pointFromWkt('POINT(0 0)')).toBeNull()
  })

  it('站点坐标优先级：标量列 > geom > 区划锚点 > null', () => {
    const anchor = ANCHORS[0]
    expect(stationLngLat(station({ station_id: 'S1', lon: 90, lat: 30, geom: 'POINT(91 31)' }), anchor)).toEqual({ lon: 90, lat: 30 })
    expect(stationLngLat(station({ station_id: 'S1', geom: 'POINT(91 31)' }), anchor)).toEqual({ lon: 91, lat: 31 })
    expect(stationLngLat(station({ station_id: 'S1' }), anchor)).toEqual({ lon: 91.287, lat: 30.041 })
    expect(stationLngLat(station({ station_id: 'S1' }), undefined)).toBeNull()
  })
})

// ---------- 风险 / 灾种 ----------

describe('风险等级 → 颜色：边界与未知', () => {
  it('1..5 全部有效，0/6/小数/字符串/空 判为未知', () => {
    for (const level of [1, 2, 3, 4, 5]) expect(toRiskLevel(level)).toBe(level)
    expect(toRiskLevel('2')).toBe(2)
    expect(toRiskLevel(0)).toBeNull()
    expect(toRiskLevel(6)).toBeNull()
    expect(toRiskLevel(2.5)).toBeNull()
    expect(toRiskLevel('红色')).toBeNull()
    expect(toRiskLevel(undefined)).toBeNull()
  })

  it('未知用独立灰，不与 5 级"无风险"混色', () => {
    expect(riskColorCss(null)).toBe(UNKNOWN_RISK_COLOR)
    expect(riskColorCss(5)).not.toBe(UNKNOWN_RISK_COLOR)
    expect(riskColorCss(1)).toBe('#cf1322')
    expect(riskLabel(null)).toBe('等级未知')
    expect(riskLabel(1)).toBe('红色')
  })

  it('聚合取最坏等级（数字越小越严重），全未知仍为未知', () => {
    expect(worstRisk([null, 4, 2, 3])).toBe(2)
    expect(worstRisk([5])).toBe(5)
    expect(worstRisk([null, null])).toBeNull()
    expect(worstRisk([])).toBeNull()
  })

  it('未知灾种归一到 unknown/未定灾种，不误标成别的灾种', () => {
    expect(toHazardType('debris_flow')).toBe('debris_flow')
    expect(toHazardType('snow_storm')).toBe('unknown')
    expect(toHazardType(undefined)).toBe('unknown')
    expect(hazardLabel('snow_storm')).toBe('未定灾种')
    expect(hazardLabel('avalanche')).toBe('冰雪雪崩')
  })

  it('图例覆盖 5 个等级且颜色互不相同', () => {
    const legend = riskLegend()
    expect(legend).toHaveLength(5)
    expect(new Set(legend.map((entry) => entry.colorCss)).size).toBe(5)
  })
})

// ---------- 图层 1：站点 ----------

describe('站点图层', () => {
  it('只有遥测（后端现状）：一条不进图、全部进未定位', () => {
    const layer = buildStationLayer({
      readings: [reading('RG-540121-01', '540121'), reading('RG-540221-01', '540221')],
      stations: [],
      anchors: {},
      risks: {},
    })
    expect(layer.plotted).toHaveLength(0)
    expect(layer.unplaced.map((feature) => feature.stationId)).toEqual(['RG-540121-01', 'RG-540221-01'])
    expect(toUnlocatedItems(layer.unplaced)[0]).toMatchObject({
      kind: 'station',
      reason: 'station-without-coord',
      regionCode: '540121',
    })
  })

  it('有锚点即可落点；风险/颜色取自区域评估', () => {
    const layer = buildStationLayer({
      readings: [reading('RG-540121-01', '540121')],
      stations: [],
      anchors: indexAnchors(ANCHORS),
      risks: riskByRegion([chain('540121', 1)], []),
    })
    expect(layer.plotted).toHaveLength(1)
    const feature = layer.plotted[0]
    expect(feature.lonlat).toEqual({ lon: 91.287, lat: 30.041 })
    expect(feature.riskLevel).toBe(1)
    expect(feature.colorCss).toBe('#cf1322')
    expect(feature.hazardType).toBe('landslide')
    expect(feature.id).toBe('station:RG-540121-01')
  })

  it('同一站点多条读数合成一个要素，每指标只留最新一条', () => {
    const layer = buildStationLayer({
      readings: [
        reading('S1', '540121', 'rain_10min', 3, { observed_at: '2026-09-30T01:00:00.000Z' }),
        reading('S1', '540121', 'rain_10min', 48, { observed_at: '2026-09-30T02:00:00.000Z' }),
        reading('S1', '540121', 'debris_level', 1.4, { quality_flag: 'suspect' }),
      ],
      stations: [],
      anchors: indexAnchors(ANCHORS),
      risks: {},
    })
    const feature = layer.plotted[0]
    expect(feature.samples).toHaveLength(2)
    expect(feature.samples.map((sample) => sample.metric)).toEqual(['debris_level', 'rain_10min'])
    expect(feature.samples.find((sample) => sample.metric === 'rain_10min')?.value).toBe(48)
    expect(feature.suspectCount).toBe(1)
    expect(feature.riskLevel).toBeNull()
    expect(feature.colorCss).toBe(UNKNOWN_RISK_COLOR)
  })

  it('台账与遥测取并集：只有台账的站点也上图（后端补齐 /stations 后无需改前端）', () => {
    const layer = buildStationLayer({
      readings: [reading('RG-540121-01', '540121')],
      stations: [station({ station_id: 'GNS-540121-02', lon: 91.3, lat: 30.05, elevation_m: 4200 })],
      anchors: indexAnchors(ANCHORS),
      risks: {},
    })
    expect(layer.all.map((feature) => feature.stationId)).toEqual(['GNS-540121-02', 'RG-540121-01'])
    const withLedger = layer.all.find((feature) => feature.stationId === 'GNS-540121-02')
    expect(withLedger?.lonlat).toEqual({ lon: 91.3, lat: 30.05 })
    expect(withLedger?.elevationM).toBe(4200)
    expect(withLedger?.nameZh).toBe('林周泥沟监测站')
  })

  it('latestSamples 对空读数返回空表', () => {
    expect(latestSamples([])).toEqual({ samples: [], suspectCount: 0 })
  })
})

// ---------- 区域风险聚合 ----------

describe('区域风险来源', () => {
  it('链路评估优先于预警记录', () => {
    const risks = riskByRegion(
      [chain('540121', 1)],
      [warning({ warning_id: 'w1', region_codes: ['540121'], risk_level: 4 })],
    )
    expect(risks['540121']).toMatchObject({ riskLevel: 1, source: 'chain' })
  })

  it('events 是追加序（旧→新）：同区域最后一条胜出', () => {
    const risks = riskByRegion([chain('540121', 4, 'evt_old'), chain('540121', 1, 'evt_new')], [])
    expect(risks['540121']?.riskLevel).toBe(1)
    expect(risks['540121']?.at).toBe('evt_new')
  })

  it('无链路评估的区域由预警兜底；risk=null 的链路不产生条目', () => {
    const risks = riskByRegion([chain(null), chain('540221', null)], [warning({ warning_id: 'w', region_codes: ['540221'], risk_level: 3 })])
    expect(risks['540221']).toMatchObject({ source: 'warning', riskLevel: 3 })
    expect(Object.keys(risks)).toEqual(['540221'])
  })

  it('越界的后端等级值不污染配色：判为未知', () => {
    const risks = riskByRegion([], [warning({ warning_id: 'w', risk_level: 9 as unknown as 1 })])
    expect(risks['540121']?.riskLevel).toBeNull()
    expect(riskColorCss(risks['540121']?.riskLevel ?? null)).toBe(UNKNOWN_RISK_COLOR)
  })
})

// ---------- 图层 2：预警 ----------

describe('预警图层', () => {
  it('一条预警覆盖 N 个区域 → N 个 marker，id 带区域后缀', () => {
    const layer = buildWarningLayer({
      warnings: [warning({ warning_id: 'wrn_1', region_codes: ['540121', '540221'] })],
      anchors: indexAnchors(ANCHORS),
    })
    expect(layer.plotted.map((feature) => feature.id)).toEqual(['warning:wrn_1@540121', 'warning:wrn_1@540221'])
    expect(layer.plotted[0].colorCss).toBe('#fa8c16')
  })

  it('未烘焙锚点的区域进未定位清单（而不是 (0,0)）', () => {
    const layer = buildWarningLayer({
      warnings: [warning({ warning_id: 'wrn_1', region_codes: ['540321'] })],
      anchors: indexAnchors(ANCHORS),
    })
    expect(layer.plotted).toHaveLength(0)
    expect(toUnlocatedItems(layer.unplaced)[0]).toMatchObject({ reason: 'region-without-anchor', regionCode: '540321' })
  })

  it('risk_level 与 hazard_type 异常时不崩：等级未知、灾种未定', () => {
    const layer = buildWarningLayer({
      warnings: [warning({ warning_id: 'wrn_x', risk_level: 0 as unknown as 1, hazard_type: 'ice' as WarningRecord['hazard_type'] })],
      anchors: indexAnchors(ANCHORS),
    })
    expect(layer.plotted[0].riskLevel).toBeNull()
    expect(layer.plotted[0].hazardType).toBe('unknown')
    expect(layer.plotted[0].colorCss).toBe(UNKNOWN_RISK_COLOR)
  })

  it('region_codes 缺失时仍产出一个条目，理由是无区域编码', () => {
    const layer = buildWarningLayer({ warnings: [warning({ warning_id: 'w', region_codes: [] })], anchors: indexAnchors(ANCHORS) })
    expect(layer.plotted).toHaveLength(0)
    expect(toUnlocatedItems(layer.unplaced)[0].reason).toBe('no-anchor')
  })
})

// ---------- 图层 3：触达 ----------

describe('通道触达汇总', () => {
  it('delivered 与 retried 都算成功，受众只累计成功通道', () => {
    const summary = summarizeReach([
      delivery('app', 'delivered', 120),
      delivery('beidou', 'retried', 30),
      delivery('sms', 'failed', 500),
      delivery('broadcast', 'pending', 800),
    ])
    expect(summary).toMatchObject({ attempts: 4, delivered: 2, failed: 1, pending: 1, audienceCount: 150, degraded: true })
    expect(summary.deliveredChannels).toEqual(['app', 'beidou'])
  })

  it('全部成功 = 不劣化；一条都没送达 = 劣化；无投递 = 不劣化', () => {
    expect(summarizeReach([delivery('app', 'delivered', 5)]).degraded).toBe(false)
    expect(summarizeReach([delivery('app', 'pending', 5)]).degraded).toBe(true)
    expect(summarizeReach([]).degraded).toBe(false)
  })

  it('触达图层跳过无投递的预警，劣化项标红', () => {
    const layer = buildReachLayer({
      warnings: [
        warning({ warning_id: 'w_ok', deliveries: [delivery('app', 'delivered', 20)] }),
        warning({ warning_id: 'w_bad', deliveries: [delivery('beidou', 'failed', 20)] }),
        warning({ warning_id: 'w_none', deliveries: [] }),
      ],
      anchors: indexAnchors(ANCHORS),
    })
    expect(layer.plotted.map((feature) => feature.warningId)).toEqual(['w_ok', 'w_bad'])
    const degraded = layer.plotted.find((feature) => feature.warningId === 'w_bad')
    expect(degraded?.degraded).toBe(true)
    expect(degraded?.colorCss).toBe('#cf1322')
    expect(layer.plotted[0].title).toContain('触达 1/1')
  })
})

// ---------- 图层 4：灾害分区面 ----------

const SQUARE = [
  [
    [91.0, 30.0],
    [91.2, 30.0],
    [91.2, 30.2],
    [91.0, 30.2],
    [91.0, 30.0],
  ],
]

describe('灾害面图层（GeoJSON 校验）', () => {
  it('Polygon + 洞：闭合点被剥掉，bbox 与配色齐备', () => {
    const doc = {
      type: 'FeatureCollection',
      features: [
        {
          type: 'Feature',
          properties: { id: 'zone-1', name: '擦巴拉沟泥石流区', region_code: '540121', risk_level: 1 },
          geometry: { type: 'Polygon', coordinates: [...SQUARE, [[91.05, 30.05], [91.1, 30.05], [91.1, 30.1], [91.05, 30.1], [91.05, 30.05]]] },
        },
      ],
    }
    const { polygons, rejected } = buildHazardZoneLayer(doc)
    expect(rejected).toHaveLength(0)
    expect(polygons[0]).toMatchObject({ id: 'zone-1', title: '擦巴拉沟泥石流区', riskLevel: 1, colorCss: '#cf1322' })
    expect(polygons[0].outer).toHaveLength(4)
    expect(polygons[0].holes).toHaveLength(1)
    expect(polygons[0].bbox).toEqual({ west: 91.0, south: 30.0, east: 91.2, north: 30.2 })
  })

  it('MultiPolygon 拆成多个面，各自可点选', () => {
    const doc = {
      type: 'FeatureCollection',
      features: [
        {
          type: 'Feature',
          properties: { id: 'multi' },
          geometry: { type: 'MultiPolygon', coordinates: [SQUARE, SQUARE.map((ring) => ring.map(([lon, lat]) => [lon + 1, lat + 1]))] },
        },
      ],
    }
    const { polygons } = buildHazardZoneLayer(doc)
    expect(polygons.map((polygon) => polygon.id)).toEqual(['multi#0', 'multi#1'])
  })

  it('非面几何/坏环/越界坐标都被拒绝并写明理由，不静默丢', () => {
    const doc = {
      type: 'FeatureCollection',
      features: [
        { type: 'Feature', properties: { id: 'line' }, geometry: { type: 'LineString', coordinates: [[91, 30], [92, 31]] } },
        { type: 'Feature', properties: { id: 'short' }, geometry: { type: 'Polygon', coordinates: [[[91, 30], [91.1, 30], [91.1, 30.1]]] } },
        { type: 'Feature', properties: { id: 'oob' }, geometry: { type: 'Polygon', coordinates: [[[200, 30], [91, 30], [91, 31], [200, 30]]] } },
        { type: 'Feature', properties: { id: 'nogeom' }, geometry: null },
      ],
    }
    const { polygons, rejected } = buildHazardZoneLayer(doc)
    expect(polygons).toHaveLength(0)
    expect(rejected.map((item) => item.reason)).toEqual([
      'unsupported-geometry',
      'ring-too-short',
      'coord-out-of-range',
      'missing-geometry',
    ])
  })

  it('文档整体不可用时给一条汇总性拒绝', () => {
    expect(buildHazardZoneLayer(null).rejected[0].reason).toBe('not-a-feature-collection')
    expect(buildHazardZoneLayer('{}').rejected[0].reason).toBe('not-a-feature-collection')
    expect(buildHazardZoneLayer({ type: 'FeatureCollection' }).rejected[0].reason).toBe('not-a-feature-collection')
  })

  it('单几何（非 FeatureCollection）也能画', () => {
    const { polygons } = buildHazardZoneLayer({ type: 'Polygon', coordinates: SQUARE })
    expect(polygons).toHaveLength(1)
  })

  it('properties 里没有风险时用区域评估兜底', () => {
    const risks = riskByRegion([chain('540121', 2)], [])
    const { polygons } = buildHazardZoneLayer(
      { type: 'FeatureCollection', features: [{ type: 'Feature', properties: { id: 'z', region_code: '540121' }, geometry: { type: 'Polygon', coordinates: SQUARE } }] },
      { risks },
    )
    expect(polygons[0]).toMatchObject({ riskLevel: 2, hazardType: 'landslide', colorCss: '#fa8c16' })
  })
})

// ---------- 视野过滤 ----------

describe('视野过滤', () => {
  const layers = buildLayers({
    readings: [reading('S1', '540121'), reading('S2', '540221')],
    stations: [],
    warnings: [],
    chains: [],
    anchors: ANCHORS,
    hazardZones: {
      type: 'FeatureCollection',
      features: [{ type: 'Feature', properties: { id: 'z1' }, geometry: { type: 'Polygon', coordinates: SQUARE } }],
    },
  })

  it('点在边界上算在视野内（闭区间），越界才剔除', () => {
    const box = { west: 91.287, south: 30.041, east: 120, north: 40 }
    expect(pointInBBox({ lon: 91.287, lat: 30.041 }, box)).toBe(true)
    expect(pointInBBox({ lon: 91.286, lat: 30.041 }, box)).toBe(false)
    const filtered = filterLayersByViewport({ bbox: box, layers })
    expect(filtered.stations.map((feature) => feature.stationId)).toEqual(['S1'])
  })

  it('空视野结果是空数组而不是 undefined', () => {
    const filtered = filterLayersByViewport({ bbox: { west: -120, south: -50, east: -110, north: -40 }, layers })
    expect(filtered.stations).toEqual([])
    expect(filtered.hazards).toEqual([])
    expect(filtered.warnings).toEqual([])
  })

  it('bbox=null 或非法 bbox 表示不过滤', () => {
    expect(filterLayersByViewport({ bbox: null, layers }).stations).toHaveLength(2)
    expect(filterLayersByViewport({ bbox: { west: 120, south: 30, east: 90, north: 20 }, layers }).stations).toHaveLength(2)
    expect(isUsableBBox({ west: 1, south: 2, east: 0, north: 3 })).toBe(false)
    expect(isUsableBBox({ west: 0, south: 0, east: 1, north: Number.NaN })).toBe(false)
  })

  it('未上图要素不受视野影响（否则转开视角会像数据没了）', () => {
    const withUnplaced = buildLayers({ readings: [reading('S9', '549999')], stations: [], warnings: [], chains: [], anchors: ANCHORS, hazardZones: null })
    const filtered = filterLayersByViewport({ bbox: { west: -170, south: -80, east: -160, north: -70 }, layers: withUnplaced })
    expect(filtered.stations).toHaveLength(0)
    expect(filtered.unplaced).toHaveLength(1)
  })

  it('面用 bbox 相交：恰好相切也算相交', () => {
    const touching = { west: 91.2, south: 30.2, east: 95, north: 33 }
    expect(filterLayersByViewport({ bbox: touching, layers }).hazards).toHaveLength(1)
    expect(filterLayersByViewport({ bbox: { west: 91.21, south: 30.21, east: 95, north: 33 }, layers }).hazards).toHaveLength(0)
  })

  it('expandBBox 外扩后仍在合法范围内', () => {
    const expanded = expandBBox({ west: 0, south: -90, east: 10, north: 90 })
    expect(expanded.south).toBe(-90)
    expect(expanded.north).toBe(90)
    expect(expanded.west).toBeLessThan(0)
  })
})

// ---------- 风险等级筛选（面板那排芯片的唯一判据）----------

describe('风险等级筛选', () => {
  const LEVELS: RiskLevel[] = [1, 2, 3, 4, 5]
  /**
   * 夹具构成（数字都据此钉死）：
   * - 一条 540121 的链路风险=1 ⇒ 该区域的**站点也继承**这个等级（`entities.ts:545` 走的是同一份 risk）；
   * - 三条预警：1 条等级 1、2 条等级 3；
   * - 触达层要有 `deliveries` 才生成（`buildReachLayer` 里 `attempts===0` 直接 continue），它的 `riskLevel` 恒为 null。
   */
  const mixed = buildLayers({
    readings: [reading('S1', '540121')],
    stations: [],
    warnings: [
      warning({ warning_id: 'w1', risk_level: 1, region_codes: ['540121'], deliveries: [delivery('app', 'delivered', 20)] }),
      warning({ warning_id: 'w2', risk_level: 3, region_codes: ['540221'] }),
      warning({ warning_id: 'w3', risk_level: 3, region_codes: ['540121'] }),
    ],
    chains: [chain('540121', 1, 'evt_000000000001')],
    anchors: ANCHORS,
    hazardZones: null,
  })

  it('夹具本身站得住：三条预警两种等级，且触达层有要素（否则下面几条在空跑）', () => {
    expect(mixed.warnings).toHaveLength(3)
    expect(mixed.reach.length, '触达层是"没有风险评级"的那一层，必须有要素才验得到').toBeGreaterThan(0)
    expect(mixed.stations.map((feature) => feature.riskLevel), '站点应当继承区域风险').toEqual([1])
  })

  it('空选择＝不过滤：原对象直接返回，不白跑一遍 filter', () => {
    expect(filterLayersByRisk(mixed, [])).toBe(mixed)
  })

  it('只留红色时一条黄色都不剩', () => {
    const only = filterLayersByRisk(mixed, [1])
    expect(only.warnings.map((feature) => feature.riskLevel)).toEqual([1])
  })

  it('多选是并集，且点的顺序不影响结果', () => {
    const a = filterLayersByRisk(mixed, [1, 3]).warnings.map((feature) => feature.id)
    const b = filterLayersByRisk(mixed, [3, 1]).warnings.map((feature) => feature.id)
    expect(a).toEqual(b)
    expect(a).toHaveLength(3)
  })

  it('没评级的要素任何筛选下都保留（触达标记不是"低风险"，是这个维度不适用）', () => {
    for (const level of LEVELS) {
      expect(filterLayersByRisk(mixed, [level]).reach, `只留等级 ${level} 时触达层被整层藏掉了`).toEqual(mixed.reach)
    }
  })

  it('芯片计数只数有评级的：站点 1 + 预警 3 = 等级 1 两条、等级 3 两条', () => {
    const counts = riskLevelCounts(mixed)
    expect(counts[1]).toBe(2)
    expect(counts[3]).toBe(2)
    expect(counts[2]).toBe(0)
    expect(counts[4]).toBe(0)
    expect(counts[5]).toBe(0)
    const graded = [...mixed.stations, ...mixed.warnings, ...mixed.reach, ...mixed.hazards].filter(
      (feature) => feature.riskLevel !== null,
    )
    expect(LEVELS.reduce((sum, level) => sum + counts[level], 0)).toBe(graded.length)
    for (const level of LEVELS) {
      const direct = graded.filter((feature) => feature.riskLevel === level).length
      expect(counts[level], `等级 ${level} 的计数与逐条重数不一致`).toBe(direct)
    }
  })
})

// ---------- 聚合 ----------

describe('聚合与缩放分档', () => {
  const points = buildLayers({
    readings: Array.from({ length: 30 }, (_, index) => reading(`S${String(index).padStart(2, '0')}`, '540121')),
    stations: Array.from({ length: 30 }, (_, index) =>
      station({ station_id: `S${String(index).padStart(2, '0')}`, lon: 91 + index * 0.01, lat: 30 + (index % 3) * 0.01 }),
    ),
    warnings: [],
    chains: [chain('540121', 1)],
    anchors: ANCHORS,
    hazardZones: null,
  })

  it('相机越高缩放越大，且被夹在 0..21', () => {
    expect(zoomForCameraHeight(637_100_000)).toBe(0)
    expect(zoomForCameraHeight(1_000_000)).toBeGreaterThan(zoomForCameraHeight(10_000_000))
    expect(zoomForCameraHeight(1)).toBe(21)
    expect(zoomForCameraHeight(Number.NaN)).toBe(0)
    expect(clusterCellDegrees(12)).toBe(0)
    expect(clusterCellDegrees(6)).toBe(1)
  })

  it('点数不足或缩放够细都不聚合', () => {
    expect(shouldCluster(23, 6)).toBe(false)
    expect(shouldCluster(24, 6)).toBe(true)
    expect(shouldCluster(1_000, 12)).toBe(false)
  })

  it('网格键：恰在格边界上的点归入上一格，不重复计数', () => {
    expect(clusterCellKey({ lon: 91.4, lat: 30.2 }, 1)).toBe('91:30')
    expect(clusterCellKey({ lon: 92.0, lat: 30.0 }, 1)).toBe('92:30')
    expect(clusterCellKey({ lon: -91.4, lat: -30.2 }, 1)).toBe('-92:-31')
  })

  it('聚合结果：计数正确、颜色取最坏、成员有序、输出确定性', () => {
    const clusters = clusterPoints(points.stations, 0.05)
    expect(clusters.length).toBeGreaterThan(0)
    expect(clusters.reduce((sum, cluster) => sum + cluster.count, 0)).toBe(30)
    expect(clusters[0].colorCss).toBe('#cf1322')
    expect(clusters[0].worstRisk).toBe(1)
    expect(clusters[0].memberIds).toEqual([...clusters[0].memberIds].sort())
    expect(clusterPoints(points.stations, 0.05)).toEqual(clusters)
    expect(clusterPoints(points.stations, 0)).toEqual([])
  })

  it('渲染计划：达到阈值时点变簇，触达层与面不参与聚合', () => {
    const plan = buildRenderPlan({ layers: points, bbox: null, zoom: 6 })
    expect(plan.clustered).toBe(true)
    expect(plan.stations).toEqual([])
    expect(plan.clusters.length).toBeGreaterThan(0)
    const plain = buildRenderPlan({ layers: points, bbox: null, zoom: 13 })
    expect(plain.clustered).toBe(false)
    expect(plain.stations).toHaveLength(30)
    expect(plain.clusters).toEqual([])
  })

  it('默认图层开关：触达层关，其余开', () => {
    expect(DEFAULT_LAYER_VISIBILITY).toMatchObject({ stations: true, warnings: true, reach: false, hazards: true, clusters: true })
  })
})

// ---------- 组装与详情索引 ----------

describe('装配与点击→详情', () => {
  const layers = buildLayers({
    readings: [reading('S1', '540121'), reading('S9', '549999')],
    stations: [station({ station_id: 'S1', lon: 91.5, lat: 30.5 })],
    warnings: [warning({ warning_id: 'w1', region_codes: ['540121'], deliveries: [delivery('beidou', 'delivered', 40)] })],
    chains: [chain('540121', 1)],
    anchors: ANCHORS,
    hazardZones: { type: 'FeatureCollection', features: [{ type: 'Feature', properties: { id: 'z1' }, geometry: { type: 'Polygon', coordinates: SQUARE } }] },
  })

  it('counts 只统计上图要素（S9 无锚点，不进计数）', () => {
    expect(layerCounts(layers)).toEqual({ stations: 1, warnings: 1, reach: 1, hazards: 1 })
  })

  it('未定位清单来自未上图要素，理由可读', () => {
    const items = toUnlocatedItems(layers.unplaced)
    expect(items).toHaveLength(1)
    expect(items[0]).toMatchObject({ id: 'station:S9', kind: 'station', reason: 'station-without-coord' })
  })

  it('点选：上图与未上图要素都能查到详情', () => {
    expect(detailOf(layers, 'station:S1')?.kind).toBe('station')
    expect(detailOf(layers, 'warning:w1@540121')).toMatchObject({ kind: 'warning', regionCode: '540121' })
    expect(detailOf(layers, 'station:S9')).toMatchObject({ kind: 'station' })
    expect(detailOf(layers, 'z1')).toMatchObject({ kind: 'hazard-zone' })
    expect(detailOf(layers, 'nope')).toBeNull()
    expect(detailOf(layers, null)).toBeNull()
  })

  it('空输入不崩：全部为空数组', () => {
    const empty = buildLayers({ readings: [], stations: [], warnings: [], chains: [], anchors: [], hazardZones: null })
    expect(empty.stations).toEqual([])
    expect(empty.rejected[0].reason).toBe('not-a-feature-collection')
    expect(buildRenderPlan({ layers: EMPTY_LAYERS, bbox: null, zoom: 4 })).toMatchObject({ clustered: false, stations: [] })
  })
})

describe('未定位清单：原因要说真话，总数要能拆开', () => {
  it('站点缺坐标的原因不再写"后端尚未开放 /api/v1/stations"（那条路由早就开着）', async () => {
    const { UNLOCATED_LABELS } = await import('@/components/map/entities')
    expect(UNLOCATED_LABELS['station-without-coord']).not.toContain('尚未开放')
    expect(UNLOCATED_LABELS['station-without-coord']).toContain('台账')
  })

  it('按原因分组并按数量排序：一个笼统数字答不了"缺哪几样"', () => {
    const items = [
      ...Array.from({ length: 6 }, (_, i) => ({ id: `station:s${i}`, kind: 'station' as const, title: `s${i}`, regionCode: '540121', reason: 'station-without-coord' as const })),
      ...Array.from({ length: 72 }, (_, i) => ({ id: `warning:w${i}`, kind: 'warning' as const, title: `w${i}`, regionCode: '540221', reason: 'region-without-anchor' as const })),
    ]
    const groups = groupUnlocatedByReason(items)
    expect(groups.map((g) => g.count)).toEqual([72, 6])
    expect(groups[0].label).toContain('锚点')
    expect(groups[1].label).toContain('台账')
    expect(groups.reduce((sum, g) => sum + g.count, 0)).toBe(items.length)
  })

  it('空清单不编出分组：没缺东西就说没缺', () => {
    expect(groupUnlocatedByReason([])).toEqual([])
  })
})
/**
 * 「定位到有点的区域」先前点了没任何反应：本机形态的站点台账一个经纬度都没有
 * （`/api/v1/stations` 的 `lon/lat` 全为 null），外接框算不出来，相机退回全局视野，
 * 而界面正停在全局视野上——于是这颗按钮看起来是死的，读的人只能再点一次。
 * 按钮该做的是把"退不回"的原因说出来。
 */
describe('focusAllNote：定位按完该说什么', () => {
  it('一个在册站点都没有', () => {
    expect(focusAllNote(0, 0)).toBe('还没有在册站点，已退回全局视野。')
  })

  it('站点在册但全都没有经纬度', () => {
    const note = focusAllNote(0, 5)
    expect(note).toContain('5 个在册站点都没有经纬度')
    expect(note).toContain('站点台账入库')
  })

  it('部分有坐标：说清按几个点定位，其余仍在未定位清单里', () => {
    expect(focusAllNote(3, 5)).toContain('3/5')
  })

  it('全都定位上了就闭嘴（没话说时不编一句提示）', () => {
    expect(focusAllNote(5, 5)).toBe('')
  })
})

/**
 * "上图要素"这个数的唯一用处是让人核对"画面上看到的对不对得上"。
 * 原先它是四层数据直接相加：图层全关掉它不动，默认关着的触达层却一直在数里。
 */
describe('plottedCount：只数开着的层', () => {
  const layers = {
    stations: Array.from({ length: 2 }),
    warnings: Array.from({ length: 1 }),
    reach: Array.from({ length: 3 }),
    hazards: Array.from({ length: 4 }),
  } as never

  it('默认视图下触达层是关的，它那 3 条不算上图', () => {
    expect(plottedCount(layers, DEFAULT_LAYER_VISIBILITY)).toBe(2 + 1 + 0 + 4)
  })

  it('四层的开关各自决定进不进数', () => {
    expect(plottedCount(layers, { ...DEFAULT_LAYER_VISIBILITY, reach: true, stations: false, warnings: false, hazards: false })).toBe(3)
    expect(plottedCount(layers, { ...DEFAULT_LAYER_VISIBILITY, stations: false })).toBe(1 + 0 + 4)
  })

  it('全关掉就是 0：画面上一个都不剩时不许还写着要素数', () => {
    expect(plottedCount(layers, { stations: false, warnings: false, reach: false, hazards: false, clusters: false })).toBe(0)
  })

  it('一张图页用的是这个函数，不是又写一遍相加', () => {
    const view = readRepoFile('frontend', 'src', 'views', 'MapView.vue')
    expect(view).toContain('plottedCount(filterLayersByViewport(')
    expect(view).not.toContain('counts.value.stations + counts.value.warnings')
  })
})

/**
 * 一张图 `load()` 的迟到响应守卫。
 *
 * 真机量到（`.tmp-verify/map-region-stale.mjs`，把无过滤那一趟 `/api/v1/warnings` 的**响应**
 * 延迟 3 秒递回——`route.fetch()` 先取走快照再 `fulfill`，只迟"送"不迟"发"）：
 * 点刷新 → 立刻把区域换成 540121，清单先是对的 **18 条**；3.1 秒后那一趟无过滤的旧快照递进来，
 * 数字跳回全区域的 **64 条**并一直停在那里，而选框上仍写着 `540121`——筛的是 A、图上画的是全部，
 * 页面一句提示都没有。30 秒自动刷新与手动刷新撞在一起也是同一条路径。
 *
 * MapView 挂着 Cesium，这一页没有组件级挂载用例，所以这里对源码对账；
 * 三条都要"摘掉守卫即红"（见提交说明里的变异记录）。
 */
describe('一张图 load() 的迟到响应守卫', () => {
  function text(): string {
    return readRepoFile('frontend', 'src', 'views', 'MapView.vue')
  }

  it('每一趟 load 占一个号，写画面之前先比对', () => {
    expect(text()).toContain('const seq = ++loadSeq')
    expect(text()).toMatch(/if \(seq !== loadSeq\) return/)
  })

  it('比对必须排在第一次写画面之前', () => {
    const source = text()
    const guard = source.indexOf('if (seq !== loadSeq) return')
    const firstWrite = source.indexOf('readings.value = settled(telemetry)')
    expect(guard, '守卫不在就等于没有守卫').toBeGreaterThanOrEqual(0)
    expect(firstWrite).toBeGreaterThan(guard)
  })

  it('号在发请求之前占：晚占就变成"谁后回谁赢"，而人要的是"谁后点谁赢"', () => {
    const source = text()
    expect(source.indexOf('const seq = ++loadSeq')).toBeLessThan(source.indexOf('await Promise.allSettled(['))
  })
})
