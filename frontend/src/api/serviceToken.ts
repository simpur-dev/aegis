/**
 * 服务令牌的前端一侧（§4.8.1）。
 *
 * 为什么需要它：后端 `AEGIS_SERVICE_TOKEN` 一配，`/api/` 之下整个业务面就要求带凭据
 * （中间件 `enforce_service_token` 是唯一执行点）。此前浏览器一处都不带，
 * 于是"开了令牌的部署"里八张页面全部 401 —— 配置项与真实拦截之间断了链。
 *
 * 凭据放哪儿（这是安全口径，不是风格选择）：
 * - **不落构建产物**：`VITE_*` 会被打进静态资源，任何拿到 bundle 的人都读得到这枚 bearer；
 * - 存 `localStorage`，只在当前浏览器、当前来源下有效，换令牌不用重新构建；
 * - 因此这台机器上的任何脚本/扩展也能读到它 —— 这是"无登录态的内网演示部署"接受的代价，
 *   正式部署该走的是真会话（平台目前没有账号体系，那一步不在这节范围内）。
 *
 * 头名用 `X-Aegis-Token`：后端两种写法都认（`Authorization: Bearer` 是给通用 MCP 客户端留的），
 * 但前端固定一种，免得哪天排查看凭据来源时要猜。
 */

import { ref, type Ref } from 'vue'
import type { AxiosInstance } from 'axios'

export const TOKEN_HEADER = 'X-Aegis-Token'

const STORAGE_KEY = 'aegis.serviceToken'

function readStored(): string {
  try {
    return globalThis.localStorage?.getItem(STORAGE_KEY) ?? ''
  } catch {
    // 隐私模式或禁用了存储：当作没配，别让整页起不来
    return ''
  }
}

function persist(value: string): void {
  try {
    if (value === '') globalThis.localStorage?.removeItem(STORAGE_KEY)
    else globalThis.localStorage?.setItem(STORAGE_KEY, value)
  } catch {
    /* 存不下去就只在内存里生效：本次会话仍然可操作，刷新后需要重填（不假装已经保存） */
  }
}

/** 当前令牌（响应式：令牌条与页脚文案跟着它变）。 */
export const serviceToken: Ref<string> = ref(readStored())

/** 最近一次是否有请求被鉴权面拒了（401）。令牌条靠它把"该填了"说出来。 */
export const authRejected: Ref<boolean> = ref(false)

export function setServiceToken(value: string): void {
  const trimmed = value.trim()
  serviceToken.value = trimmed
  persist(trimmed)
  if (trimmed !== '') authRejected.value = false
}

export function clearServiceToken(): void {
  setServiceToken('')
}

/** 给不能挂拦截器的调用点（原生 `fetch`，如助手 SSE、地图离线资产探测）用。 */
export function tokenHeaders(): Record<string, string> {
  return serviceToken.value === '' ? {} : { [TOKEN_HEADER]: serviceToken.value }
}

/**
 * 给一个 axios 实例装上凭据头与 401 记账。
 *
 * 每个 api 模块都自带实例（隔离边界是刻意的），所以这里只提供一个动作：
 * 谁创建实例谁调用它一次。漏一处的那条链在开令牌时就是 401 ——
 * `serviceToken.spec.ts` 从源码逐个数过哪些模块装了。
 */
export function installServiceToken(instance: AxiosInstance): AxiosInstance {
  instance.interceptors.request.use((config) => {
    const token = serviceToken.value
    if (token !== '') config.headers.set(TOKEN_HEADER, token)
    return config
  })
  instance.interceptors.response.use(
    (response) => response,
    (error: unknown) => {
      const status = (error as { response?: { status?: number } })?.response?.status
      if (status === 401) authRejected.value = true
      return Promise.reject(error)
    },
  )
  return instance
}
