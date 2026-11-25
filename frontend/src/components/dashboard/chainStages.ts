import type { StageResult } from '@/api/types'

export const stageLabels: Record<string, string> = {
  perceive: '感知',
  assess: '研判',
  plan: '决策',
  execute: '执行',
  feedback: '反馈',
}

const modeLabels: Record<StageResult['mode'], string> = {
  agent: '智能体',
  local: '平台兜底',
  hybrid: '混合',
  skipped: '跳过',
}

export function stageLabel(stage: StageResult): string {
  return stageLabels[stage.name] ?? stage.name
}

export function stageTitle(stage: StageResult): string {
  const result = stage.ok ? '' : ' · 失败'
  return `${stageLabel(stage)} · ${modeLabels[stage.mode]}${result} · ${stage.latency_ms}ms${stage.note ? ` · ${stage.note}` : ''}`
}
