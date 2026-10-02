/**
 * 装配事实（`GET /api/v1/integrations`）的前端契约层。
 *
 * 后端把"哪条腿在跑、哪条腿是瘸的"做成了一等公民（`backend/src/aegis/integrations.py`），
 * 但只有接口能看见等于现场没人看得见：大屏值守时要判断"预案为什么变慢"，
 * 第一眼看的就是这个面板，而不是去翻日志。
 *
 * 两条硬约束（决定这里能声明什么）：
 * - **不硬编码腿清单**：行是后端给的，前端只负责贴中文标签。后端加一条腿（现在有 7 条）
 *   必须在 UI 上直接出现，而不是因为不在标签表里被过滤掉——那等于把新故障面藏起来；
 * - **详情里的值一律按字符串渲染**：后端已在出口处脱敏（DSN/URI 只留 host[:port]），
 *   前端再把 key 名含 `password`/`token`/`secret`/`dsn`/`uri` 的值隐掉，
 *   避免哪天有人把完整串塞进 detail 就顺着大屏公开出去了。
 */

import axios, { type AxiosInstance, type AxiosRequestConfig } from 'axios'

export interface IntegrationRow {
  readonly name: string
  readonly enabled: boolean
  readonly driver: string
  readonly detail: Record<string, unknown>
}

export interface IntegrationSnapshot {
  readonly items: IntegrationRow[]
  readonly degraded: string[]
  readonly all_enabled: boolean
}

/** 后端 `IntegrationState.name` 的中文标签；未登记的名字原样显示（见文件头约束）。 */
export const LEG_LABELS: Record<string, string> = {
  store: '运行态存储',
  analytics: '分析旁路',
  knowledge: '案例知识层',
  retrieval: '混合检索',
  mqtt: 'MQTT 推送腿',
  weather: '气象拉取腿',
  tracing: '链路追踪',
}

const SECRET_KEY = /(password|passwd|secret|token|api[_-]?key|dsn|uri|authorization)/i

/**
 * 单个详情值的展示上限。真实后端会往 detail 里塞整段数据集出处说明（长度由
 * `integrations.spec.ts` 直接从 `knowledge/cases.py` 量出，超过这个上限），
 * 一行放不下就得截断，但截断不许等于丢弃——完整串挂到 title 上。
 */
export const MAX_DETAIL_CHARS = 64

export function legLabel(name: string): string {
  return LEG_LABELS[name] ?? name
}

/** 与后端 `IntegrationState.degradation_reason()` 同一口径：标记键有值才算降级。 */
function isDegradationMark(key: string, value: unknown): boolean {
  const marked = key.startsWith('degraded') || key.endsWith('_error') || key === 'error'
  return marked && String(value ?? '').trim() !== ''
}

/** 三态：未启用 / 已启用 / 已启用但降级。后端把"装配时跳过"和"跑起来后降级"分成两行事实。 */
export function legState(row: IntegrationRow): 'disabled' | 'enabled' | 'degraded' {
  if (!row.enabled) return 'disabled'
  return Object.entries(row.detail ?? {}).some(([key, value]) => isDegradationMark(key, value)) ? 'degraded' : 'enabled'
}

export function legStateLabel(state: ReturnType<typeof legState>): string {
  return { disabled: '未启用', enabled: '运行中', degraded: '降级运行' }[state]
}

/** 详情里可展示的键值：凭据类键一律不给看。 */
export function visibleDetail(row: IntegrationRow): Array<{ key: string; value: string; hint: string }> {
  return Object.entries(row.detail ?? {})
    .filter(([key]) => !SECRET_KEY.test(key))
    .map(([key, value]) => {
      const text = formatDetail(value)
      if (text.length <= MAX_DETAIL_CHARS) return { key, value: text, hint: '' }
      return { key, value: `${text.slice(0, MAX_DETAIL_CHARS - 1)}…`, hint: text }
    })
}

function formatDetail(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'number' || typeof value === 'boolean' || typeof value === 'string') return String(value)
  return Array.isArray(value) ? value.join('、') : JSON.stringify(value)
}

export class IntegrationsApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'IntegrationsApiError'
    this.status = status
  }
}

export async function fetchIntegrations(instance?: AxiosInstance): Promise<IntegrationSnapshot> {
  const http: AxiosInstance = instance ?? axios.create({ baseURL: '/', timeout: 8_000 })
  const config: AxiosRequestConfig = { url: '/api/v1/integrations', method: 'get' }
  try {
    const response = await http.request<IntegrationSnapshot>(config)
    const items = Array.isArray(response.data?.items) ? response.data.items : []
    return {
      items: items.map((item) => ({
        name: String(item.name ?? ''),
        enabled: item.enabled === true,
        driver: String(item.driver ?? ''),
        detail: item.detail && typeof item.detail === 'object' ? (item.detail as Record<string, unknown>) : {},
      })),
      degraded: Array.isArray(response.data?.degraded) ? response.data.degraded.map(String) : [],
      all_enabled: response.data?.all_enabled === true,
    }
  } catch (error) {
    if (axios.isAxiosError(error)) {
      throw new IntegrationsApiError(error.response?.status ?? 0, `装配事实读取失败: ${error.message}`)
    }
    throw error
  }
}
