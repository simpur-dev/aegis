/**
 * 帧 → 一句话。纯函数，不依赖组件状态，方便单测。
 *
 * 为什么要抽出来：这段 switch 原来长在页面组件的 `<script setup>` 里，页面一拆两处（页面 + 抽屉）
 * 就得复制一遍；而它恰恰是"后端加了新帧型、前端忘了加分支"最容易出事的地方——漏一个 case
 * 就返回 undefined，界面上出现一句空白，控制台什么都不报。
 */

import type { AssistantFrame, ProposalFrame } from '@/api/assistant'

/** 过程帧：有回答的轮次把它们折进「过程 ｜ N 帧」；rejected/error/answer/proposal 永远直接可见。 */
export const QUIET_TYPES: ReadonlySet<string> = new Set(['meta', 'intent', 'status', 'result', 'done'])

/** 帧里的数字来自 JSON，后端改了口径也不该让渲染层崩掉：能读就按位显示，读不出就原样给出去。 */
export function fixed(value: unknown, digits = 2): string {
  const numeric = Number(value)
  return Number.isFinite(numeric) ? numeric.toFixed(digits) : String(value)
}

export function proposalText(proposal: ProposalFrame): string {
  const minutes = Math.round(Number(proposal.expires_in_seconds) / 60 || 0)
  return `${proposal.summary || proposal.action}（${proposal.action_id}，约 ${minutes} 分钟内有效）`
}

export function resultText(frame: AssistantFrame): string {
  const payload: Record<string, unknown> = { ...frame }
  delete payload.type
  return JSON.stringify(payload)
}

export function frameText(frame: AssistantFrame): string {
  switch (frame.type) {
    case 'meta': {
      const actions = Array.isArray(frame.actions) ? frame.actions : []
      const seat = typeof frame.operator_id === 'string' && frame.operator_id !== '' ? ` ｜ 席位 ${frame.operator_id}` : ''
      return `会话 ${frame.session_id} ｜ LLM ${frame.llm_configured ? '已配置' : '未配置'} ｜ 动作 ${actions.length} 个${seat}`
    }
    case 'intent': {
      const needsConfirm = frame.requires_confirmation === true ? ' ｜ 需人工确认' : ''
      const note = typeof frame.note === 'string' && frame.note !== '' ? ` ｜ ${frame.note}` : ''
      return `识别为 ${String(frame.action)}（${String(frame.decided_by)}，置信 ${fixed(frame.confidence)}）${needsConfirm}${note}`
    }
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
    default:
      // 后端加了前端还不认识的帧型：如实说"这一帧读不懂"，而不是渲染一句空白。
      return `未识别的帧：${JSON.stringify(frame).slice(0, 200)}`
  }
}
