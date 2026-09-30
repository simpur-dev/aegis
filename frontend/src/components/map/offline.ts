/**
 * 「一张图」离线/弱网状态机（纯逻辑，无框架、无 Cesium、无网络）。
 *
 * 现场口径（西藏高原野外）有三档现实：
 * 1. `online`   —— 后端 + 底图瓦片 + 地形瓦片全部可达：完整三维一张图；
 * 2. `degraded` —— 还能出图或还能读数，但至少一路缺失（地形没烘焙 → 椭球面；底图没烘焙 → 无影像；
 *    后端断但本地切片在 → 只看离线图）。这一档**必须继续渲染**，不能白屏；
 * 3. `offline`  —— 实时数据与本地底图两路都没有：只剩文字清单。
 *
 * 判定与"渲染"分离：本文件只回答"现在处于哪一态 / 某一路源能不能用"，
 * MapView 只负责把状态显示出来，viewer.ts 只负责按结论挑实现。
 * 探针的 fetch 由调用方注入（生产用 `window.fetch`，单测用假函数），因此测试不触网。
 */

// ---------- 事实与状态 ----------

export type NetworkStatus = 'online' | 'degraded' | 'offline'

export const NETWORK_LABELS: Record<NetworkStatus, string> = {
  online: '源齐备',
  degraded: '降级运行',
  offline: '离线/不可达',
}

/** Ant Design Vue `a-tag` 的颜色口径（视图直接取用，避免各处硬编码）。 */
export const NETWORK_TAG_COLORS: Record<NetworkStatus, 'green' | 'orange' | 'red'> = {
  online: 'green',
  degraded: 'orange',
  offline: 'red',
}

export interface SourceFacts {
  /** `navigator.onLine`：浏览器口径的"有没有网卡在用"，断网时它是唯一可信信号。 */
  browserOnline: boolean
  apiReachable: boolean
  basemapReachable: boolean
  terrainReachable: boolean
  /** 同源静态烘焙资产是否就位（离线兜底路径）。 */
  localBasemapAvailable: boolean
}

const SEVERITY: Record<NetworkStatus, number> = { online: 0, degraded: 1, offline: 2 }

export function severityOf(status: NetworkStatus): number {
  return SEVERITY[status]
}

/** 即时分类（无滞回）：给定事实直接给结论，是状态机的唯一判据，单独可测。 */
export function classifyFacts(facts: SourceFacts): NetworkStatus {
  const mapAvailable = facts.basemapReachable || facts.localBasemapAvailable
  if (!facts.browserOnline) return facts.apiReachable || mapAvailable ? 'degraded' : 'offline'
  if (!facts.apiReachable) return mapAvailable ? 'degraded' : 'offline'
  if (!mapAvailable || !facts.terrainReachable) return 'degraded'
  return 'online'
}

// ---------- 状态机（含恢复滞回） ----------

/**
 * 恢复需要的连续一致探针数：弱网下"一次成功"很常见（缓存/瞬时连通），
 * 立刻升档会让状态标签在"降级↔正常"之间反复跳，现场指挥读不到稳定结论。
 * 降档（变差）则**立刻**生效——宁可过早告警，不可漏报。
 */
export const RECOVERY_SAMPLES = 2

export interface OfflineMachine {
  status: NetworkStatus
  /** 连续多少轮探针都指向这个更优状态。 */
  pendingStatus: NetworkStatus | null
  pendingSamples: number
  reason: string
  /** 最近一次进入新状态的时钟读数（由事件携带，不用 Date.now，便于测试确定性）。 */
  at: number | null
}

export type OfflineEvent =
  | { type: 'browser_offline'; at?: number }
  | { type: 'browser_online'; at?: number }
  | { type: 'probe'; facts: SourceFacts; at?: number }

export const FALLBACK_REASONS: Record<NetworkStatus, string> = {
  online: '后端 / 底图 / 地形三路均可用',
  degraded: '至少一路缺位，已按可用源继续出图',
  offline: '实时数据与本地底图都不可用，仅显示文字清单',
}

export function initialMachine(status: NetworkStatus = 'degraded'): OfflineMachine {
  return { status, pendingStatus: null, pendingSamples: 0, reason: FALLBACK_REASONS[status], at: null }
}

/** 接受一个探针结论即视为"回升计数"作废：中途来一次同档/更差档的探针，升档确认必须从头数。 */
function adopt(machine: OfflineMachine, status: NetworkStatus, reason: string, at: number | null): OfflineMachine {
  return { status, pendingStatus: null, pendingSamples: 0, reason, at: at ?? machine.at }
}

/**
 * 纯转移函数：`reduceOffline(machine, event) -> machine`。
 * - `browser_offline` 直接落 `offline`（硬信号，不等探针）；
 * - `browser_online` 只是解除"硬断网"，状态留给下一次探针裁决；
 * - `probe` 按 `classifyFacts` 的结论转移：变差立即降，变好需连续 `RECOVERY_SAMPLES` 次。
 */
export function reduceOffline(machine: OfflineMachine, event: OfflineEvent): OfflineMachine {
  if (event.type === 'browser_offline') {
    return adopt(machine, 'offline', '浏览器报告网络已断开', event.at ?? null)
  }
  if (event.type === 'browser_online') {
    return { ...machine, pendingStatus: null, pendingSamples: 0, reason: '网络恢复，等待探针确认' }
  }

  const target = classifyFacts(event.facts)
  const at = event.at ?? null
  if (severityOf(target) >= severityOf(machine.status)) {
    return adopt(machine, target, reasonFor(target, event.facts), at)
  }
  if (machine.pendingStatus === target) {
    const samples = machine.pendingSamples + 1
    if (samples >= RECOVERY_SAMPLES) {
      return adopt(machine, target, reasonFor(target, event.facts), at)
    }
    return { ...machine, pendingStatus: target, pendingSamples: samples, reason: reasonFor(target, event.facts), at }
  }
  return { ...machine, pendingStatus: target, pendingSamples: 1, reason: reasonFor(target, event.facts), at }
}

function reasonFor(status: NetworkStatus, facts: SourceFacts): string {
  const missing: string[] = []
  if (!facts.apiReachable) missing.push('后端 API')
  if (!facts.basemapReachable && !facts.localBasemapAvailable) missing.push('底图')
  if (!facts.terrainReachable) missing.push('地形')
  if (status === 'offline') return missing.length > 0 ? `不可用：${missing.join(' / ')}` : FALLBACK_REASONS.offline
  if (status === 'degraded') return missing.length > 0 ? `已降级，缺：${missing.join(' / ')}` : FALLBACK_REASONS.degraded
  return FALLBACK_REASONS.online
}

// ---------- 探针 ----------

export const PROBE_TIMEOUT_MS = 4_000

/** 只声明用到的 fetch 形状：生产传 `window.fetch`，单测传假函数。 */
export type ProbeFetch = (
  url: string,
  init?: { method?: string; signal?: AbortSignal },
) => Promise<{ ok: boolean; status: number }>

export interface ProbeTargets {
  api: string
  basemapTemplate: string
  terrain: string
  localBasemap: string
}

/** `{z}/{x}/{y}` 模板 → 具体瓦片 URL（探针取 0/0/0：只关心源在不在，不关心瓦片内容）。 */
export function tileUrlForTemplate(template: string, z = 0, x = 0, y = 0): string {
  return template.replace('{z}', String(z)).replace('{x}', String(x)).replace('{y}', String(y))
}

/**
 * 同源资产判定：只允许以 `/` 开头的相对路径或同 host 的绝对 URL。
 * 这是"只用同源资产"约束在代码里的可测形式：任何指向第三方瓦片/地形/地理编码服务的模板
 * 都会被这一步挡掉，而不是依赖 review 时人眼扫一遍。
 */
export function isLocalAssetUrl(url: string, origin = 'http://localhost'): boolean {
  const trimmed = url.trim()
  if (!trimmed || /\s/.test(trimmed)) return false
  // 协议相对地址（//host/x）的主机由页面上下文决定，不可信，一律拒绝。
  if (trimmed.startsWith('//')) return false
  if (trimmed.startsWith('/')) return true
  let parsed: URL
  let base: URL
  try {
    parsed = new URL(trimmed)
    base = new URL(origin)
  } catch {
    // 无 scheme 的相对路径（terrain/x、./x）不是合法的资产地址：底图/地形模板必须从站点根开始。
    return false
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return false
  return parsed.host === base.host
}

/**
 * 单路探针：HEAD 成功或 405（不少静态服务器不接 HEAD）都算可用；
 * 任何异常/超时/404 都算不可用（探针**永不抛错**——弱网下探测失败是常态而不是事故）。
 */
export async function probeSource(
  fetchLike: ProbeFetch,
  url: string,
  timeoutMs: number = PROBE_TIMEOUT_MS,
): Promise<boolean> {
  if (!url) return false
  const controller = typeof AbortController === 'undefined' ? null : new AbortController()
  const timer = setTimeout(() => controller?.abort(), Math.max(1, timeoutMs))
  try {
    const response = await fetchLike(url, { method: 'HEAD', signal: controller?.signal })
    return response.ok || response.status === 405
  } catch {
    return false
  } finally {
    clearTimeout(timer)
  }
}

/** 四路探针并发出结果：互不依赖，串行会把弱网下的探测延迟叠成 4 倍。 */
export async function collectFacts(deps: {
  fetch: ProbeFetch
  browserOnline: boolean
  targets: ProbeTargets
  timeoutMs?: number
}): Promise<SourceFacts> {
  const timeout = deps.timeoutMs ?? PROBE_TIMEOUT_MS
  const [api, basemap, terrain, localBasemap] = await Promise.all([
    probeSource(deps.fetch, deps.targets.api, timeout),
    probeSource(deps.fetch, tileUrlForTemplate(deps.targets.basemapTemplate), timeout),
    probeSource(deps.fetch, deps.targets.terrain, timeout),
    probeSource(deps.fetch, deps.targets.localBasemap, timeout),
  ])
  return {
    browserOnline: deps.browserOnline,
    apiReachable: api,
    basemapReachable: basemap,
    terrainReachable: terrain,
    localBasemapAvailable: localBasemap,
  }
}
