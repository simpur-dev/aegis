// Ported from Dify web/app/components/workflow/utils/edge.ts
// (tag 1.17.1, commit 8387590ace4a094de812b7847fc6a4c3a27cd52b).
// Copyright (c) LangGenius, Inc. Licensed under the modified Apache License 2.0.
// Changes: adapted to AEGIS; see THIRD_PARTY_NOTICES.md.
import type { NodeState } from '@/utils/graph'

const EDGE_COLOR_NORMAL = 'var(--aeg-color-editor-edge)'
const EDGE_COLOR_RUNNING = 'var(--aeg-color-editor-state-running)'
const EDGE_COLOR_AWAITING = 'var(--aeg-color-editor-state-awaiting)'
const EDGE_COLOR_SUCCESS = 'var(--aeg-color-status-success)'
const EDGE_COLOR_DANGER = 'var(--aeg-color-status-danger)'

/**
 * 上游按 `NodeRunningStatus` 给连线取色；AEGIS 的状态机没有 Exception 档，
 * 由 `timeout` 承接（等价于上游的异常失败），故不再单设 exception 分支。
 * `isFailBranch` 只影响 running：人工确认后的失败分支仍要按危险色提示。
 */
export function getEdgeColor(state?: NodeState | null, isFailBranch = false): string {
  if (state === 'succeeded' || state === 'degraded' || state === 'bypassed') return EDGE_COLOR_SUCCESS
  if (state === 'failed' || state === 'timeout') return EDGE_COLOR_DANGER
  if (state === 'awaiting_human') return EDGE_COLOR_AWAITING
  if (state === 'running') return isFailBranch ? EDGE_COLOR_DANGER : EDGE_COLOR_RUNNING
  return EDGE_COLOR_NORMAL
}
