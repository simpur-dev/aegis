import { reactive, ref } from 'vue'

import type { AssistantFrame, ConfirmResultDto } from '@/api/assistant'

/** 待确认动作在这页只有三种下落：后端回执、本地收起、请求没发出去。 */
export type Decision = { kind: 'busy' } | { kind: 'dismissed' } | { kind: 'error'; message: string } | { kind: 'result'; result: ConfirmResultDto }

/**
 * 对话状态放在组件外面。
 *
 * 这一页的能力条写着"会话保留 30 分钟"（后端给的 TTL），而时间线与会话号原先是组件内的
 * ref：切到别的菜单再回来，整段对话没了、会话号也回到"未开始"，后端那 30 分钟里还留着的
 * 会话（包括没来得及签的待确认动作）从页面上再也接不上。真机量过：`as_8bd49f9a44e2`
 * 一换页就消失，页面一句说明都没有。值班员"去图上看一眼那个沟"是再自然不过的动作。
 */
const frames = ref<AssistantFrame[]>([])
const sessionId = ref<string | null>(null)
const decisions = reactive<Record<string, Decision>>({})

export function useAssistantSession(): {
  frames: typeof frames
  sessionId: typeof sessionId
  decisions: typeof decisions
  clearTimeline: () => void
} {
  return { frames, sessionId, decisions, clearTimeline }
}

/** 清的是这一屏的记录；会话号留着，后端那段会话还在时限内就仍接得上。 */
function clearTimeline(): void {
  frames.value = []
  for (const key of Object.keys(decisions)) delete decisions[key]
}

/** 只给测试用：模块级状态在用例之间会串，每条用例都得从头开始。 */
export function resetAssistantSession(): void {
  clearTimeline()
  sessionId.value = null
}
