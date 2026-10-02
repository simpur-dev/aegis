/**
 * 装配完成后的**只读**场景句柄：URL 带 `?mapDebug` 才挂，默认不挂。
 *
 * 为什么需要它：视觉烟雾门禁（`e2e/map-visual.spec.ts`）要读 `globe.getHeight()` 这类真场景状态，
 * 而 Cesium 不把 Viewer 挂在任何能从 DOM 反查的地方。没有句柄就只能退回"看截图猜有没有出图"，
 * 那恰好是这一轮要避免的东西——地形不出几何时截图也是黑的，但原因可能是资产、可能是取景、也可能
 * 是 GL 上下文丢了，三者需要不同的修法。
 *
 * 为什么用查询串开关而不是无条件挂：给线上留一个全局可达的 viewer 入口没有必要，
 * 而高原现场排障时" URL 加一段就能看场景状态"确实有用。销毁时必须清掉，
 * 否则 SPA 里切走再切回来会读到已经 destroy 的旧 Viewer。
 */

export const MAP_DEBUG_QUERY_FLAG = 'mapDebug'

export const MAP_DEBUG_WINDOW_KEY = '__aegisMap'

export interface MapDebugHandle {
  /** 装配出来的 Cesium Viewer；类型刻意留 unknown，句柄不该把测试绑到某个 Cesium 版本上。 */
  readonly viewer: unknown
}

/** 纯判定：给定 search 串，要不要挂句柄。默认口径与浏览器 `location.search` 一致。 */
export function mapDebugEnabled(search: string): boolean {
  return new URLSearchParams(search).has(MAP_DEBUG_QUERY_FLAG)
}

/**
 * 挂句柄；未开启调试或没有 window 时返回 null（node 环境的单测走这条路，不会炸）。
 * 返回值就是挂上去的那个对象，调用方不必再回读 window。
 */
export function publishMapDebugHandle(viewer: unknown, search: string): MapDebugHandle | null {
  if (typeof window === 'undefined' || !mapDebugEnabled(search)) return null
  const handle: MapDebugHandle = { viewer }
  debugWindow()[MAP_DEBUG_WINDOW_KEY] = handle
  return handle
}

export function clearMapDebugHandle(): void {
  if (typeof window === 'undefined') return
  delete debugWindow()[MAP_DEBUG_WINDOW_KEY]
}

function debugWindow(): Record<string, unknown> {
  return globalThis.window as unknown as Record<string, unknown>
}
