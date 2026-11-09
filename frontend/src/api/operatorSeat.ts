/**
 * 值班席位（声明身份，P0 决策 D2）：`X-Aegis-Operator` 头的前端一侧。
 *
 * 为什么像 `serviceToken` 一样是模块级 ref：席位是"这台浏览器当前是谁在值班"，
 * 换页不丢、页脚与告警卡/助手写动作读同一个来源——两处各存一份就会出现
 * "这边以为自己是值班员，那边以为自己是值班长"。
 *
 * 存 localStorage 而不是登录态：平台没有账号体系（P4 才有），这是声明身份，
 * 不是认证。界面上必须始终让"你是谁"可见，否则写动作的审计就无人认领。
 */

import type { AxiosInstance } from 'axios'
import { ref, type Ref } from 'vue'

export const OPERATOR_HEADER = 'X-Aegis-Operator'

const STORAGE_KEY = 'aegis.operatorSeat'

function readStored(): string {
  try {
    return globalThis.localStorage?.getItem(STORAGE_KEY) ?? ''
  } catch {
    return ''
  }
}

function persist(value: string): void {
  try {
    if (value === '') globalThis.localStorage?.removeItem(STORAGE_KEY)
    else globalThis.localStorage?.setItem(STORAGE_KEY, value)
  } catch {
    /* 存不下去就只在内存里生效（与 serviceToken 同一口径） */
  }
}

/** 当前席位 ID（空串 = 没选席位；写动作会被后端 401 拒绝，界面要提示先选席位）。 */
export const operatorSeat: Ref<string> = ref(readStored())

export function setOperatorSeat(value: string): void {
  const trimmed = value.trim()
  operatorSeat.value = trimmed
  persist(trimmed)
}

export function clearOperatorSeat(): void {
  setOperatorSeat('')
}

/** 给写动作请求装席位头；没选席位时返回空对象，由后端 401 说"没带席位"。 */
export function operatorHeaders(): Record<string, string> {
  return operatorSeat.value === '' ? {} : { [OPERATOR_HEADER]: operatorSeat.value }
}

/**
 * 给 axios 实例装席位头拦截器（与 `installServiceToken` 同一形态）。
 *
 * 在请求时读当前席位、不在创建时抄一份：席位是运行期可变的，抄进默认头会让
 * "刚换了席位、请求还带着旧头"变成一条只有后端审计才看得见的错账。
 */
export function installOperatorHeader(instance: AxiosInstance): void {
  instance.interceptors.request.use((config) => {
    const value = operatorSeat.value
    if (value !== '') config.headers.set(OPERATOR_HEADER, value)
    return config
  })
}
