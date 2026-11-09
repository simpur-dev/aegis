/**
 * 能力面状态：动作清单、LLM 是否配置、当前席位能做什么。
 *
 * 为什么放在组件外：页面（`/assistant`）与全站抽屉渲染的是同一个助手，两者都要显示
 * "语义服务未配置 / 读不到能力面"。各拉一次会得到两份状态——一处显示正常、另一处显示读不到，
 * 而它们说的是同一条腿。这里也只发一次请求。
 */

import { computed, ref, watch } from 'vue'

import type { CapabilitiesDto } from '@/api/assistant'
import { assistantApi, isAssistantDisabled, isAssistantUnavailable } from '@/api/assistant'
import { operatorSeat } from '@/api/operatorSeat'

export type CapabilitiesKind = 'none' | 'unavailable' | 'disabled' | 'other'

const capabilities = ref<CapabilitiesDto | null>(null)
const error = ref('')
const kind = ref<CapabilitiesKind>('none')
const loading = ref(false)

const llmConfigured = computed(() => capabilities.value?.llm_configured === true)

async function load(): Promise<void> {
  loading.value = true
  try {
    capabilities.value = await assistantApi.capabilities()
    error.value = ''
    kind.value = 'none'
  } catch (cause) {
    capabilities.value = null
    kind.value = isAssistantUnavailable(cause) ? 'unavailable' : isAssistantDisabled(cause) ? 'disabled' : 'other'
    error.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    loading.value = false
  }
}

// 能力面按席位判定：换席位不重载，页面就会拿旧角色的 allowed 去灰按钮，
// 与后端实际权限对不上（席位头本身已随请求实时发送，这里只补"读回来的那份清单"）。
watch(operatorSeat, () => {
  void load()
})

/** 只给测试用：模块级状态在用例之间会串。 */
export function resetAssistantCapabilities(): void {
  capabilities.value = null
  error.value = ''
  kind.value = 'none'
  loading.value = false
}

export function useAssistantCapabilities(): {
  capabilities: typeof capabilities
  error: typeof error
  kind: typeof kind
  loading: typeof loading
  llmConfigured: typeof llmConfigured
  load: () => Promise<void>
} {
  return { capabilities, error, kind, loading, llmConfigured, load }
}
