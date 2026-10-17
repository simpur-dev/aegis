<script setup lang="ts">
/**
 * 智能助手页（完善计划批次 B3；2026-10-17 重排为通用助手的对话式布局，二轮降噪）。
 *
 * 语义约束一条没动，都来自后端事实，不是审美：
 * 1. **帧序列是过程事实**。后端刻意不做 token 级伪流式（`api/assistant_api.py:1-8`），
 *    给出的每一帧都是"哪一步在等什么"。所以一轮里的 meta/intent/status/result/done 一帧不少——
 *    但**收进「过程 ｜ N 帧」一个折行**（原生 details，默认合上）：值班员要的是回答，
 *    过程是"要核对时点开就有"，不是常驻屏幕的噪声；流式进行中在输入区上方报最新帧名，过程仍然可见；
 * 2. **执行类动作只能经 `/confirm` 落地**（`services/assistant.py:332-362`）。
 *    「放弃」只在这页把卡片收起来——后端没有取消接口，页面就照实说"仅在本地隐藏"，
 *    不许把"我没点"显示成"已经撤销"；
 * 3. **能力面读不到 ≠ 一切正常**。`llm_configured=false` 明说"语义服务未配置：仅规则词表可用"，
 *    接口失败明说"读不到"，503（腿没装配）与 404（出口未启用）是两句话
 *    （`assistant_api.py` 的 `_assistant` 与 `api/app.py` 的 `if cfg.assistant_enabled:`）。
 *
 * 布局（通用助手的形状）：中列聊天面——用户的话靠右成气泡、助手靠左，answer 是主回复；
 * rejected/error 属于"必须看见的事实"，不折叠；提案卡内嵌在会话流里。
 * 用户气泡的文字后端帧里没有，存在 `useAssistantSession` 的 `sentTexts`（与轮次尾对齐，刷页接得上）；
 * 对不上号的老轮次就画没有，不拿别的句子冒充。
 */
import { message } from 'ant-design-vue'
import { computed, nextTick, onMounted, ref, watch } from 'vue'

import type { ActionSpecDto, AssistantFrame, CapabilitiesDto, ConfirmResultDto, ProposalFrame } from '@/api/assistant'
import { assistantApi, CHAT_LIMITS, isAssistantDisabled, isAssistantUnavailable, preflightChat } from '@/api/assistant'
import PageHero from '@/components/PageHero.vue'
import ReportForm from '@/components/reports/ReportForm.vue'
import { useAssistantSession } from '@/composables/useAssistantSession'

const capabilities = ref<CapabilitiesDto | null>(null)
const capabilitiesError = ref('')
const capabilitiesKind = ref<'none' | 'unavailable' | 'disabled' | 'other'>('none')
const capabilitiesLoading = ref(false)

// 时间线与会话号在组件外面：换菜单再回来还得接得上（见 useAssistantSession 的说明）
const { frames, sentTexts, sessionId, decisions, clearTimeline, recordSent } = useAssistantSession()
const reporter = ref('值班员')
const regionCode = ref('')
const draft = ref('')
const streaming = ref(false)
const streamError = ref('')
const reportOpen = ref(false)

const llmConfigured = computed(() => capabilities.value?.llm_configured === true)

/** 一轮对话：用户那句话（可能对不上号，是 null）+ 这一轮的帧。 */
interface Turn {
  userText: string | null
  frames: AssistantFrame[]
}

/**
 * 帧按轮分组：meta 帧是后端开新轮的标记；开头没跟 meta 的散帧（比如只回了 error）
 * 也自成一轮。用户的话按"从尾部数第 i 轮 ↔ sentTexts 倒数第 i 条"贴回去——
 * 两边都只在尾部追加、只在头部截断（MAX_PERSISTED_FRAMES），尾对齐在截断后依然成立。
 */
const turns = computed<Turn[]>(() => {
  const result: Turn[] = []
  for (const frame of frames.value) {
    const last = result[result.length - 1]
    if (frame.type === 'meta' || last === undefined) result.push({ userText: null, frames: [frame] })
    else last.frames.push(frame)
  }
  for (let turnIdx = result.length - 1, sentIdx = sentTexts.value.length - 1; turnIdx >= 0 && sentIdx >= 0; turnIdx--, sentIdx--) {
    result[turnIdx].userText = sentTexts.value[sentIdx]
  }
  return result
})

/** 过程帧一族：有回答的轮次把它们折进「过程」折行；rejected/error/answer/proposal 永远直接可见。 */
const QUIET_TYPES: ReadonlySet<string> = new Set(['meta', 'intent', 'status', 'result', 'done'])

function mainFrames(turn: Turn): AssistantFrame[] {
  return turn.frames.filter((frame) => !QUIET_TYPES.has(frame.type))
}

function quietFrames(turn: Turn): AssistantFrame[] {
  return turn.frames.filter((frame) => QUIET_TYPES.has(frame.type))
}

/** 有 answer 才值得把过程收起来；只回了 error 的轮次，过程本身就是全部内容，原样摆出。 */
function collapseQuiet(turn: Turn): boolean {
  return turn.frames.some((frame) => frame.type === 'answer')
}

/** 流式进行中报最新帧名：过程折叠了，但"此刻在等什么"仍要一眼可见。 */
const lastFrame = computed(() => frames.value[frames.value.length - 1] ?? null)

/** 聊天面滚到队尾：值班员永远看最新一帧（测试环境宿主可能没挂，守一下）。 */
const scrollHost = ref<HTMLElement | null>(null)
watch([() => frames.value.length, streaming], async () => {
  await nextTick()
  const el = scrollHost.value
  if (el) el.scrollTop = el.scrollHeight
})

/** 帧里的数字来自 JSON，后端改了口径也不该让渲染层崩掉：能读就按位显示，读不出就原样给出去。 */
function fixed(value: unknown, digits = 2): string {
  const numeric = Number(value)
  return Number.isFinite(numeric) ? numeric.toFixed(digits) : String(value)
}

function proposalText(proposal: ProposalFrame): string {
  return `${proposal.summary || proposal.action}（${proposal.action_id}，约 ${Math.round(Number(proposal.expires_in_seconds) / 60 || 0)} 分钟内有效）`
}

function resultText(frame: AssistantFrame): string {
  const payload: Record<string, unknown> = { ...frame }
  delete payload.type
  return JSON.stringify(payload)
}

function frameText(frame: AssistantFrame): string {
  switch (frame.type) {
    case 'meta':
      return `会话 ${frame.session_id} ｜ LLM ${frame.llm_configured ? '已配置' : '未配置'} ｜ 白名单 ${frame.actions.length} 个动作`
    case 'intent':
      return `识别为 ${frame.action}（${frame.decided_by}，置信 ${fixed(frame.confidence)}）${frame.requires_confirmation ? ' ｜ 需人工确认' : ''}${frame.note ? ` ｜ ${frame.note}` : ''}`
    case 'rejected':
      return `已拒绝执行「${frame.instruction}」：${frame.reason}`
    case 'status':
      return frame.text
    case 'proposal':
      return proposalText(frame)
    case 'result':
      return resultText(frame)
    case 'answer':
      return frame.text
    case 'error':
      return frame.message
    case 'done':
      return `本轮 ${fixed(frame.latency_ms, 0)} ms ｜ 累计被拒 ${frame.rejected_count} 次`
  }
}

async function loadCapabilities(): Promise<void> {
  capabilitiesLoading.value = true
  try {
    capabilities.value = await assistantApi.capabilities()
    capabilitiesError.value = ''
    capabilitiesKind.value = 'none'
  } catch (error) {
    capabilities.value = null
    capabilitiesKind.value = isAssistantUnavailable(error) ? 'unavailable' : isAssistantDisabled(error) ? 'disabled' : 'other'
    capabilitiesError.value = error instanceof Error ? error.message : String(error)
  } finally {
    capabilitiesLoading.value = false
  }
}

/** 点建议条只填句子、不代发：发不发仍是值班员的决定，这页任何时候都不替人按下发送。 */
function useExample(spec: ActionSpecDto): void {
  if (spec.example === '') return
  draft.value = spec.example
}

async function send(): Promise<void> {
  const text = draft.value.trim()
  const request = {
    message: text,
    session_id: sessionId.value ?? undefined,
    reporter: reporter.value.trim() || undefined,
    region_code: regionCode.value.trim() || undefined,
  }
  // 发出前按后端口径挡一次（数值与本文件的正则由跨端门禁对着 assistant_api.py 校验）：
  // 空、超长、区划代码写成小写，都在这一步拦下，而不是等一个 422 回来。
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
 *
 * 先前这个框里按 Enter 只是插一个换行，页面没有任何快捷键说明：值班员打完一句
 * "最近发布了哪些预警"按回车，屏幕上什么都没有（真机：0 条请求、0 帧），
 * 而界面上没有一处提示说"要点发送"。现在按键有反馈、约定写在字数条旁边。
 */
function onMessageKeydown(event: KeyboardEvent): void {
  if (event.shiftKey) return
  event.preventDefault()
  if (streaming.value) {
    // 按钮在流式期间点不动（antd 的 loading 让它接不到点击），键盘这条路必须自己守住：
    // 否则两轮回答会织进同一条时间线，会话号也说不清是哪一轮的。
    streamError.value = '上一轮还在进行中，等它回来再发下一条（要先换行用 Shift+Enter）。'
    return
  }
  void send()
}

async function confirmProposal(proposal: ProposalFrame): Promise<void> {
  decisions[proposal.action_id] = { kind: 'busy' }
  try {
    const result = await assistantApi.confirm({
      session_id: proposal.session_id,
      action_id: proposal.action_id,
      actor: reporter.value.trim() || undefined,
    })
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

/** 后端 confirm() 的四条返回分支（services/assistant.py:332-362）：一分支一句话，不许合并。 */
const CONFIRM_STATUS_LABELS: Record<ConfirmResultDto['status'], string> = {
  executed: '已执行',
  rejected: '已拒绝',
  expired: '已过期',
  failed: '执行失败',
}

/**
 * 执行类动作的闭环：把能跟进的号带回来。
 *
 * `/confirm` 的后端回执其实整份结果都带着（`assistant.py` 的 `result`：上报回执里有链路、
 * 可能还有预警与核签工单），但页面上此前只剩一句"已执行 create.report"——
 * 值班员经助手报上去一条险情，手里一个号都没有，要去别的页翻着找自己刚报的东西。
 */
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
  if (decision.kind === 'dismissed') return '仅本页放弃：后端没有取消接口，该动作仍在会话内，确认或过期才会失效。'
  if (decision.kind === 'error') return `确认请求失败：${decision.message}`
  const result = decision.result
  const label = CONFIRM_STATUS_LABELS[result.status] ?? result.status
  if (result.status === 'executed') return `${label} ${result.action ?? ''}（后端回执 ${result.status}）${executionHandles(result)}`.trim()
  return `${label}：${result.reason ?? result.error ?? '后端未给出原因'}`
}

onMounted(() => void loadCapabilities())
</script>

<template>
  <div class="assistant">
    <PageHero title="语义交互" badge="对话" caption="对话 → 白名单动作 → 人工确认。">
      <template #actions>
        <a-tag v-if="capabilitiesError" color="red" data-testid="caps-state">读不到能力面</a-tag>
        <a-tag v-else-if="capabilities && !llmConfigured" color="orange" data-testid="caps-state">语义服务未配置</a-tag>
        <a-tag v-else-if="capabilities" color="green" data-testid="caps-state">能力面已读取</a-tag>
        <a-button size="small" :loading="capabilitiesLoading" data-testid="caps-refresh" @click="loadCapabilities">重读能力面</a-button>
        <a-button size="small" data-testid="open-report" @click="reportOpen = true">人工上报</a-button>
      </template>
    </PageHero>

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
      <div ref="scrollHost" class="chat__scroll" data-testid="timeline">
        <!-- 空态：欢迎 + 建议（点芯片只填句子，发不发由人） -->
        <div v-if="!frames.length && !streaming" class="chat__empty" data-testid="timeline-empty">
          <div class="chat__greeting">有什么可以帮你？</div>
          <p class="chat__hint">查询预警与态势、发起演练、上报险情。执行类动作先提案，经你确认才落地。</p>
          <!--
            这排芯片此前是 <a-tag>：看着像建议，点下去什么都没有。点一下把该动作自己的示例句填进
            输入框，发不发仍由值班员决定（示例句来自能力面，词表口径只在后端一处维护）。
            「需确认」「缺 …」是量化出来的事实，降为小字缀在标题后，不再满屏实色。
          -->
          <div v-if="capabilities" class="caps" data-testid="caps-actions">
            <button
              v-for="spec in capabilities.actions"
              :key="spec.action"
              type="button"
              class="caps__chip"
              :class="spec.available ? 'is-available' : 'is-missing'"
              :data-testid="`action-${spec.action}`"
              :disabled="spec.example === ''"
              :title="spec.example === '' ? '能力面没给这条动作的示例句' : `填入：${spec.example}`"
              @click="useExample(spec)"
            >
              {{ spec.title }}<span v-if="spec.requires_confirmation" class="caps__chip__note">需确认</span><span
                v-if="!spec.available"
                class="caps__chip__note is-warn"
              >缺 {{ spec.missing.join('、') }}</span>
            </button>
          </div>
          <div v-if="capabilities" class="chat__ttl" data-testid="caps-ttl">
            会话保留 {{ Math.round(capabilities.session_ttl_seconds / 60) }} 分钟
            ｜ 单会话最多 {{ capabilities.max_pending_actions }} 个待确认动作
            ｜ 解析腿 <span v-if="!capabilities.semantic_parser" class="pill-dashed">缺席</span><span v-else>在位</span>
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
                  </div>
                  <a-space size="small">
                    <a-button
                      type="primary"
                      size="small"
                      :disabled="!!decisions[frame.action_id]"
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
/* 布局是通用助手的形状：中列聊天面（滚动区 + 常驻输入区），全部颜色来自令牌。 */
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
.chat__scroll {
  flex: 1;
  overflow-y: auto;
  padding: var(--aeg-space-s6) var(--aeg-space-s6) var(--aeg-space-s5);
  display: flex;
  flex-direction: column;
  gap: var(--aeg-space-s6);
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
 * 芯片改中性：可用的就是一块安静的表面，点上去才知道；「需确认」「缺 …」降为小字缀注。
 * 缺依赖那一档保住红字——它是"量出来的状态"，不是装饰。
 */
.caps__chip {
  display: inline-flex;
  align-items: center;
  gap: var(--aeg-space-s1);
  min-height: 26px;
  padding: 3px var(--aeg-space-s3);
  border: 1px solid var(--aeg-color-border);
  /* 999px 写字面而不写 var(--aeg-radius-pill)：uiTheme 的芯片形状判据（T13 才改口径）钉着这个字面，值本就相同 */
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
.caps__chip.is-missing {
  border-style: dashed;
  background: transparent;
  color: var(--aeg-color-text-secondary);
}
.caps__chip.is-missing:hover:not(:disabled) {
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
/* 过程折行：一行小字，点开才是帧记录 */
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
