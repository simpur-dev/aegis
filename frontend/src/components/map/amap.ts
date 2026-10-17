/**
 * 高德 **JS API** 的运行时加载与配置。
 *
 * 为什么自己注入 `<script>` 而不加 `@amap/amap-jsapi-loader` 依赖：那个包只做同一件事
 * （拼 URL、挂 script、等 `window.AMap`），多一个依赖就多一份供应链与离线打包的账。
 *
 * key 走 `import.meta.env.VITE_AMAP_KEY`，值放在**不入库**的 `frontend/.env.local`
 * （根 `.gitignore` 收了 `.env` 与 `.env.*`，只放行 `.env.example`）。
 * 没配 key 时这里返回 null，调用方必须显式退回离线档——不能静默去蹭一个不带 key 的端点：
 * 那条路（`webrd0x.is.autonavi.com/appmaptile`）不要 key、不走配额与署名要求，
 * 不属于"已获授权接入高德"，实测接过一次，已撤。
 */

/** JS API 2.0 的最小面：只声明本项目真正碰到的那几样，不给整个 AMap 命名空间造轮子。 */
export interface AMapNamespace {
  Map: new (container: HTMLElement, options: Record<string, unknown>) => AMapMapInstance
  Marker: new (options: Record<string, unknown>) => AMapOverlay
  CircleMarker: new (options: Record<string, unknown>) => AMapOverlay
  Polygon: new (options: Record<string, unknown>) => AMapOverlay
  LabelMarker?: new (options: Record<string, unknown>) => AMapOverlay
  event: { on: (target: unknown, name: string, handler: (event?: unknown) => void) => void }
}

export interface AMapMapInstance {
  add(overlays: AMapOverlay | AMapOverlay[]): void
  remove(overlays: AMapOverlay | AMapOverlay[]): void
  setZoomAndCenter(zoom: number, center: [number, number], immediately?: boolean): void
  /** 换官方样式档（amap://styles/*）：主题翻转时由组件推进，档位来自 `@/theme/map` 的派生层。 */
  setMapStyle(name: string): void
  destroy(): void
}

export interface AMapOverlay {
  on(event: string, handler: (e?: unknown) => void): void
}

export const AMAP_SCRIPT_ID = 'aegis-amap-jsapi'

/** 高德要求的域名级安全密钥：账号开了校验就必须带，否则地图初始化会报 INVALID_USER_SCODE。 */
export function amapSecurityCode(): string {
  return (import.meta.env.VITE_AMAP_SECURITY_CODE as string | undefined)?.trim() ?? ''
}

export function amapKey(): string {
  return (import.meta.env.VITE_AMAP_KEY as string | undefined)?.trim() ?? ''
}

/** 没配 key 就别谈"接入"：调用方据此把底图档退回离线，而不是加载出一个空白地图。 */
export function amapConfigured(): boolean {
  return amapKey().length > 0
}

let loading: Promise<AMapNamespace> | null = null

/**
 * 加载一次，重复调用复用同一个 Promise（切底图档来回点不该重复注入 script）。
 * 失败要清掉缓存，否则一次网络抖动会把之后所有尝试永久锁死在 rejected 上。
 */
export function loadAmap(timeoutMs = 15_000): Promise<AMapNamespace> {
  if (loading) return loading
  const key = amapKey()
  if (!key) return Promise.reject(new Error('未配置 VITE_AMAP_KEY：高德 JS API 不可用'))
  loading = new Promise<AMapNamespace>((resolve, reject) => {
    const existing = window.AMap
    if (existing) {
      resolve(existing)
      return
    }
    const security = amapSecurityCode()
    if (security) window._AMapSecurityConfig = { securityJsCode: security }
    const script = document.createElement('script')
    script.id = AMAP_SCRIPT_ID
    script.src = `https://webapi.amap.com/maps?v=2.0&key=${encodeURIComponent(key)}`
    script.async = true
    const timer = setTimeout(() => {
      cleanup()
      reject(new Error(`高德 JS API 加载超时（${timeoutMs / 1000}s）：检查网络与 key 的域名白名单`))
    }, timeoutMs)
    function cleanup(): void {
      clearTimeout(timer)
      script.removeEventListener('load', onLoad)
      script.removeEventListener('error', onError)
    }
    function onLoad(): void {
      cleanup()
      if (window.AMap) resolve(window.AMap)
      else {
        script.remove()
        reject(new Error('高德 JS API 脚本已加载但 window.AMap 不存在（key 无效或未配安全密钥）'))
      }
    }
    function onError(): void {
      cleanup()
      script.remove()
      reject(new Error('高德 JS API 脚本加载失败（网络不可达或 key 被拒）'))
    }
    script.addEventListener('load', onLoad)
    script.addEventListener('error', onError)
    document.head.appendChild(script)
  }).catch((error: unknown) => {
    loading = null
    throw error
  })
  return loading
}

declare global {
  interface Window {
    AMap?: AMapNamespace
    _AMapSecurityConfig?: { securityJsCode: string }
  }
}
