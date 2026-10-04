import { reactive, ref, watch } from 'vue'

import type { AssistantFrame, ConfirmResultDto } from '@/api/assistant'

/** 待确认动作在这页只有三种下落：后端回执、本地收起、请求没发出去。 */
export type Decision = { kind: 'busy' } | { kind: 'dismissed' } | { kind: 'error'; message: string } | { kind: 'result'; result: ConfirmResultDto }

/**
 * 对话状态放在组件外面。
 *
 * 这一页的能力条写着"会话保留 30 分钟"（后端给的 TTL），而时间线与会话号原先是组件内的
 * ref：切到别的菜单再回来，整段对话没了、会话号回到"未开始"，后端那 30 分钟里还留着的
 * 会话（包括没来得及签的待确认动作）从页面上再也接不上。真机量过：`as_8bd49f9a44e2`
 * 一换页就消失，页面一句说明都没有。值班员"去图上看一眼那个沟"是再自然不过的动作。
 *
 * 再往下一档：整页刷新（F5）走的是同一套后端会话，所以状态另存一份到 sessionStorage——
 * 标签页还在，对话就该还在；标签页关了，浏览器自然把这块清掉。
 */
const STORAGE_KEY = 'aegis.assistant.session.v1'
/** 只留最近这些帧：一轮对话几帧，留几十轮足够回看，而不必把整段历史堆进浏览器存储。 */
const MAX_PERSISTED_FRAMES = 60

const frames = ref<AssistantFrame[]>([])
const sessionId = ref<string | null>(null)
const decisions = reactive<Record<string, Decision>>({})

function storage(): Storage | null {
  if (typeof window === 'undefined' || typeof window.sessionStorage === 'undefined') return null
  return window.sessionStorage
}

/** 页面装载（或测试里模拟"新开一页"）时把上一次的样子接回来。 */
export function rehydrate(): void {
  const bucket = storage()
  if (bucket === null) return
  let raw: string | null = null
  try {
    raw = bucket.getItem(STORAGE_KEY)
  } catch {
    return
  }
  if (raw === null) return
  try {
    const saved = JSON.parse(raw) as { frames?: AssistantFrame[]; session_id?: string | null; decisions?: Record<string, Decision> }
    frames.value = Array.isArray(saved.frames) ? saved.frames : []
    sessionId.value = typeof saved.session_id === 'string' ? saved.session_id : null
    for (const key of Object.keys(decisions)) delete decisions[key]
    // 「放弃」只在这页记录（后端没有取消接口），所以它必须跟时间线一起活过刷新：
    // 只存时间线不存决定，刷一次页就让人刚拒绝的演练又变回一颗可点的「确认」。
    Object.assign(decisions, saved.decisions ?? {})
  } catch {
    // 存不下或读不出都按"没有上一段"处理：宁可重开一场，也不要拿半截 JSON 画时间线
    frames.value = []
    sessionId.value = null
  }
}

function persist(): void {
  const bucket = storage()
  if (bucket === null) return
  const recent = frames.value.slice(-MAX_PERSISTED_FRAMES)
  try {
    bucket.setItem(STORAGE_KEY, JSON.stringify({ frames: recent, session_id: sessionId.value, decisions: { ...decisions } }))
  } catch {
    // 配额或隐私模式写不进：页面上这段对话照样还在，换页照样接得上，只是刷新接不上
  }
}

rehydrate()
watch([frames, sessionId, decisions], persist, { deep: true })

/** 清的是这一屏的记录；会话号留着，后端那段会话还在时限内就仍接得上。 */
export function clearTimeline(): void {
  frames.value = []
  for (const key of Object.keys(decisions)) delete decisions[key]
}

/** 只给测试用：模块级状态在用例之间会串，每条用例都得从头开始（含浏览器存储那一层）。 */
export function resetAssistantSession(): void {
  clearTimeline()
  sessionId.value = null
  storage()?.removeItem(STORAGE_KEY)
}

/** 只给测试用：模拟整页刷新——内存清空、浏览器存储不动，再走一遍装载时接回来的路。 */
export function reloadAssistantSession(): void {
  frames.value = []
  sessionId.value = null
  rehydrate()
}

export function useAssistantSession(): {
  frames: typeof frames
  sessionId: typeof sessionId
  decisions: typeof decisions
  clearTimeline: () => void
} {
  return { frames, sessionId, decisions, clearTimeline }
}
