<script setup lang="ts">
/**
 * 智能助手页（完善计划批次 B3）：语义交互的前端落点。
 *
 * 三条设计约束都来自后端事实，不是审美：
 * 1. **帧序列是过程事实**。后端刻意不做 token 级伪流式（`api/assistant_api.py:1-8`），
 *    给出的每一帧都是"哪一步在等什么"，所以这里按帧渲染时间线，不拼成打字机效果；
 * 2. **执行类动作只能经 `/confirm` 落地**（`services/assistant.py:332-362`）。
 *    「放弃」只在这页把卡片收起来——后端没有取消接口，页面就照实说"仅在本地隐藏"，
 *    不许把"我没点"显示成"已经撤销"；
 * 3. **能力面读不到 ≠ 一切正常**。`llm_configured=false` 明说"语义服务未配置：仅规则词表可用"，
 *    接口失败明说"读不到"，503（腿没装配）与 404（出口未启用）是两句话
 *    （`assistant_api.py:57-63` 与 `app.py:538-541`）。
 */
import { message } from 'ant-design-vue'
import { computed, onMounted, ref } from 'vue'

import type { ActionSpecDto, AssistantFrame, CapabilitiesDto, ConfirmResultDto, ProposalFrame } from '@/api/assistant'
import { assistantApi, CHAT_LIMITS, isAssistantDisabled, isAssistantUnavailable, preflightChat } from '@/api/assistant'
import ReportForm from '@/components/reports/ReportForm.vue'
import { useAssistantSession } from '@/composables/useAssistantSession'

const capabilities = ref<CapabilitiesDto | null>(null)
const capabilitiesError = ref('')
const capabilitiesKind = ref<'none' | 'unavailable' | 'disabled' | 'other'>('none')
const capabilitiesLoading = ref(false)

// 时间线与会话号在组件外面：换菜单再回来还得接得上（见 useAssistantSession 的说明）
const { frames, sessionId, decisions, clearTimeline } = useAssistantSession()
const reporter = ref('值班员')
const regionCode = ref('')
const draft = ref('')
const streaming = ref(false)
const streamError = ref('')
const reportOpen = ref(false)

const llmConfigured = computed(() => capabilities.value?.llm_configured === true)
const proposals = computed(() => frames.value.filter((frame): frame is ProposalFrame => frame.type === 'proposal'))

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
    for await (const frame of assistantApi.chat(request)) {
      if (frame.type === 'meta') sessionId.value = frame.session_id
      frames.value.push(frame)
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
    <a-card size="small" title="语义交互（对话 → 白名单动作 → 人工确认）">
      <template #extra>
        <a-space size="small">
          <a-tag v-if="capabilitiesError" color="red" data-testid="caps-state">读不到能力面</a-tag>
          <a-tag v-else-if="capabilities && !llmConfigured" color="orange" data-testid="caps-state">语义服务未配置</a-tag>
          <a-tag v-else-if="capabilities" color="green" data-testid="caps-state">能力面已读取</a-tag>
          <a-button size="small" :loading="capabilitiesLoading" data-testid="caps-refresh" @click="loadCapabilities">重读能力面</a-button>
          <a-button size="small" data-testid="open-report" @click="reportOpen = true">人工上报</a-button>
        </a-space>
      </template>

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

      <div v-if="capabilities" class="caps" data-testid="caps-actions">
        <!--
          这排芯片此前是 <a-tag>：看着像建议，点下去什么都没有。12 个动作都 spelled 出来，
          却要值班员自己猜"要打出哪个词才算问到了它"——本机没有 LLM 时意图只走词表规则，
          猜错的代价是一句"未能识别意图"。现在点一下就把它自己的示例句填进输入框，
          发不发仍由值班员决定（示例句来自能力面，词表口径只在后端一处维护）。
        -->
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
          {{ spec.title }}{{ spec.requires_confirmation ? '（需确认）' : '' }}{{ spec.available ? '' : `：缺 ${spec.missing.join('、')}` }}
        </button>
        <div class="muted" data-testid="caps-ttl">
          会话保留 {{ Math.round(capabilities.session_ttl_seconds / 60) }} 分钟 ｜ 单会话最多 {{ capabilities.max_pending_actions }} 个待确认动作 ｜
          解析腿 {{ capabilities.semantic_parser ? '在位' : '缺席' }}
        </div>
      </div>
    </a-card>

    <a-card size="small" title="对话" style="margin-top: 12px">
      <a-space wrap style="margin-bottom: 8px">
        <a-input v-model:value="reporter" :maxlength="CHAT_LIMITS.reporter.max" style="width: 160px" placeholder="上报人名义" data-testid="field-reporter" />
        <a-input
          v-model:value="regionCode"
          :maxlength="24"
          style="width: 160px"
          placeholder="区划代码（6–24 位大写字母数字）"
          data-testid="field-region"
        />
        <span class="muted" data-testid="session-id">会话 {{ sessionId ?? '未开始（由后端 meta 帧给出）' }}</span>
      </a-space>
      <a-textarea
        v-model:value="draft"
        :rows="3"
        :maxlength="CHAT_LIMITS.message.max"
        placeholder="如：最近发布了哪些预警 / 帮我上报 540121 沟道泥位抬升"
        data-testid="field-message"
        @keydown="onMessageKeydown"
      />
      <!-- 计数自己渲染而不是用 antd 的 show-count：那一个的文本挂在 data-count 上，界面上看得见却测不着 -->
      <div class="muted" data-testid="char-count">
        {{ draft.length }} / {{ CHAT_LIMITS.message.max }} 字 ｜ Enter 发送，Shift+Enter 换行
      </div>
      <a-space style="margin-top: 8px">
        <a-button type="primary" :loading="streaming" data-testid="send" @click="send">发送</a-button>
        <a-button data-testid="clear" @click="clearTimeline">清空时间线</a-button>
      </a-space>

      <p v-if="streamError" class="error" data-testid="stream-error">{{ streamError }}</p>

      <div class="timeline" data-testid="timeline">
        <div
          v-for="(frame, index) in frames"
          :key="index"
          class="frame"
          :class="{ refusal: frame.type === 'rejected' }"
          :data-testid="`frame-${frame.type}`"
        >
          <span class="kind">{{ frame.type }}</span>
          <span class="body">{{ frameText(frame) }}</span>
        </div>
        <div v-if="!frames.length && !streaming" class="muted" data-testid="timeline-empty">
          还没有发起对话。发送后每一帧（meta/intent/status/result/answer/rejected/done）都会原样留在这里。
        </div>
      </div>

      <div v-for="proposal in proposals" :key="proposal.action_id" class="proposal" :data-testid="`proposal-${proposal.action_id}`">
        <div class="proposal-head">
          待确认动作：{{ proposal.action }} ｜ {{ proposalText(proposal) }}
        </div>
        <a-space size="small">
          <a-button
            type="primary"
            size="small"
            :disabled="!!decisions[proposal.action_id]"
            :loading="decisions[proposal.action_id]?.kind === 'busy'"
            :data-testid="`confirm-${proposal.action_id}`"
            @click="confirmProposal(proposal)"
          >
            确认
          </a-button>
          <a-button
            size="small"
            :disabled="!!decisions[proposal.action_id]"
            :data-testid="`dismiss-${proposal.action_id}`"
            @click="dismissProposal(proposal)"
          >
            放弃
          </a-button>
        </a-space>
        <div v-if="decisionText(proposal.action_id)" class="decision" data-testid="decision">{{ decisionText(proposal.action_id) }}</div>
      </div>
    </a-card>

    <a-modal v-model:open="reportOpen" title="人工上报（与监测事件同一条链路）" :footer="null" width="720px" data-testid="report-modal">
      <ReportForm :initial-region-code="regionCode" :initial-reporter="reporter" />
    </a-modal>
  </div>
</template>

<style scoped>
.muted {
  color: rgba(0, 0, 0, 0.45);
  font-size: 12px;
}
.caps {
  margin-top: 8px;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.caps__chip {
  padding: 2px 8px;
  border: 1px solid #b7eb8f;
  border-radius: 4px;
  background: #f6ffed;
  color: #389e0d;
  font-size: 12px;
  cursor: pointer;
}
.caps__chip.is-missing {
  border-color: #ffa39e;
  background: #fff1f0;
  color: #cf1322;
}
.caps__chip:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}
.error {
  color: #cf1322;
  font-size: 13px;
  margin-top: 8px;
}
.timeline {
  margin-top: 10px;
  border-top: 1px solid rgba(0, 0, 0, 0.08);
  padding-top: 8px;
}
.frame {
  display: flex;
  gap: 8px;
  padding: 3px 0;
  font-size: 13px;
}
.frame .kind {
  color: rgba(0, 0, 0, 0.45);
  font-family: 'Consolas', monospace;
  min-width: 76px;
}
.frame.refusal .body {
  color: #cf1322;
  font-weight: 600;
}
.proposal {
  margin-top: 10px;
  border: 1px solid #ffd591;
  background: #fffbe6;
  padding: 8px 10px;
}
.proposal-head {
  font-size: 13px;
  margin-bottom: 6px;
}
.decision {
  margin-top: 6px;
  font-size: 12px;
  color: rgba(0, 0, 0, 0.65);
}
</style>
