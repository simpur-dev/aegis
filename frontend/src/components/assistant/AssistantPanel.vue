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
 * 3. **能力面读不到 ≠ 一切正常**。`llm_configured=false` 明说"仅规则词表可用"，
 *    503（腿没装配）与 404（出口未启用）是两句话。
 * 4. **角色不够的动作照样列出来**，置灰并说明缺什么——把动作藏起来，用户只会以为助手不会这个。
 */
import { message } from 'ant-design-vue'
import { nextTick, onMounted, ref, watch } from 'vue'

import type { ActionSpecDto, ConfirmResultDto, ProposalFrame } from '@/api/assistant'
import { assistantApi, CHAT_LIMITS, isAssistantDisabled, isAssistantUnavailable, preflightChat } from '@/api/assistant'
import ReportForm from '@/components/reports/ReportForm.vue'
import AlertStack from '@/components/assistant/AlertStack.vue'
import HandlingBar from '@/components/assistant/HandlingBar.vue'
import { useAssistantCapabilities } from '@/composables/useAssistantCapabilities'
import { useAssistantSession } from '@/composables/useAssistantSession'
import { useConversationModel } from '@/composables/useConversationModel'
import { frameText, proposalText } from '@/utils/frameText'

const props = withDefaults(defineProps<{ mode?: 'page' | 'dock' }>(), { mode: 'page' })

const {
  capabilities,
  error: capabilitiesError,
  kind: capabilitiesKind,
  llmConfigured,
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
  try {
    // 用户那句话要跟这一轮绑在一起记：等到第一帧落地才记——
    // 一发出去就记的话，零帧的失败请求会让这句话冒充一轮，把尾对齐全盘错位。
    let recorded = false
    for await (const frame of assistantApi.chat(request)) {
      if (frame.type === 'meta') sessionId.value = frame.session_id
      frames.value.push(frame)
      if (!recorded) {
        recordSent(text)
        recorded = true
      }
    }
    draft.value = ''
    pendingSend = null
  } catch (error) {
    if (isAssistantUnavailable(error)) streamError.value = `语义交互服务未装配：${error instanceof Error ? error.message : String(error)}`
    else if (isAssistantDisabled(error)) streamError.value = '助手出口未启用（路由整段没挂载）：这一轮请求没有发出，页面上没有新事实。'
    else streamError.value = error instanceof Error ? `对话中断：${error.message}` : '对话中断'
  } finally {
    streaming.value = false
  }
}

/**
 * Enter 发送、Shift+Enter 换行——多行输入框的通用约定。
 * 流式期间按钮点不动（antd 的 loading 让它接不到点击），键盘这条路必须自己守住：
 * 否则两轮回答会织进同一条时间线，会话号也说不清是哪一轮的。
 */
function onMessageKeydown(event: KeyboardEvent): void {
  if (event.shiftKey) return
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

onMounted(() => void loadCapabilities())
</script>

<template>
  <div class="panel" :class="`panel--${props.mode}`" data-testid="assistant-panel">
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
    <a-alert
      v-else-if="capabilities && !llmConfigured"
      type="warning"
      show-icon
      banner
      data-testid="banner-no-llm"
      message="语义服务未配置：仅规则词表可用"
      description="没有 LLM 网关时意图判定只走词表规则，自由问句会读不出来；查询类动作与人工确认仍可用。"
    />

    <div class="chat">
      <AlertStack />
      <HandlingBar />
      <div ref="scrollHost" class="chat__scroll" data-testid="timeline">
        <!-- 空态：欢迎 + 建议（点芯片只填句子，发不发由人） -->
        <div v-if="!frames.length && !streaming" class="chat__empty" data-testid="timeline-empty">
          <div class="chat__greeting">有什么可以帮你？</div>
          <p class="chat__hint">查询预警与态势、发起演练、上报险情。执行类动作先提案，经你确认才落地。</p>
          <div v-if="capabilities" class="caps" data-testid="caps-actions">
            <button
              v-for="spec in capabilities.actions"
              :key="spec.action"
              type="button"
              class="caps__chip"
              :class="spec.allowed ? 'is-available' : 'is-denied'"
              :data-testid="`action-${spec.action}`"
              :disabled="spec.example === ''"
              :title="spec.example === '' ? '能力面没给这条动作的示例句' : `填入：${spec.example}`"
              @click="useExample(spec)"
            >
              {{ spec.title }}<span v-if="spec.needs_confirm" class="caps__chip__note">需确认</span><span
                v-if="!spec.allowed"
                class="caps__chip__note is-warn"
              >{{ spec.denied_reason || '当前席位无权限' }}</span>
            </button>
          </div>
          <div v-if="capabilities" class="chat__ttl" data-testid="caps-ttl">
            会话保留 {{ Math.round(capabilities.session_ttl_seconds / 60) }} 分钟
            ｜ 待确认动作 {{ Math.round(capabilities.confirm_ttl_seconds / 60) }} 分钟内有效
            ｜ 动作台账
            <span v-if="capabilities.control_actions_persistent">已持久化</span>
            <span v-else class="pill-dashed">内存形态，重启即失</span>
          </div>
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
          <a-input v-model:value="reporter" :maxlength="CHAT_LIMITS.reporter.max" style="width: 130px" size="small" placeholder="上报人名义" data-testid="field-reporter" />
          <a-input
            v-model:value="regionCode"
            :maxlength="24"
            style="width: 170px"
            size="small"
            placeholder="区划代码（可留空）"
            data-testid="field-region"
          />
          <a-button size="small" data-testid="open-report" @click="reportOpen = true">人工上报</a-button>
          <span class="muted" data-testid="session-id">会话 {{ sessionId ?? '未开始（由后端 meta 帧给出）' }}</span>
        </div>
        <div class="composer__box">
          <a-textarea
            v-model:value="draft"
            :rows="2"
            :auto-size="{ minRows: 2, maxRows: 6 }"
            :maxlength="CHAT_LIMITS.message.max"
            placeholder="如：最近发布了哪些预警 / 帮我上报 540121 沟道泥位抬升"
            data-testid="field-message"
            @keydown="onMessageKeydown"
          />
          <div class="composer__actions">
            <a-button type="primary" :loading="streaming" data-testid="send" @click="send">发送</a-button>
            <a-button size="small" type="text" data-testid="clear" @click="clearTimeline">清空</a-button>
          </div>
        </div>
        <!-- 计数自己渲染而不是用 antd 的 show-count：那一个的文本挂在 data-count 上，界面上看得见却测不着 -->
        <div class="composer__meta">
          <span class="muted" data-testid="char-count">
            {{ draft.length }} / {{ CHAT_LIMITS.message.max }} 字 ｜ Enter 发送，Shift+Enter 换行
          </span>
        </div>
      </div>
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
}
.panel--dock {
  height: 100%;
}
.muted {
  color: var(--aeg-color-text-secondary);
  font-size: var(--aeg-font-xs);
}
.chat {
  margin: 0 auto;
  max-width: 820px;
  width: 100%;
  height: calc(100vh - var(--aeg-header-height) - 208px);
  min-height: 480px;
  display: flex;
  flex-direction: column;
  background: var(--aeg-color-card);
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-lg);
  overflow: hidden;
}
/* 抽屉里没有页头，高度跟着容器走 */
.panel--dock .chat {
  height: 100%;
  min-height: 0;
  border: none;
  border-radius: 0;
  background: transparent;
}
.chat__scroll {
  flex: 1;
  overflow-y: auto;
  padding: var(--aeg-space-s6) var(--aeg-space-s6) var(--aeg-space-s5);
  display: flex;
  flex-direction: column;
  gap: var(--aeg-space-s6);
}
.panel--dock .chat__scroll {
  padding: var(--aeg-space-s3);
}
.chat__empty {
  margin: auto;
  text-align: center;
  padding: var(--aeg-space-s6) var(--aeg-space-s5);
}
.chat__greeting {
  font-size: var(--aeg-font-lg);
  font-weight: var(--aeg-weight-semibold);
}
.chat__hint {
  margin: var(--aeg-space-s2) auto 0;
  max-width: 48ch;
  color: var(--aeg-color-text-secondary);
  font-size: var(--aeg-font-sm);
  line-height: var(--aeg-lh-relaxed);
}
.caps {
  margin-top: var(--aeg-space-s5);
  display: flex;
  flex-wrap: wrap;
  gap: var(--aeg-space-s2);
  justify-content: center;
  max-width: 560px;
  margin-left: auto;
  margin-right: auto;
}
/*
 * 芯片改中性：可用的就是一块安静的表面，点上去才知道；「需确认」「无权限」降为小字缀注。
 * 无权限那一档保住红字——它是"量出来的状态"，不是装饰。
 */
.caps__chip {
  display: inline-flex;
  align-items: center;
  gap: var(--aeg-space-s1);
  min-height: 26px;
  padding: 3px var(--aeg-space-s3);
  border: 1px solid var(--aeg-color-border);
  /* 999px 写字面而不写 var(--aeg-radius-pill)：uiTheme 的芯片形状判据钉着这个字面，值本就相同 */
  border-radius: 999px;
  background: var(--aeg-color-alter);
  color: var(--aeg-color-text);
  font-size: var(--aeg-font-sm);
  font-weight: var(--aeg-weight-medium);
  line-height: var(--aeg-lh-snug);
  cursor: pointer;
  transition:
    background-color var(--aeg-motion-base) ease,
    border-color var(--aeg-motion-base) ease;
}
.caps__chip:hover:not(:disabled) {
  border-color: var(--aeg-color-brand-line);
  background: var(--aeg-color-brand-soft);
}
.caps__chip__note {
  color: var(--aeg-color-text-secondary);
  font-weight: var(--aeg-weight-regular);
}
.caps__chip__note.is-warn {
  color: var(--aeg-color-status-danger);
}
.caps__chip.is-denied {
  border-style: dashed;
  background: transparent;
  color: var(--aeg-color-text-secondary);
}
.caps__chip.is-denied:hover:not(:disabled) {
  border-color: var(--aeg-color-status-danger);
}
.caps__chip:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}
.chat__ttl {
  margin-top: var(--aeg-space-s4);
  font-size: var(--aeg-font-xs);
  color: var(--aeg-color-text-secondary);
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
  border-top: 1px solid var(--aeg-color-border-subtle);
  padding: var(--aeg-space-s3) var(--aeg-space-s6) var(--aeg-space-s4);
  background: var(--aeg-color-card);
}
.panel--dock .composer {
  padding: var(--aeg-space-s2) var(--aeg-space-s3) var(--aeg-space-s3);
}
.composer__context {
  display: flex;
  flex-wrap: wrap;
  gap: var(--aeg-space-s2);
  align-items: center;
  margin-bottom: var(--aeg-space-s2);
}
.composer__box {
  display: flex;
  align-items: flex-end;
  gap: var(--aeg-space-s2);
  border: 1px solid var(--aeg-color-border-strong);
  border-radius: var(--aeg-radius-lg);
  background: var(--aeg-color-card);
  padding: var(--aeg-space-s2) var(--aeg-space-s2) var(--aeg-space-s2) var(--aeg-space-s3);
  transition: border-color var(--aeg-motion-base) ease;
}
.composer__box:focus-within {
  border-color: var(--aeg-color-brand);
}
.composer__box :deep(.ant-input),
.composer__box textarea {
  flex: 1;
  min-height: 40px;
  border: none;
  box-shadow: none;
  background: transparent;
  padding: var(--aeg-space-s1) 0;
  resize: none;
  font-size: var(--aeg-font-base);
  line-height: var(--aeg-lh-normal);
}
.composer__actions {
  flex: none;
  display: flex;
  flex-direction: column;
  gap: var(--aeg-space-s1);
}
.composer__meta {
  margin-top: var(--aeg-space-s1);
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: var(--aeg-space-s2);
}
</style>
