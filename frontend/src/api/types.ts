/** 与后端契约同构的前端类型（contracts/*.schema.json 的 TS 视图）。 */

export type HazardType =
  | 'landslide'
  | 'rockfall'
  | 'debris_flow'
  | 'avalanche'
  | 'lake_outburst'
  | 'quake_triggered'
  | 'unknown'

/** 风险等级：1 最高（红），5 无风险。 */
export type RiskLevel = 1 | 2 | 3 | 4 | 5

export const HAZARD_LABELS: Record<HazardType, string> = {
  landslide: '滑坡',
  rockfall: '崩塌危岩',
  debris_flow: '泥石流',
  avalanche: '冰雪雪崩',
  lake_outburst: '冰湖溃决',
  quake_triggered: '震入灾害链',
  unknown: '未定灾种',
}

export const RISK_LABELS: Record<RiskLevel, string> = {
  1: '红色',
  2: '橙色',
  3: '黄色',
  4: '蓝色',
  5: '无风险',
}

export const RISK_COLORS: Record<RiskLevel, string> = {
  1: '#cf1322',
  2: '#fa8c16',
  3: '#fadb14',
  4: '#1677ff',
  5: '#8c8c8c',
}

export interface TelemetryReading {
  station_id: string
  metric: string
  value: number
  unit: string
  region_code: string
  observed_at: string
  ingested_at: string
  source: string
  quality_flag: 'ok' | 'suspect' | 'missing' | 'drift'
}

export interface DeliveryAttempt {
  channel: string
  audience_count: number
  status: 'pending' | 'delivered' | 'failed' | 'retried'
  attempted_at: string
  receipt_at?: string | null
  provider_msg_id?: string | null
}

export interface WarningRecord {
  warning_id: string
  event_id: string
  trace_id: string
  hazard_type: HazardType
  region_codes: string[]
  risk_level: RiskLevel
  title_zh: string
  body_zh: string
  body_bo?: string | null
  audiences: string[]
  channels: string[]
  translation_pending: boolean
  generated_at: string
  released_at?: string | null
  deliveries: DeliveryAttempt[]
}

export interface TaskUnit {
  task_unit_id: string
  event_id: string
  hazard_type: string
  region_code: string
  task_type: string
  objective: string
  priority: number
  sla_seconds: number
  required_capabilities: string[]
  owner_role: string
  dependencies: string[]
  created_by: string
  created_at: string
}

export interface StageResult {
  name: string
  mode: 'agent' | 'local' | 'hybrid' | 'skipped'
  ok: boolean
  latency_ms: number
  note: string
}

export interface ChainSummary {
  trace_id: string
  event_id: string
  ok: boolean
  acted: boolean
  stages: StageResult[]
  risk?: {
    hazard_type: HazardType
    region_code: string
    risk_level: RiskLevel
    confidence: number
    rationale: string
  } | null
  task_units: string[]
  warning_id?: string | null
  errors: string[]
  degradations: string[]
}

export interface AgentInfo {
  agent_id: string
  agent_type: string
  capabilities: string[]
  hazard_types: string[]
  healthy: boolean
  inflight: number
  max_concurrency: number
  served: number
  failed: number
  version: string
  idle_seconds: number
}

export interface LatencyStats {
  count: number
  p50_ms: number
  p95_ms: number
  p99_ms: number
  max_ms: number
  mean_ms: number
  budget_ms?: number
  breaches?: number
  breach_rate?: number
}

export interface LatencyReport {
  sla_thresholds: Record<string, number>
  metrics: Record<string, LatencyStats>
  collaboration: { transactions: number; success_rate: number | null; target: number; pass: boolean }
  violations: Record<string, number>
  gateway_counters: Record<string, number>
  store: Record<string, number>
}

export interface DrillResponse {
  ingest: {
    readings: number
    sources_ok: string[]
    sources_failed: Record<string, string>
    published: number
    max_ingest_latency_seconds: number
  }
  chains: ChainSummary[]
  regions: number
  note?: string
}
