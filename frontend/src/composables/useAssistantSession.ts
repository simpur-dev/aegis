import { reactive, ref, watch } from 'vue'

import type { AssistantFrame, ConfirmResultDto } from '@/api/assistant'
import { assistantApi, isAbortError, isAssistantDisabled, isAssistantUnavailable, preflightChat } from '@/api/assistant'

/** 待确认动作只有三种下落：后端回执、本地收起、请求没发出去。 */
type Decision =
  | { kind: 'busy'; operation: 'confirm' | 'cancel' }
  | { kind: 'cancelled' }
  /** 兼容旧版浏览器已收起、后端仍未取消的提案。 */
  | { kind: 'dismissed' }
  | { kind: 'error'; message: string }
  | { kind: 'result'; result: ConfirmResultDto }
  /** 双人复核的第一票：动作还没执行，这张提案卡仍要让另一名席位再点一次确认。 */
  | { kind: 'review'; result: ConfirmResultDto }

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
/**
 * 每一轮用户说的话（后端的帧里没有"用户问了什么"这回事，气泡只能自己记）。
 * 与轮次**从尾部对齐**：两边都只在尾部追加、只在头部按同一条上限截断，
 * 所以"第 i 轮 ↔ sentTexts 倒数第 i 条"在截断后依然成立；对不上的老轮次就空着，不冒充。
 * 只在流式落地第一帧时才记（视图侧调 `recordSent`）——一发出去就记的话，
 * 一次零帧的失败请求会让这句话冒充一轮、把对齐全盘错位。
 */
const sentTexts = ref<string[]>([])
const sessionId = ref<string | null>(null)
const decisions = reactive<Record<string, Decision>>({})
const draft = ref('')
const reporter = ref('值班员')
const regionCode = ref('')
const streaming = ref(false)
const streamError = ref('')
let inflight: AbortController | null = null
let pendingSend: { signature: string; key: string } | null = null

function newIdempotencyKey(): string {
  const uuid = globalThis.crypto?.randomUUID?.()
  return uuid ? `chat_${uuid.replace(/-/g, '').slice(0, 24)}`
    : `chat_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`
}

function stopStream(): void {
  inflight?.abort()
}

async function send(): Promise<void> {
  if (streaming.value) return
  const text = draft.value.trim()
  const request = {
    message: text,
    session_id: sessionId.value ?? undefined,
    reporter: reporter.value.trim() || undefined,
    region_code: regionCode.value.trim() || undefined,
  }
  const blocked = preflightChat(request)
  if (blocked) {
    streamError.value = blocked
    return
  }
  const signature = JSON.stringify([text, request.reporter, request.region_code])
  if (pendingSend?.signature !== signature) pendingSend = { signature, key: newIdempotencyKey() }
  streaming.value = true
  streamError.value = ''
  const controller = new AbortController()
  inflight = controller
  try {
    let recorded = false
    let versioned = false
    let ended = false
    let busy = false
    for await (const frame of assistantApi.chat({ ...request, idempotency_key: pendingSend.key }, { signal: controller.signal })) {
      if (inflight !== controller) return
      if (frame.type === 'meta') {
        sessionId.value = frame.session_id
        versioned = frame.stream_version === 1
      }
      if (frame.type === 'done') ended = true
      if (frame.type === 'error' && frame.code === 'E_ASSISTANT_BUSY') busy = true
      frames.value.push(frame)
      if (!recorded) {
        recordSent(text)
        recorded = true
      }
    }
    if (controller.signal.aborted) {
      streamError.value = '已停止接收这一轮。已提交的操作请以动作台账为准。'
      return
    }
    if (!recorded || (versioned && !ended)) {
      streamError.value = '回复中断，草稿已保留。可直接重新发送。'
      return
    }
    if (busy) return
    if (draft.value.trim() === text) draft.value = ''
    pendingSend = null
  } catch (error) {
    if (inflight !== controller) return
    if (isAbortError(error)) streamError.value = '已停止接收这一轮。已提交的操作请以动作台账为准。'
    else if (isAssistantUnavailable(error)) streamError.value = `语义交互服务未装配：${error instanceof Error ? error.message : String(error)}`
    else if (isAssistantDisabled(error)) streamError.value = '助手出口未启用，这一轮没有发出。'
    else streamError.value = error instanceof Error ? `对话中断：${error.message}` : '对话中断'
  } finally {
    if (inflight === controller) {
      streaming.value = false
      inflight = null
    }
  }
}

function storage(): Storage | null {
  if (typeof window === 'undefined' || typeof window.sessionStorage === 'undefined') return null
  return window.sessionStorage
}

/** 页面装载（或测试里模拟"新开一页"）时把上一次的样子接回来。 */
function rehydrate(): void {
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
    const saved = JSON.parse(raw) as {
      frames?: AssistantFrame[]
      session_id?: string | null
      decisions?: Record<string, Decision>
      sent_texts?: unknown
    }
    frames.value = Array.isArray(saved.frames) ? saved.frames : []
    sessionId.value = typeof saved.session_id === 'string' ? saved.session_id : null
    sentTexts.value = Array.isArray(saved.sent_texts) ? saved.sent_texts.filter((item): item is string => typeof item === 'string') : []
    for (const key of Object.keys(decisions)) delete decisions[key]
    // 「放弃」只在这页记录（后端没有取消接口），所以它必须跟时间线一起活过刷新：
    // 只存时间线不存决定，刷一次页就让人刚拒绝的演练又变回一颗可点的「确认」。
    for (const [key, decision] of Object.entries(saved.decisions ?? {})) {
      if (decision?.kind !== 'busy') decisions[key] = decision
    }
  } catch {
    // 存不下或读不出都按"没有上一段"处理：宁可重开一场，也不要拿半截 JSON 画时间线
    frames.value = []
    sessionId.value = null
    sentTexts.value = []
  }
}

function persist(): void {
  const bucket = storage()
  if (bucket === null) return
  const recent = frames.value.slice(-MAX_PERSISTED_FRAMES)
  try {
    bucket.setItem(
      STORAGE_KEY,
      JSON.stringify({
        frames: recent, session_id: sessionId.value,
        decisions: Object.fromEntries(recent.flatMap((frame) => frame.type === 'proposal' && decisions[frame.action_id]
          ? [[frame.action_id, decisions[frame.action_id]]] : [])),
        sent_texts: sentTexts.value.slice(-MAX_PERSISTED_FRAMES),
      }),
    )
  } catch {
    // 配额或隐私模式写不进：页面上这段对话照样还在，换页照样接得上，只是刷新接不上
  }
}

rehydrate()
watch([frames, sessionId, decisions, sentTexts], persist, { deep: true })

/** 清的是这一屏的记录；会话号留着，后端那段会话还在时限内就仍接得上。 */
export function clearTimeline(): void {
  if (streaming.value) return
  frames.value = []
  sentTexts.value = []
  for (const key of Object.keys(decisions)) delete decisions[key]
}

/** 一轮对话的第一帧落地时由视图调用：把用户那句话跟这轮绑在一起。 */
export function recordSent(text: string): void {
  sentTexts.value.push(text)
}

/** 只给测试用：模块级状态在用例之间会串，每条用例都得从头开始（含浏览器存储那一层）。 */
export function resetAssistantSession(): void {
  stopStream()
  inflight = null
  streaming.value = false
  streamError.value = ''
  pendingSend = null
  draft.value = ''
  reporter.value = '值班员'
  regionCode.value = ''
  clearTimeline()
  sessionId.value = null
  storage()?.removeItem(STORAGE_KEY)
}

/** 只给测试用：模拟整页刷新——内存清空、浏览器存储不动，再走一遍装载时接回来的路。 */
export function reloadAssistantSession(): void {
  frames.value = []
  sessionId.value = null
  sentTexts.value = []
  rehydrate()
}

export function useAssistantSession() {
  return { frames, sentTexts, sessionId, decisions, draft, reporter, regionCode, streaming, streamError, clearTimeline, send, stopStream }
}
