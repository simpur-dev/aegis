<script setup lang="ts">
/**
 * AssistantPanel —— 唯一的会话实现。页面（`/assistant`）与全站抽屉都渲染它，
 * 只是容器不同（`mode="page" | "dock"`）。
 *
 * 语义约束一条没动，都来自后端事实，不是审美：
 * 1. **帧序列是过程事实**。后端刻意不做 token 级伪流式，给出的每一帧都是"哪一步在等什么"：
 *    meta/intent/status/result/done 一帧不少，但收进「过程 ｜ N 帧」一个折行（原生 details）；
 *    流式进行中在输入区上方报最新帧名，过程仍然可见。
 * 2. **执行类动作只能经 `/confirm` 落地**。「放弃」只把卡片收起来——后端没有取消接口，
 *    页面就照实说"仅在本地隐藏"，不许把"我没点"显示成"已经撤销"。
 * 3. **能力面读不到 ≠ 一切正常**。503（腿没装配）与 404（出口未启用）是两句话；
 *    `llm_configured=false` 的口径由页头 caps-state 标签承担（黄色横幅已按要求移除）。
 * 4. **角色不够的动作照样列出来**，置灰并说明缺什么——把动作藏起来，用户只会以为助手不会这个。
 * 5. 独立桌面页的告警和处置区同时可见，保留面板高度调节。
 *    抽屉和窄屏独立页通过工具栏切换这两个区，输入区始终在会话底部。
 */
import { message } from 'ant-design-vue'
import {
  ApartmentOutlined, BellOutlined, DownOutlined, LockOutlined, PlayCircleOutlined, RightOutlined,
  SearchOutlined, SendOutlined, ToolOutlined, UploadOutlined,
} from '@ant-design/icons-vue'
import { useMediaQuery } from '@vueuse/core'
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, useId, watch } from 'vue'
import type { Component, ComponentPublicInstance } from 'vue'

import type { ActionSpecDto, ConfirmResultDto, ProposalFrame } from '@/api/assistant'
import { assistantApi, CHAT_LIMITS, isAbortError, isAssistantDisabled, isAssistantUnavailable, preflightChat } from '@/api/assistant'
import ReportForm from '@/components/reports/ReportForm.vue'
import AlertStack from '@/components/assistant/AlertStack.vue'
import HandlingBar from '@/components/assistant/HandlingBar.vue'
import { useAssistantCapabilities } from '@/composables/useAssistantCapabilities'
import { useAssistantSession } from '@/composables/useAssistantSession'
import { useAlerts } from '@/composables/useAlerts'
import { useConversationModel } from '@/composables/useConversationModel'
import { useHandlingSpace } from '@/composables/useHandlingSpace'
import { frameText, proposalText } from '@/utils/frameText'

const props = withDefaults(defineProps<{ mode?: 'page' | 'dock' }>(), { mode: 'page' })
const narrowViewport = useMediaQuery('(max-width: 720px)')
const compact = computed(() => props.mode === 'dock' || narrowViewport.value)
const fieldId = useId()

const {
  capabilities,
  error: capabilitiesError,
  kind: capabilitiesKind,
  load: loadCapabilities,
} = useAssistantCapabilities()

// 时间线与会话号在组件外面：换菜单、换容器（页面 ↔ 抽屉）都还得接得上
const { frames, sentTexts, sessionId, decisions, clearTimeline, recordSent } = useAssistantSession()
const { turns, lastFrame, mainFrames, quietFrames, collapseQuiet } = useConversationModel(frames, sentTexts)

const reporter = ref('值班员')
const regionCode = ref('')
const draft = ref('')
const streaming = ref(false)
const streamError = ref('')
const reportOpen = ref(false)
const composerBox = ref<HTMLElement | null>(null)

// 抽屉「会话优先」：告警/处置收进两枚图标，点开才占空间，一次只开一块
const dockPane = ref<'alerts' | 'handling' | null>(null)
const { openCards, refresh: refreshAlerts } = useAlerts()
const { detail: handlingDetail, refresh: refreshHandling } = useHandlingSpace()
const dockAlertCount = computed(() => openCards.value.length)
const dockHandlingCount = computed(() => handlingDetail.value?.tasks_total ?? 0)
function toggleDockPane(pane: 'alerts' | 'handling'): void {
  dockPane.value = dockPane.value === pane ? null : pane
}
/** Esc 先收展开层、再按才关抽屉：capture 阶段拦在 AssistantDock 的 bubble 监听之前。 */
function onDockEscape(ev: KeyboardEvent): void {
  if (!compact.value || dockPane.value === null || ev.key !== 'Escape') return
  ev.stopPropagation()
  dockPane.value = null
}
onMounted(() => {
  window.addEventListener('keydown', onDockEscape, true)
  // 面板随抽屉首开才挂载；挂载即对账一次，徽标在展开前就是真数
  if (props.mode === 'dock') {
    refreshAlerts()
    refreshHandling()
  }
})
onBeforeUnmount(() => window.removeEventListener('keydown', onDockEscape, true))

/** 聊天面滚到队尾：值班员永远看最新一帧（测试环境宿主可能没挂，守一下）。 */
const scrollHost = ref<HTMLElement | null>(null)
watch([() => frames.value.length, streaming], async () => {
  await nextTick()
  const el = scrollHost.value
  if (el) el.scrollTop = el.scrollHeight
})

/** 点建议条只填句子、不代发：发不发仍是值班员的决定，这里任何时候都不替人按下发送。 */
function useExample(spec: ActionSpecDto): void {
  if (spec.example === '') return
  draft.value = spec.example
  composerBox.value?.querySelector('textarea')?.focus({ preventScroll: true })
}

const catalogId = useId()
const showCatalog = ref(false)

const QUICK_TASKS = [
  { action: 'warning.list', title: '查询预警', detail: '查看近期风险预警', icon: SearchOutlined },
  { action: 'chain.explain', title: '解释链路', detail: '回看研判与处置', icon: ApartmentOutlined },
  { action: 'drill.start', title: '发起演练', detail: '推演应急处置流程', icon: PlayCircleOutlined },
  { action: 'report.submit', title: '上报险情', detail: '记录现场异常与风险', icon: UploadOutlined },
] as const
const DOMAIN_LABELS = {
  query: '查询与解读', drill: '演练与上报', warning: '预警处置', task: '处置任务',
  notify: '通知', workflow: '流程', rule: '规则', device: '设备',
}
const ROLE_LABELS: Record<string, string> = { duty: '值班员', lead: '值班长', admin: '管理员' }

const quickTasks = computed(() => QUICK_TASKS.flatMap((task) => {
  const spec = capabilities.value?.actions.find((action) => action.action === task.action)
  return spec ? [{ ...task, spec }] : []
}))

const catalogGroups = computed(() => {
  const featured = new Set<string>(quickTasks.value.map((task) => task.action))
  const remaining = capabilities.value?.actions.filter((action) => !featured.has(action.action)) ?? []
  const groups = Object.entries(DOMAIN_LABELS).map(([domain, label]) => ({
    domain, label, items: remaining.filter((action) => action.domain === domain),
  }))
  groups.push({
    domain: 'other',
    label: '其他',
    items: remaining.filter((action) => !Object.hasOwn(DOMAIN_LABELS, action.domain)),
  })
  return groups.filter((group) => group.items.length)
})

function roleNote(spec: ActionSpecDto): string {
  return ROLE_LABELS[spec.required_role] ?? spec.required_role
}

function actionTitle(spec: ActionSpecDto): string {
  const hint = spec.example ? `填入：${spec.example}` : '该动作没有示例句'
  return spec.allowed ? hint : `${hint} ｜ ${spec.denied_reason || '当前席位无权限'}`
}

/**
 * 幂等键跟着"这一句草稿"走，不是每次发送新造：
 * 断线重发或双击时同一句会命中同一行动作台账，不会多出一张待确认卡片。
 * 成功收尾才清掉；失败保留，让重试仍然是"同一次意图"。
 */
let pendingSend: { text: string; key: string } | null = null

function newIdempotencyKey(): string {
  const uuid = globalThis.crypto?.randomUUID?.()
  if (uuid) return `chat_${uuid.replace(/-/g, '').slice(0, 24)}`
  return `chat_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`
}

function idempotencyKeyFor(text: string): string {
  if (pendingSend === null || pendingSend.text !== text) pendingSend = { text, key: newIdempotencyKey() }
  return pendingSend.key
}

/** 当前这一轮的可中断句柄：停止按钮只掐读取端，已经发生的动作不由它决定。 */
let inflight: AbortController | null = null

function stopStream(): void {
  if (!streaming.value) return
  inflight?.abort()
}

async function send(): Promise<void> {
  const text = draft.value.trim()
  const request = {
    message: text,
    session_id: sessionId.value ?? undefined,
    reporter: reporter.value.trim() || undefined,
    region_code: regionCode.value.trim() || undefined,
    idempotency_key: idempotencyKeyFor(text),
  }
  // 发出前按后端口径挡一次（数值与本文件的正则由跨端门禁对着 assistant_api.py 校验）
  const blocked = preflightChat(request)
  if (blocked !== '') {
    streamError.value = blocked
    return
  }
  streaming.value = true
  streamError.value = ''
  const controller = new AbortController()
  inflight = controller
  try {
    // 用户那句话要跟这一轮绑在一起记：等到第一帧落地才记——
    // 一发出去就记的话，零帧的失败请求会让这句话冒充一轮，把尾对齐全盘错位。
    let recorded = false
    for await (const frame of assistantApi.chat(request, { signal: controller.signal })) {
      if (frame.type === 'meta') sessionId.value = frame.session_id
      frames.value.push(frame)
      if (!recorded) {
        recordSent(text)
        recorded = true
      }
    }
    // 流式期间输入框可能已经换上新草稿：只在还是这一句时清空，别吞掉刚打的字
    if (draft.value.trim() === text) draft.value = ''
    pendingSend = null
  } catch (error) {
    if (isAbortError(error)) {
      streamError.value = '已停止接收这一轮：已经发生的动作仍以对话记录与动作台账为准，不会因为停止而撤回。'
    } else if (isAssistantUnavailable(error)) streamError.value = `语义交互服务未装配：${error instanceof Error ? error.message : String(error)}`
    else if (isAssistantDisabled(error)) streamError.value = '助手出口未启用（路由整段没挂载）：这一轮请求没有发出，页面上没有新事实。'
    else streamError.value = error instanceof Error ? `对话中断：${error.message}` : '对话中断'
  } finally {
    streaming.value = false
    if (inflight === controller) inflight = null
  }
}

/**
 * Enter 发送、Shift+Enter 换行——多行输入框的通用约定。
 * 流式期间按钮点不动（antd 的 loading 让它接不到点击），键盘这条路必须自己守住：
 * 否则两轮回答会织进同一条时间线，会话号也说不清是哪一轮的。
 */
function onMessageKeydown(event: KeyboardEvent): void {
  // 输入法合成中（含选词的 Enter）不是发送意图；这里一拦，拼音和候选就没了。
  // keyCode 229 是旧 Safari 在合成期给的同一个信号。
  if (event.isComposing || event.keyCode === 229) return
  // 只有 Enter 才是"发送"：以前这里漏了键名判断，打字/退格都会被 preventDefault 并触发一轮发送
  if (event.key !== 'Enter' || event.shiftKey) return
  event.preventDefault()
  if (streaming.value) {
    streamError.value = '上一轮还在进行中，等它回来再发下一条（要先换行用 Shift+Enter）。'
    return
  }
  void send()
}

async function confirmProposal(proposal: ProposalFrame): Promise<void> {
  decisions[proposal.action_id] = { kind: 'busy' }
  try {
    const result = await assistantApi.confirm(proposal.action_id, { actor: reporter.value.trim() || undefined })
    if (result.status === 'awaiting_review') {
      // 第一票只记票、不执行：卡片不能锁上，另一名席位还要能点"确认"。
      decisions[proposal.action_id] = { kind: 'review', result }
      message.info(result.reason ?? '等待另一名席位复核')
      return
    }
    decisions[proposal.action_id] = { kind: 'result', result }
    if (result.status !== 'executed') {
      message.warning(`确认结果：${CONFIRM_STATUS_LABELS[result.status] ?? result.status}`)
    }
  } catch (error) {
    decisions[proposal.action_id] = {
      kind: 'error',
      message: error instanceof Error ? error.message : String(error),
    }
  }
}

/** 只在本地收起卡片：后端没有取消接口，说清这一点比"看起来撤销了"重要。 */
function dismissProposal(proposal: ProposalFrame): void {
  decisions[proposal.action_id] = { kind: 'dismissed' }
}

/** 后端 confirm() 的四条返回分支：一分支一句话，不许合并。 */
const CONFIRM_STATUS_LABELS: Record<ConfirmResultDto['status'], string> = {
  executed: '已执行',
  rejected: '已拒绝',
  expired: '已过期',
  failed: '执行失败',
  awaiting_review: '待第二人复核',
}

/** 复核第一票不是终态：确认/放弃两个按钮还要留着给另一名席位。 */
function decisionFinal(actionId: string): boolean {
  const decision = decisions[actionId]
  return !!decision && decision.kind !== 'review'
}

/** 执行类动作的闭环：把能跟进的号带回来，别让值班员手里一个号都没有。 */
function executionHandles(result: ConfirmResultDto): string {
  const raw = JSON.stringify(result.result ?? {})
  const found: string[] = []
  const patterns: ReadonlyArray<readonly [string, RegExp]> = [
    ['链路', /trc_[0-9a-f]{6,}/],
    ['预警', /wrn_[0-9a-f]{6,}/],
    ['工单', /wfi_[0-9a-f]{6,}/],
  ]
  for (const [label, pattern] of patterns) {
    const hit = raw.match(pattern)
    if (hit !== null) found.push(`${label} ${hit[0]}`)
  }
  return found.length > 0 ? `｜ ${found.join(' ｜ ')}` : '｜ 后端没回可跟进的号，请到"预警发布"或"流程编排"页核对是否真的落地'
}

function decisionText(actionId: string): string {
  const decision = decisions[actionId]
  if (!decision) return ''
  if (decision.kind === 'busy') return '确认请求进行中…'
  if (decision.kind === 'dismissed') return '仅本地放弃：后端没有取消接口，该动作仍在会话内，确认或过期才会失效。'
  if (decision.kind === 'error') return `确认请求失败：${decision.message}`
  if (decision.kind === 'review') return `复核 ${decision.result.reviews?.length ?? 1}/${decision.result.review_required ?? 2}：还需另一名席位确认（不能是同一人）`
  const result = decision.result
  const label = CONFIRM_STATUS_LABELS[result.status] ?? result.status
  if (result.status === 'executed') return `${label} ${result.action ?? ''}（后端回执 ${result.status}）${executionHandles(result)}`.trim()
  return `${label}：${result.reason ?? result.error ?? '后端未给出原因'}`
}

/**
 * 右栏竖排拉杆：满栏里两块面板高度互补——拖一块的另一块让位，
 * 否则默认各半时栏已填满，拉杆第一步就拖不动（上一轮横排实测过的死局）。
 * 拖动习语与驾驶舱同一份：pointer capture + rAF 合帧直写 DOM，pointerup 才提交记忆，
 * 手势期间不改响应式状态，免掉逐发回流。
 */
type PaneKind = 'alerts' | 'handling'
const PANE_MIN = 160
const PANE_GAP = 16
const PANE_STORAGE: Record<PaneKind, string> = {
  alerts: 'aegis.assistant.alerts-pane',
  handling: 'aegis.assistant.handling-pane',
}
const PANES: ReadonlyArray<{ kind: PaneKind; label: string; component: Component }> = [
  { kind: 'alerts', label: '在案告警', component: AlertStack },
  { kind: 'handling', label: '处置空间', component: HandlingBar },
]

function readPaneHeight(kind: PaneKind): number | null {
  try {
    const raw = localStorage.getItem(PANE_STORAGE[kind])
    if (raw === null) return null
    const value = Number(raw)
    return Number.isFinite(value) && value >= PANE_MIN ? Math.round(value) : null
  } catch {
    return null
  }
}

const sideColRef = ref<HTMLElement | null>(null)
const alertsPaneRef = ref<HTMLElement | null>(null)
const handlingPaneRef = ref<HTMLElement | null>(null)
const paneHeights = reactive<Record<PaneKind, number | null>>({
  alerts: readPaneHeight('alerts'),
  handling: readPaneHeight('handling'),
})
/** 右栏全高预算：窗口变化时跟着量，拉杆夹在 [160, 栏高−缝−160] 内。 */
const sideBudget = ref(0)
let budgetObserver: ResizeObserver | null = null

function setPaneRef(kind: PaneKind, el: Element | ComponentPublicInstance | null): void {
  const html = el instanceof HTMLElement ? el : null
  if (kind === 'alerts') alertsPaneRef.value = html
  else handlingPaneRef.value = html
}

function paneEl(kind: PaneKind): HTMLElement | null {
  return kind === 'alerts' ? alertsPaneRef.value : handlingPaneRef.value
}
function otherKind(kind: PaneKind): PaneKind {
  return kind === 'alerts' ? 'handling' : 'alerts'
}
function columnHeight(): number {
  return sideBudget.value || sideColRef.value?.clientHeight || 0
}
function paneDefault(): number {
  const col = columnHeight()
  return col > 0 ? Math.max(PANE_MIN, Math.round((col - PANE_GAP) / 2)) : 0
}
function paneMax(): number {
  return Math.max(PANE_MIN, columnHeight() - PANE_GAP - PANE_MIN)
}
function clampPane(height: number): number {
  return Math.min(paneMax(), Math.max(PANE_MIN, Math.round(height)))
}
function rememberPane(kind: PaneKind, height: number | null): void {
  paneHeights[kind] = height
  try {
    if (height === null) localStorage.removeItem(PANE_STORAGE[kind])
    else localStorage.setItem(PANE_STORAGE[kind], String(height))
  } catch {
    /* 存储不可用时只记这一屏 */
  }
}
/** 互补落地：一块定高，另一块拿剩下的；两块一起记，刷新后仍是同一道缝。 */
function commitPair(kind: PaneKind, height: number): void {
  const rest = Math.max(PANE_MIN, columnHeight() - PANE_GAP - height)
  rememberPane(kind, height)
  rememberPane(otherKind(kind), rest)
  paintPair(kind, height, rest)
}
/** 记忆可能来自更高的屏：栏高量出来后按当前预算重新夹一次，否则右栏会溢出视口。 */
function reconcilePanes(): void {
  const col = columnHeight()
  if (col <= 0) return
  const { alerts, handling } = paneHeights
  if (alerts === null && handling === null) return
  const anchorKind: PaneKind = alerts !== null ? 'alerts' : 'handling'
  const anchor = clampPane((alerts ?? handling) as number)
  const rest = Math.max(PANE_MIN, col - PANE_GAP - anchor)
  if (paneHeights[anchorKind] === anchor && paneHeights[otherKind(anchorKind)] === rest) return
  commitPair(anchorKind, anchor)
}
function paintPair(kind: PaneKind, height: number, rest: number): void {
  for (const [k, h] of [[kind, height], [otherKind(kind), rest]] as ReadonlyArray<readonly [PaneKind, number]>) {
    const el = paneEl(k)
    if (el === null) continue
    el.style.height = `${h}px`
    el.querySelector('.side-pane__handle')?.setAttribute('aria-valuenow', String(h))
  }
}

let dragKind: PaneKind | null = null
let dragStartY = 0
let dragStartH = 0
let dragLastY = 0
let dragRaf = 0

function writePaneHeight(): void {
  dragRaf = 0
  if (dragKind === null) return
  const height = clampPane(dragStartH + (dragLastY - dragStartY))
  paintPair(dragKind, height, Math.max(PANE_MIN, columnHeight() - PANE_GAP - height))
}

function onPaneHandleDown(event: PointerEvent, kind: PaneKind): void {
  if (dragKind !== null) return
  const el = paneEl(kind)
  if (el === null) return
  dragKind = kind
  dragStartY = dragLastY = event.clientY
  dragStartH = paneHeights[kind] ?? (el.getBoundingClientRect().height || paneDefault())
  ;(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId)
  el.classList.add('is-resizing')
}

function onPaneHandleMove(event: PointerEvent): void {
  if (dragKind === null) return
  dragLastY = event.clientY
  if (dragRaf === 0) dragRaf = requestAnimationFrame(writePaneHeight)
}

function onPaneHandleUp(event: PointerEvent): void {
  if (dragKind === null) return
  const kind = dragKind
  dragKind = null
  paneEl(kind)?.classList.remove('is-resizing')
  if (dragRaf !== 0) {
    cancelAnimationFrame(dragRaf)
    dragRaf = 0
  }
  if (dragLastY !== dragStartY) commitPair(kind, clampPane(dragStartH + (dragLastY - dragStartY)))
  const handle = event.currentTarget as HTMLElement
  if (handle.hasPointerCapture(event.pointerId)) handle.releasePointerCapture(event.pointerId)
}

function onPaneHandleKey(event: KeyboardEvent, kind: PaneKind): void {
  const el = paneEl(kind)
  if (el === null) return
  const current = paneHeights[kind] ?? (el.getBoundingClientRect().height || paneDefault())
  const steps: Record<string, number> = { ArrowDown: current + 24, ArrowUp: current - 24, Home: PANE_MIN, End: paneMax() }
  const next = steps[event.key]
  if (next === undefined) return
  event.preventDefault()
  commitPair(kind, clampPane(next))
}

/** 双击复位 = 两块一起回默认各半，并把两份记忆都清掉。 */
function resetPane(): void {
  rememberPane('alerts', null)
  rememberPane('handling', null)
  for (const k of ['alerts', 'handling'] as const) {
    const el = paneEl(k)
    if (el === null) continue
    el.style.height = ''
    el.querySelector('.side-pane__handle')?.setAttribute('aria-valuenow', String(paneDefault()))
  }
}

/* 值班台默认适配首屏。列表单独滚动，拉杆可调整整台高度。 */
const DESK_MIN = 420
const DESK_STORAGE = 'aegis.assistant.desk-height'
const deskRef = ref<HTMLElement | null>(null)
const deskMin = ref(0)
const deskOverride = ref<number | null>(readDeskHeight())

function readDeskHeight(): number | null {
  try {
    const raw = localStorage.getItem(DESK_STORAGE)
    if (raw === null) return null
    const value = Number(raw)
    return Number.isFinite(value) && value >= DESK_MIN ? Math.round(value) : null
  } catch {
    return null
  }
}
/** 给两栏留出同一个首屏高度，页脚跟在值班台之后。 */
function viewportDeskHeight(): number {
  const main = document.querySelector('.main-content') as HTMLElement | null
  const el = deskRef.value
  if (main === null || el === null) return 0
  const above = el.getBoundingClientRect().top - main.getBoundingClientRect().top + main.scrollTop
  return Math.max(DESK_MIN, Math.round(main.clientHeight - above - 16))
}
function deskMax(): number {
  return Math.max(DESK_MIN, viewportDeskHeight() * 2, deskRef.value?.scrollHeight ?? 0)
}
function clampDesk(height: number): number {
  return Math.min(deskMax(), Math.max(DESK_MIN, Math.round(height)))
}
const deskStyle = computed(() => {
  if (deskOverride.value !== null) return { height: `${deskOverride.value}px`, minHeight: '0px' }
  return deskMin.value > 0 ? { height: `${deskMin.value}px` } : undefined
})
function refreshDeskGeometry(): void {
  if (compact.value) return
  sideBudget.value = sideColRef.value?.clientHeight ?? 0
  reconcilePanes()
  deskMin.value = viewportDeskHeight()
  if (deskMin.value > 0) deskRef.value?.style.setProperty('--chat-pin-h', `${deskMin.value}px`)
}
function paintDesk(height: number): void {
  const el = deskRef.value
  if (el === null) return
  el.style.height = `${height}px`
  el.style.minHeight = '0px'
  el.parentElement?.querySelector('[data-testid="desk-handle"]')?.setAttribute('aria-valuenow', String(height))
}
function commitDesk(height: number): void {
  deskOverride.value = height
  try {
    localStorage.setItem(DESK_STORAGE, String(height))
  } catch {
    /* 存储不可用时只记这一屏 */
  }
}
function resetDesk(): void {
  deskOverride.value = null
  try {
    localStorage.removeItem(DESK_STORAGE)
  } catch {
    /* 同上 */
  }
  const el = deskRef.value
  if (el === null) return
  el.style.height = deskMin.value > 0 ? `${deskMin.value}px` : ''
  el.style.minHeight = ''
  el.parentElement?.querySelector('[data-testid="desk-handle"]')?.setAttribute('aria-valuenow', String(deskMin.value || DESK_MIN))
}

let deskDragStartY = 0
let deskDragStartH = 0
let deskDragLastY = 0
let deskDragRaf = 0
let deskDragging = false
function onDeskHandleDown(ev: PointerEvent): void {
  const el = ev.currentTarget as HTMLElement
  deskDragging = true
  deskDragStartY = ev.clientY
  deskDragLastY = ev.clientY
  deskDragStartH = deskRef.value?.getBoundingClientRect().height ?? DESK_MIN
  el.setPointerCapture(ev.pointerId)
  el.classList.add('is-resizing')
}
function onDeskHandleMove(ev: PointerEvent): void {
  // 悬停的 pointermove 也进这里：没按下就写高度会把 clientY 当成台高
  if (!deskDragging) return
  deskDragLastY = ev.clientY
  if (deskDragRaf !== 0) return
  deskDragRaf = requestAnimationFrame(() => {
    deskDragRaf = 0
    paintDesk(clampDesk(deskDragStartH + (deskDragLastY - deskDragStartY)))
  })
}
function onDeskHandleUp(ev: PointerEvent): void {
  if (!deskDragging) return
  deskDragging = false
  const el = ev.currentTarget as HTMLElement
  if (deskDragRaf !== 0) {
    cancelAnimationFrame(deskDragRaf)
    deskDragRaf = 0
  }
  if (deskDragLastY !== deskDragStartY) commitDesk(clampDesk(deskDragStartH + (deskDragLastY - deskDragStartY)))
  if (el.hasPointerCapture(ev.pointerId)) el.releasePointerCapture(ev.pointerId)
  el.classList.remove('is-resizing')
}
function onDeskHandleKey(ev: KeyboardEvent): void {
  const current = deskOverride.value ?? deskRef.value?.getBoundingClientRect().height ?? (deskMin.value || DESK_MIN)
  const steps: Record<string, number> = { ArrowDown: current + 24, ArrowUp: current - 24, Home: DESK_MIN, End: deskMax() }
  const next = steps[ev.key]
  if (next === undefined) return
  ev.preventDefault()
  commitDesk(clampDesk(next))
  paintDesk(clampDesk(next))
}

onMounted(() => {
  void loadCapabilities()
  if (props.mode !== 'page') return
  refreshDeskGeometry()
  const desk = deskRef.value
  if (desk && typeof ResizeObserver !== 'undefined') {
    budgetObserver = new ResizeObserver(refreshDeskGeometry)
    budgetObserver.observe(desk)
    const main = desk.closest('.main-content')
    if (main) budgetObserver.observe(main)
  }
})
watch(compact, refreshDeskGeometry, { flush: 'post' })

onBeforeUnmount(() => {
  budgetObserver?.disconnect()
  if (dragRaf !== 0) cancelAnimationFrame(dragRaf)
  if (deskDragRaf !== 0) cancelAnimationFrame(deskDragRaf)
})
</script>

<template>
  <div class="panel" :class="[`panel--${props.mode}`, { 'panel--compact': compact }]" data-testid="assistant-panel">
    <a-alert
      v-if="capabilitiesError && capabilitiesKind === 'unavailable'"
      type="error"
      show-icon
      banner
      data-testid="banner-unavailable"
      message="语义交互腿未装配（503 / E_ASSISTANT_UNAVAILABLE）：这个出口当前不工作"
      :description="capabilitiesError"
    />
    <a-alert
      v-else-if="capabilitiesError"
      type="error"
      show-icon
      banner
      data-testid="banner-caps-error"
      :message="capabilitiesKind === 'disabled' ? '助手出口未启用：路由整段没挂载（404）' : '读不到能力面：以下说明都按未验证处理'"
      :description="capabilitiesError"
    />

    <!-- page 模式值班台：左会话占满全高，右栏告警/处置竖排；抽屉模式 desk 退化为 contents -->
    <div
      ref="deskRef"
      class="desk"
      :style="!compact ? deskStyle : undefined"
      :data-testid="props.mode === 'page' ? 'assistant-desk' : undefined"
    >
      <div class="chat">
        <!-- 抽屉模式会话优先：两块收进图标栏，点开才滑出；问答框常驻底 -->
        <template v-if="compact">
          <div class="dockbar" role="group" aria-label="值班工具" data-testid="dock-bar">
            <button
              type="button"
              class="dockbar__button"
              :class="{ 'is-open': dockPane === 'alerts' }"
              :aria-expanded="dockPane === 'alerts'"
              aria-controls="dock-pane"
              :title="`在案告警 · ${dockAlertCount} 条`"
              data-testid="dock-toggle-alerts"
              @click="toggleDockPane('alerts')"
            >
              <BellOutlined aria-hidden="true" />
              <span>在案告警</span>
              <span v-if="dockAlertCount > 0" class="dockbar__badge is-alert" data-testid="dock-badge-alerts">{{ dockAlertCount }}</span>
              <RightOutlined class="dockbar__chevron" aria-hidden="true" />
            </button>
            <button
              type="button"
              class="dockbar__button"
              :class="{ 'is-open': dockPane === 'handling' }"
              :aria-expanded="dockPane === 'handling'"
              aria-controls="dock-pane"
              :title="`处置空间 · ${dockHandlingCount} 任务`"
              data-testid="dock-toggle-handling"
              @click="toggleDockPane('handling')"
            >
              <ToolOutlined aria-hidden="true" />
              <span>处置空间</span>
              <span v-if="dockHandlingCount > 0" class="dockbar__badge" data-testid="dock-badge-handling">{{ dockHandlingCount }}</span>
              <RightOutlined class="dockbar__chevron" aria-hidden="true" />
            </button>
          </div>
          <Transition name="dockpane">
            <div v-if="dockPane !== null" id="dock-pane" class="dockpane" data-testid="dock-pane">
              <AlertStack v-show="dockPane === 'alerts'" />
              <HandlingBar v-show="dockPane === 'handling'" />
            </div>
          </Transition>
        </template>
        <div ref="scrollHost" class="chat__scroll" data-testid="timeline">
        <div v-if="!frames.length && !streaming" class="welcome" data-testid="timeline-empty">
          <section class="welcome__intro">
            <span class="welcome__mark" aria-hidden="true">✦</span>
            <div class="welcome__copy">
              <h2>有什么可以帮你？</h2>
              <p>查询预警与态势、发起演练或上报险情。</p>
            </div>
          </section>

          <section v-if="capabilities && (quickTasks.length || catalogGroups.length)" class="welcome__actions" data-testid="caps-actions">
            <header class="welcome__head">
              <h3>{{ quickTasks.length ? '快捷任务' : '可用任务' }}</h3>
              <button
                v-if="quickTasks.length && catalogGroups.length"
                type="button"
                class="welcome__disclosure"
                :aria-expanded="showCatalog"
                :aria-controls="catalogId"
                data-testid="caps-disclosure"
                @click="showCatalog = !showCatalog"
              >
                {{ showCatalog ? '收起能力' : '更多能力' }} <DownOutlined aria-hidden="true" />
              </button>
            </header>

            <div v-if="quickTasks.length" class="welcome__grid" data-testid="quick-tasks">
              <button
                v-for="task in quickTasks"
                :key="task.action"
                type="button"
                class="quick-task"
                :class="task.spec.allowed ? 'is-available' : 'is-denied'"
                :data-testid="`action-${task.action}`"
                :disabled="!task.spec.example"
                :title="actionTitle(task.spec)"
                @click="useExample(task.spec)"
              >
                <span class="quick-task__icon"><component :is="task.icon" aria-hidden="true" /></span>
                <span class="quick-task__copy">
                  <strong>{{ task.title }}</strong>
                  <span class="quick-task__detail">{{ task.detail }}</span>
                  <span v-if="!task.spec.allowed || task.spec.needs_confirm" class="quick-task__rules">
                    <span v-if="!task.spec.allowed"><LockOutlined aria-hidden="true" /> 需{{ roleNote(task.spec) }}</span>
                    <span v-if="task.spec.needs_confirm">需确认</span>
                  </span>
                </span>
              </button>
            </div>

            <div
              v-if="catalogGroups.length"
              v-show="showCatalog || !quickTasks.length"
              :id="catalogId"
              class="caps"
              data-testid="caps-catalog"
            >
              <section v-for="group in catalogGroups" :key="group.domain" class="caps__group">
                <h4>{{ group.label }}</h4>
                <div class="caps__row">
                  <button
                    v-for="spec in group.items"
                    :key="spec.action"
                    type="button"
                    class="caps__chip"
                    :class="spec.allowed ? 'is-available' : 'is-denied'"
                    :data-testid="`action-${spec.action}`"
                    :disabled="!spec.example"
                    :title="actionTitle(spec)"
                    @click="useExample(spec)"
                  >
                    <LockOutlined v-if="!spec.allowed" aria-hidden="true" />
                    {{ spec.title }}
                    <span v-if="spec.needs_confirm">需确认</span>
                    <span v-if="!spec.allowed">需{{ roleNote(spec) }}</span>
                  </button>
                </div>
              </section>
            </div>
          </section>

          <details v-if="capabilities" class="welcome__retention">
            <summary>会话与操作记录</summary>
            <p data-testid="caps-ttl">
              会话保留 {{ Math.round(capabilities.session_ttl_seconds / 60) }} 分钟 ·
              待确认动作 {{ Math.round(capabilities.confirm_ttl_seconds / 60) }} 分钟内有效 ·
              动作台账{{ capabilities.control_actions_persistent ? '已持久化' : '为内存形态，重启即失' }}
            </p>
          </details>
        </div>

        <!-- 会话流：按轮分组，用户在右、助手在左；答案先给，过程帧折一行 -->
        <template v-for="turn in turns" :key="turn.frames[0]">
          <div class="turn">
            <div v-if="turn.userText !== null" class="bubble-user">{{ turn.userText }}</div>
            <div class="turn__assistant">
              <template v-for="(frame, frameIndex) in mainFrames(turn)" :key="frameIndex">
                <!-- 提案帧渲染成待确认卡：确认/放弃在卡上，处置结果也落回这张卡 -->
                <div v-if="frame.type === 'proposal'" class="proposal" :data-testid="`proposal-${frame.action_id}`">
                  <div class="proposal__head">
                    <span class="proposal__badge">待确认</span>
                    <span class="proposal__text">{{ frame.action }} ｜ {{ proposalText(frame) }}</span>
                    <span v-if="(frame.review_required ?? 0) >= 2" class="proposal__review" data-testid="review-progress">
                      双人复核 {{ frame.reviews?.length ?? 0 }}/{{ frame.review_required }}
                    </span>
                  </div>
                  <a-space size="small">
                    <a-button
                      type="primary"
                      size="small"
                      :disabled="decisionFinal(frame.action_id)"
                      :loading="decisions[frame.action_id]?.kind === 'busy'"
                      :data-testid="`confirm-${frame.action_id}`"
                      @click="confirmProposal(frame)"
                    >
                      确认
                    </a-button>
                    <a-button
                      size="small"
                      :disabled="!!decisions[frame.action_id]"
                      :data-testid="`dismiss-${frame.action_id}`"
                      @click="dismissProposal(frame)"
                    >
                      放弃
                    </a-button>
                  </a-space>
                  <div v-if="decisionText(frame.action_id)" class="decision" data-testid="decision">{{ decisionText(frame.action_id) }}</div>
                </div>

                <!-- 回答是主回复 -->
                <div v-else-if="frame.type === 'answer'" class="frame frame--answer" :data-testid="`frame-${frame.type}`">
                  <span class="body">{{ frameText(frame) }}</span>
                </div>

                <!-- 拒绝与错误是必须看见的事实，不折叠 -->
                <div v-else class="frame refusal" :data-testid="`frame-${frame.type}`">
                  <span class="kind">{{ frame.type }}</span>
                  <span class="body">{{ frameText(frame) }}</span>
                </div>
              </template>

              <!-- 过程帧：有回答就折一行（原生 details，要核对时点开）；没有回答就原样摆出 -->
              <details v-if="collapseQuiet(turn)" class="process">
                <summary>过程 ｜ {{ quietFrames(turn).length }} 帧</summary>
                <div v-for="(frame, frameIndex) in quietFrames(turn)" :key="frameIndex" class="frame" :data-testid="`frame-${frame.type}`">
                  <span class="kind">{{ frame.type }}</span>
                  <span class="body">{{ frameText(frame) }}</span>
                </div>
              </details>
              <template v-else>
                <div v-for="(frame, frameIndex) in quietFrames(turn)" :key="frameIndex" class="frame" :data-testid="`frame-${frame.type}`">
                  <span class="kind">{{ frame.type }}</span>
                  <span class="body">{{ frameText(frame) }}</span>
                </div>
              </template>
            </div>
          </div>
        </template>

        <!-- 流式进行中：报最新帧名，过程折叠了但"此刻在等什么"仍一眼可见 -->
        <div v-if="streaming" class="typing">
          正在处理…<span v-if="lastFrame" class="typing__frame">{{ lastFrame.type }}</span>
        </div>
      </div>

      <div class="composer">
        <p v-if="streamError" class="error" data-testid="stream-error">{{ streamError }}</p>
        <div class="composer__context">
          <div class="composer__field">
            <label :for="`${fieldId}-reporter`">上报人</label>
            <a-input
              :id="`${fieldId}-reporter`"
              v-model:value="reporter"
              :maxlength="CHAT_LIMITS.reporter.max"
              placeholder="上报人名义"
              data-testid="field-reporter"
            />
          </div>
          <div class="composer__field">
            <label :for="`${fieldId}-region`">区域代码（选填）</label>
            <a-input
              :id="`${fieldId}-region`"
              v-model:value="regionCode"
              :maxlength="24"
              placeholder="如 540121"
              data-testid="field-region"
            />
          </div>
          <a-button size="small" data-testid="open-report" @click="reportOpen = true">人工上报</a-button>
        </div>
        <div ref="composerBox" class="composer__box">
          <a-textarea
            v-model:value="draft"
            :rows="2"
            :auto-size="{ minRows: 2, maxRows: compact ? 3 : 6 }"
            :maxlength="CHAT_LIMITS.message.max"
            aria-label="输入你的问题或指令"
            placeholder="输入你的问题或指令…"
            data-testid="field-message"
            @keydown="onMessageKeydown"
          />
        </div>
        <!-- 计数自己渲染而不是用 antd 的 show-count：那一个的文本挂在 data-count 上，界面上看得见却测不着 -->
        <div class="composer__meta">
          <div class="composer__info">
            <span class="composer__count" data-testid="char-count">
              <span>{{ draft.length }} / {{ CHAT_LIMITS.message.max }} 字</span>
              <span class="composer__keys">Enter 发送，Shift+Enter 换行</span>
            </span>
            <span v-if="sessionId" class="composer__session" data-testid="session-id" :title="sessionId">会话 {{ sessionId }}</span>
          </div>
          <div class="composer__actions">
            <a-button v-if="streaming" danger size="small" data-testid="stop" @click="stopStream">停止</a-button>
            <a-button size="small" type="text" data-testid="clear" @click="clearTimeline">清空</a-button>
            <a-button type="primary" :loading="streaming" data-testid="send" @click="send">
              <template #icon><SendOutlined aria-hidden="true" /></template>
              发送
            </a-button>
          </div>
        </div>
      </div>
      </div>

      <!-- 右栏：在案告警 上、处置空间 下，竖排满高；每板底边一条拉杆，两板高度互补 -->
      <div v-if="!compact" ref="sideColRef" class="side" data-testid="assistant-side">
        <section
          v-for="pane in PANES"
          :key="pane.kind"
          :ref="(el) => setPaneRef(pane.kind, el)"
          class="side-pane"
          :class="`side-pane--${pane.kind}`"
          :style="paneHeights[pane.kind] !== null ? { height: `${paneHeights[pane.kind]}px` } : undefined"
          :data-testid="`${pane.kind}-pane`"
        >
          <div class="side-pane__body">
            <component :is="pane.component" />
          </div>
          <div
            class="side-pane__handle"
            role="slider"
            tabindex="0"
            :aria-label="`拖动调整「${pane.label}」面板高度（上下方向键微调，双击复位）`"
            :aria-valuemin="PANE_MIN"
            :aria-valuemax="paneMax()"
            :aria-valuenow="paneHeights[pane.kind] ?? paneDefault()"
            :data-testid="`${pane.kind}-pane-handle`"
            @pointerdown="onPaneHandleDown($event, pane.kind)"
            @pointermove="onPaneHandleMove"
            @pointerup="onPaneHandleUp"
            @pointercancel="onPaneHandleUp"
            @keydown="onPaneHandleKey($event, pane.kind)"
            @dblclick="resetPane()"
          >
            <span class="side-pane__grip" aria-hidden="true"></span>
          </div>
        </section>
      </div>
    </div>

    <!-- 整页拉杆：值班台比一屏高时整页可滚，这条负责把整台高度拖回来/放出去 -->
    <div
      v-if="props.mode === 'page'"
      v-show="!compact"
      class="desk-handle"
      role="slider"
      tabindex="0"
      aria-label="拖动调整值班台整体高度（上下方向键微调，双击复位）"
      :aria-valuemin="DESK_MIN"
      :aria-valuemax="deskMax()"
      :aria-valuenow="deskOverride ?? (deskMin || DESK_MIN)"
      data-testid="desk-handle"
      @pointerdown="onDeskHandleDown"
      @pointermove="onDeskHandleMove"
      @pointerup="onDeskHandleUp"
      @pointercancel="onDeskHandleUp"
      @keydown="onDeskHandleKey"
      @dblclick="resetDesk()"
    >
      <span class="side-pane__grip" aria-hidden="true"></span>
    </div>

    <a-modal v-model:open="reportOpen" title="人工上报（与监测事件同一条链路）" :footer="null" width="720px" data-testid="report-modal">
      <ReportForm :initial-region-code="regionCode" :initial-reporter="reporter" />
    </a-modal>
  </div>
</template>

<style scoped>
.panel {
  display: flex;
  flex-direction: column;
  gap: var(--aeg-space-s3);
  min-height: 0;
}
.panel--page {
  width: 100%;
  flex: 1;
  min-height: 380px;
  gap: var(--aeg-space-s4);
}

.panel--page:not(.panel--compact) .desk {
  display: grid;
  flex: none;
  min-height: 0;
  grid-template-columns: minmax(0, 1.8fr) minmax(0, 1fr);
  grid-template-rows: minmax(0, 1fr);
  gap: var(--aeg-space-s4);
}
.panel--compact .desk {
  display: contents;
}
.side {
  display: flex;
  flex-direction: column;
  gap: var(--aeg-space-s4);
  min-height: 0;
  min-width: 0;
}
/* 默认各占一半（栏高减缝再对半）；拉杆落地后由内联高度接管 */
.side-pane {
  flex: none;
  height: calc(50% - var(--aeg-space-s2));
  min-height: 160px;
  max-height: 100%;
  display: flex;
  flex-direction: column;
  background: linear-gradient(145deg, var(--aeg-color-overview-panel), var(--aeg-color-overview-tile));
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-xl);
  overflow: hidden;
  /* 与驾驶舱同一口径：自成合成层，面板内滚动只动纹理，不重栅整页 */
  transform: translateZ(0);
}
.side-pane__body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
}
/* 面板自己就是卡片面：里面两个组件在抽屉模式自带的那层边框/底色在这里脱掉，避免卡中卡 */
.side-pane--alerts :deep(.alerts) {
  border: none;
  border-radius: 0;
  background: transparent;
}
.side-pane--handling :deep(.handling) {
  border: none;
  background: transparent;
}
.side-pane :deep(:is(.alerts__title, .handling__title)) {
  font-size: var(--aeg-font-md);
}
.side-pane--handling :deep(.handling__tasks) {
  max-height: none;
}
.side-pane__handle {
  flex: none;
  height: 14px;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: row-resize;
  touch-action: none;
  border-top: 1px solid var(--aeg-color-border-subtle);
  background: var(--aeg-color-alter);
  transition: background-color var(--aeg-motion-base) ease;
}
.side-pane__handle:hover,
.side-pane__handle:focus-visible,
.side-pane.is-resizing .side-pane__handle {
  background: var(--aeg-color-brand-soft);
}
.side-pane__handle:focus-visible {
  outline: 2px solid var(--aeg-color-brand);
  outline-offset: -2px;
}
.side-pane__grip {
  width: 36px;
  height: 3px;
  border-radius: var(--aeg-radius-pill);
  background: var(--aeg-color-border-strong);
}
/* 整页拉杆：与板拉杆同一族 affordance，横贯值班台底边 */
.desk-handle {
  flex: none;
  height: 14px;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: row-resize;
  touch-action: none;
  border: 1px solid var(--aeg-color-border-subtle);
  border-radius: var(--aeg-radius-sm);
  background: var(--aeg-color-alter);
  transition: background-color var(--aeg-motion-base) ease;
}
.desk-handle:hover,
.desk-handle:focus-visible,
.desk-handle.is-resizing {
  background: var(--aeg-color-brand-soft);
}
.desk-handle:focus-visible {
  outline: 2px solid var(--aeg-color-brand);
  outline-offset: -2px;
}

.panel--dock {
  height: 100%;
}
.panel--page.panel--compact {
  flex: none;
  height: clamp(400px, calc(100dvh - 228px), 780px);
}
.chat {
  min-width: 0;
  min-height: 0;
  width: 100%;
  container: assistant-chat / inline-size;
  display: flex;
  flex-direction: column;
  background: var(--aeg-color-overview-tile);
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-xl);
  box-shadow: inset 0 1px 0 var(--aeg-color-glass-border), var(--aeg-shadow-md);
  overflow: hidden;
}
.panel--compact .chat {
  flex: 1;
}
.panel--dock .chat {
  border: none;
  border-radius: 0;
  background: transparent;
  box-shadow: none;
}
.panel--page:not(.panel--compact) .chat {
  position: sticky;
  top: var(--aeg-space-s4);
  align-self: start;
  height: min(100%, var(--chat-pin-h, 100%));
}
@supports (backdrop-filter: blur(14px)) {
  @media (prefers-reduced-transparency: no-preference) {
    .panel--page .chat {
      background: color-mix(in srgb, var(--aeg-color-overview-tile) 86%, transparent);
      backdrop-filter: blur(14px) saturate(125%);
    }
  }
}
.chat__scroll {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: var(--aeg-space-s4);
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: var(--aeg-space-s4);
}
.panel--compact .chat__scroll {
  padding: var(--aeg-space-s3);
}
.dockbar {
  flex: none;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--aeg-space-s1);
  margin: var(--aeg-space-s3) var(--aeg-space-s3) 0;
  padding: var(--aeg-space-s1);
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-lg);
  background: var(--aeg-color-sunken);
}
.dockbar__button {
  display: inline-flex;
  align-items: center;
  gap: var(--aeg-space-s1);
  min-width: 0;
  min-height: 40px;
  padding: 0 var(--aeg-space-s2);
  border: 1px solid transparent;
  border-radius: var(--aeg-radius-md);
  background: transparent;
  color: var(--aeg-color-text);
  font-size: var(--aeg-font-sm);
  font-weight: var(--aeg-weight-semibold);
  white-space: nowrap;
  cursor: pointer;
  transition: border-color var(--aeg-motion-base), background-color var(--aeg-motion-base);
}
.dockbar__button > :first-child {
  color: var(--aeg-color-overview-accent);
  font-size: var(--aeg-font-md);
}
.dockbar__button:is(:hover, .is-open) {
  background: var(--aeg-color-overview-panel);
  border-color: var(--aeg-color-brand-line);
}
.dockbar__chevron {
  margin-left: auto;
  color: var(--aeg-color-text-secondary);
  font-size: var(--aeg-font-xs);
}
.dockbar__badge {
  min-width: 16px;
  padding: 0 var(--aeg-space-s1);
  border-radius: var(--aeg-radius-pill);
  background: var(--aeg-color-brand-soft);
  color: var(--aeg-color-text);
  font-size: var(--aeg-font-xs);
  line-height: 16px;
  text-align: center;
  font-variant-numeric: tabular-nums;
}
.dockbar__badge.is-alert {
  background: var(--aeg-tag-red-bg);
  color: var(--aeg-tag-red-text);
  box-shadow: inset 0 0 0 1px var(--aeg-tag-red-line);
}
/* 展开层：点开才占空间，上限抽屉 45%，内部滚；一次只开一块 */
.dockpane {
  flex: 0 1 auto;
  min-height: 0;
  overscroll-behavior: contain;
  max-height: 45%;
  overflow-y: auto;
  padding: var(--aeg-space-s2) var(--aeg-space-s3);
  border-bottom: 1px solid var(--aeg-color-border-subtle);
}
.dockpane-enter-active {
  animation: dockpane-in var(--aeg-motion-base) ease;
}
.dockpane-leave-active {
  animation: dockpane-in var(--aeg-motion-base) ease reverse;
}
@keyframes dockpane-in {
  from {
    opacity: 0;
    transform: translateY(-8px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
.welcome {
  display: grid;
  gap: var(--aeg-space-s4);
  min-width: 0;
  width: 100%;
  max-width: 920px;
  margin-inline: auto;
}
.welcome__intro {
  position: relative;
  isolation: isolate;
  display: flex;
  align-items: flex-start;
  gap: var(--aeg-space-s3);
  overflow: hidden;
  padding: var(--aeg-space-s4);
  border: 1px solid var(--aeg-color-brand-line);
  border-radius: var(--aeg-radius-xl);
  background:
    radial-gradient(ellipse at 86% 8%, var(--aeg-color-brand-soft), transparent 64%),
    linear-gradient(135deg, var(--aeg-color-overview-panel), var(--aeg-color-overview-tile));
  box-shadow: inset 0 1px 0 var(--aeg-color-glass-border);
}
.welcome__intro::before {
  position: absolute;
  z-index: -1;
  inset: 0 0 0 38%;
  background: url('/backgrounds/tibet-relief.webp') 66% 46% / cover;
  opacity: 0.15;
  mask-image: linear-gradient(90deg, transparent, black);
  content: '';
}
.welcome__mark,
.quick-task__icon {
  display: grid;
  place-items: center;
  width: 36px;
  height: 36px;
  flex: 0 0 36px;
  border: 1px solid var(--aeg-color-brand-line);
  border-radius: var(--aeg-radius-md);
  background: var(--aeg-color-brand-soft);
  color: var(--aeg-color-overview-accent);
  font-size: var(--aeg-font-md);
}
.welcome__mark {
  font-size: var(--aeg-font-xl);
}
.welcome__copy {
  min-width: 0;
}
.welcome__copy h2,
.welcome__head h3 {
  margin: 0;
  color: var(--aeg-color-text);
  font-weight: var(--aeg-weight-bold);
}
.welcome__copy h2 {
  font-size: var(--aeg-font-xl);
  line-height: var(--aeg-lh-tight);
}
.welcome__copy p {
  color: var(--aeg-color-text-secondary);
  font-size: var(--aeg-font-base);
  line-height: var(--aeg-lh-normal);
  margin: var(--aeg-space-s2) 0 0;
}
.welcome__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--aeg-space-s2);
  margin-bottom: var(--aeg-space-s2);
}
.welcome__head h3 {
  font-size: var(--aeg-font-md);
}
.welcome__disclosure {
  min-height: 32px;
  padding: 0 var(--aeg-space-s1);
  border: 0;
  background: transparent;
  color: var(--aeg-color-overview-accent);
  cursor: pointer;
  font-size: var(--aeg-font-sm);
}
.welcome__disclosure[aria-expanded='true'] :deep(svg) {
  transform: rotate(180deg);
}
.welcome__grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--aeg-space-s2);
}
.quick-task {
  display: flex;
  align-items: flex-start;
  gap: var(--aeg-space-s2);
  min-width: 0;
  min-height: 96px;
  padding: var(--aeg-space-s3);
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-lg);
  background: linear-gradient(145deg, var(--aeg-color-overview-panel), var(--aeg-color-overview-tile) 76%);
  box-shadow: inset 0 1px 0 color-mix(in srgb, var(--aeg-color-glass-border) 43%, transparent);
  color: var(--aeg-color-text);
  text-align: left;
  cursor: pointer;
  transition: border-color var(--aeg-motion-base), background-color var(--aeg-motion-base);
}
.quick-task:hover:not(:disabled) {
  border-color: var(--aeg-color-overview-accent);
  background: var(--aeg-color-overview-panel);
}
.quick-task:disabled,
.caps__chip:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
.quick-task__copy {
  display: grid;
  gap: var(--aeg-space-s1);
  min-width: 0;
}
.quick-task:nth-child(2) .quick-task__icon {
  color: var(--aeg-color-editor-cat-human);
  background: color-mix(in srgb, currentColor 12%, transparent);
  border-color: color-mix(in srgb, currentColor 26%, transparent);
}
.quick-task:nth-child(3) .quick-task__icon {
  color: var(--aeg-color-brand-hover);
}
.quick-task:nth-child(4) .quick-task__icon {
  color: var(--aeg-color-editor-cat-logic);
  background: color-mix(in srgb, currentColor 12%, transparent);
  border-color: color-mix(in srgb, currentColor 26%, transparent);
}
.quick-task__copy strong {
  font-size: var(--aeg-font-md);
  font-weight: var(--aeg-weight-bold);
}
.quick-task__detail,
.quick-task__rules {
  color: var(--aeg-color-text-secondary);
  font-size: var(--aeg-font-sm);
}
.quick-task__rules {
  display: flex;
  flex-wrap: wrap;
  gap: var(--aeg-space-s1);
}
.quick-task__rules > span {
  padding-inline: var(--aeg-space-s1);
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-pill);
  background: var(--aeg-color-sunken);
  white-space: nowrap;
}
.caps {
  display: grid;
  gap: var(--aeg-space-s3);
  padding: var(--aeg-space-s3) 0 0;
}
.caps__group h4 {
  margin: 0 0 var(--aeg-space-s1);
  font-size: var(--aeg-font-sm);
  color: var(--aeg-color-text-secondary);
}
.caps__row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--aeg-space-s1);
}
.caps__chip {
  display: inline-flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--aeg-space-s1);
  min-height: 30px;
  padding: var(--aeg-space-s1) var(--aeg-space-s2);
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-pill);
  background: var(--aeg-color-overview-tile);
  color: var(--aeg-color-text);
  font-size: var(--aeg-font-sm);
  cursor: pointer;
}
.caps__chip:hover:not(:disabled) {
  border-color: var(--aeg-color-brand-line);
}
.caps__chip.is-denied,
.caps__chip > span {
  color: var(--aeg-color-text-secondary);
}
.welcome__retention {
  color: var(--aeg-color-text-secondary);
  font-size: var(--aeg-font-sm);
}
.welcome__retention summary {
  cursor: pointer;
}
.welcome__retention p {
  margin: var(--aeg-space-s2) 0 0;
}
.turn {
  display: flex;
  flex-direction: column;
  gap: var(--aeg-space-s3);
}
.bubble-user {
  align-self: flex-end;
  max-width: 78%;
  background: var(--aeg-color-brand-soft);
  border: 1px solid var(--aeg-color-brand-line);
  border-radius: var(--aeg-radius-lg);
  border-bottom-right-radius: var(--aeg-radius-sm);
  padding: var(--aeg-space-s2) var(--aeg-space-s4);
  font-size: var(--aeg-font-base);
  line-height: var(--aeg-lh-normal);
  white-space: pre-wrap;
  word-break: break-word;
}
.turn__assistant {
  display: flex;
  flex-direction: column;
  gap: var(--aeg-space-s2);
  max-width: 94%;
}
.frame {
  display: flex;
  gap: var(--aeg-space-s2);
  align-items: baseline;
  padding: 2px 0;
  font-size: var(--aeg-font-sm);
}
.frame .kind {
  flex: none;
  color: var(--aeg-color-text-secondary);
  font-family: var(--aeg-font-mono);
  font-size: var(--aeg-font-xs);
  line-height: 16px;
  background: var(--aeg-color-sunken);
  border: 1px solid var(--aeg-color-border-subtle);
  border-radius: var(--aeg-radius-pill);
  padding: 0 var(--aeg-space-s2);
}
.frame .body {
  color: var(--aeg-color-text-secondary);
  word-break: break-word;
}
.frame--answer .body {
  color: var(--aeg-color-text);
  font-size: var(--aeg-font-base);
  line-height: var(--aeg-lh-relaxed);
  white-space: pre-wrap;
  max-width: 68ch;
}
.frame.refusal .body {
  color: var(--aeg-color-status-danger);
  font-weight: var(--aeg-weight-semibold);
}
.process summary {
  display: inline-flex;
  align-items: center;
  cursor: pointer;
  list-style: none;
  color: var(--aeg-color-text-secondary);
  font-size: var(--aeg-font-xs);
  padding: 2px var(--aeg-space-s2);
  margin-left: calc(-1 * var(--aeg-space-s2));
  border-radius: var(--aeg-radius-sm);
  transition: color var(--aeg-motion-base) ease, background-color var(--aeg-motion-base) ease;
}
.process summary:hover {
  color: var(--aeg-color-text);
  background: var(--aeg-color-alter);
}
.process summary::-webkit-details-marker {
  display: none;
}
.process[open] summary {
  margin-bottom: var(--aeg-space-s1);
}
.typing {
  color: var(--aeg-color-text-secondary);
  font-size: var(--aeg-font-sm);
}
.typing__frame {
  margin-left: var(--aeg-space-s2);
  font-family: var(--aeg-font-mono);
  font-size: var(--aeg-font-xs);
  color: var(--aeg-color-text-secondary);
  background: var(--aeg-color-sunken);
  border: 1px solid var(--aeg-color-border-subtle);
  border-radius: var(--aeg-radius-pill);
  padding: 0 var(--aeg-space-s2);
  line-height: 16px;
}
.proposal {
  border: 1px solid var(--aeg-tag-orange-line);
  background: var(--aeg-tag-orange-bg);
  border-radius: var(--aeg-radius-md);
  padding: var(--aeg-space-s3) var(--aeg-space-s4);
  display: flex;
  flex-direction: column;
  gap: var(--aeg-space-s2);
}
.proposal__head {
  display: flex;
  align-items: baseline;
  gap: var(--aeg-space-s2);
  flex-wrap: wrap;
  font-size: var(--aeg-font-sm);
}
.proposal__badge {
  flex: none;
  font-size: var(--aeg-font-xs);
  font-weight: var(--aeg-weight-semibold);
  line-height: 16px;
  color: var(--aeg-tag-orange-text);
  background: var(--aeg-color-card);
  border: 1px solid var(--aeg-tag-orange-line);
  border-radius: var(--aeg-radius-pill);
  padding: 0 var(--aeg-space-s2);
}
.proposal__text {
  word-break: break-word;
}
.decision {
  font-size: var(--aeg-font-sm);
  color: var(--aeg-color-text-secondary);
  word-break: break-word;
}
.error {
  color: var(--aeg-color-status-danger);
  font-size: var(--aeg-font-sm);
  margin: 0 0 var(--aeg-space-s2);
}
.composer {
  flex: none;
  border-top: 1px solid var(--aeg-color-border);
  padding: var(--aeg-space-s3) var(--aeg-space-s4);
  background: var(--aeg-color-overview-panel);
}
.panel--compact .composer {
  padding-inline: var(--aeg-space-s3);
}
.composer__context {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1.2fr) auto;
  gap: var(--aeg-space-s2);
  align-items: end;
  margin-bottom: var(--aeg-space-s2);
}
.composer__field {
  display: grid;
  gap: var(--aeg-space-s1);
  min-width: 0;
}
.composer__field label {
  color: var(--aeg-color-text-secondary);
  font-size: var(--aeg-font-sm);
}
.composer__context :deep(.ant-input),
.composer__context :deep(.ant-btn) {
  min-height: 32px;
  border-color: var(--aeg-color-border-strong);
  border-radius: var(--aeg-radius-md);
  background: var(--aeg-color-sunken);
  box-shadow: none;
}
.composer__context :deep(.ant-btn:not(:disabled):hover) {
  border-color: var(--aeg-color-overview-accent);
  color: var(--aeg-color-overview-accent);
}
.composer__box {
  display: flex;
  align-items: flex-end;
  border: 1px solid var(--aeg-color-border-strong);
  border-radius: var(--aeg-radius-lg);
  background: var(--aeg-color-sunken);
  padding: var(--aeg-space-s2) var(--aeg-space-s3);
  transition: border-color var(--aeg-motion-base), box-shadow var(--aeg-motion-base);
}
.composer__box:focus-within {
  border-color: var(--aeg-color-overview-accent);
  box-shadow: 0 0 0 2px var(--aeg-color-brand-soft);
}
.composer__box :deep(.ant-input) {
  min-height: 40px;
  padding: 0;
  border: none;
  box-shadow: none;
  background: transparent;
  resize: none;
  font-size: var(--aeg-font-base);
  line-height: var(--aeg-lh-normal);
}
.composer :deep(.ant-input::placeholder) {
  color: var(--aeg-color-text-muted);
}
.composer__actions {
  flex: none;
  display: flex;
  align-items: center;
  gap: var(--aeg-space-s1);
}
.composer__actions :deep(.ant-btn) {
  min-height: 36px;
  border-radius: var(--aeg-radius-md);
}
.composer__meta .composer__actions :deep(.ant-btn-primary:not(:disabled)) {
  background: var(--aeg-color-masthead-tab);
  border-color: var(--aeg-color-masthead-tab);
  color: var(--aeg-color-masthead-tab-ink);
  font-weight: var(--aeg-weight-semibold);
  box-shadow: var(--aeg-shadow-sm);
}
.composer__meta .composer__actions :deep(.ant-btn-primary:not(:disabled):hover) {
  filter: brightness(1.08);
}
.composer__actions :deep(.ant-btn-text) {
  color: var(--aeg-color-text-secondary);
}
.composer__meta {
  margin-top: var(--aeg-space-s2);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--aeg-space-s2);
}
.composer__info {
  min-width: 0;
  color: var(--aeg-color-text-secondary);
  font-size: var(--aeg-font-sm);
}
.composer__count {
  display: flex;
  flex-wrap: wrap;
  gap: 0 var(--aeg-space-s2);
  font-variant-numeric: tabular-nums;
}
.composer__session {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
@container assistant-chat (max-width: 410px) {
  .quick-task {
    flex-direction: column;
    min-height: 0;
  }
  .quick-task__icon {
    width: 28px;
    height: 28px;
    flex-basis: 28px;
  }
  .quick-task__copy strong {
    font-size: var(--aeg-font-base);
  }
  .dockbar__chevron {
    display: none;
  }
  .composer__keys {
    font-size: var(--aeg-font-xs);
  }
  .panel--page.panel--compact .welcome {
    gap: var(--aeg-space-s3);
  }
  .panel--page.panel--compact .welcome__intro {
    padding: var(--aeg-space-s3);
  }
  .panel--page.panel--compact .welcome__copy h2 {
    font-size: var(--aeg-font-lg);
  }
  .panel--page.panel--compact .quick-task {
    flex-direction: row;
    gap: var(--aeg-space-s1);
    min-height: 92px;
    padding: var(--aeg-space-s2);
  }
}
</style>
