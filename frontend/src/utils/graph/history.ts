// Ported from Dify web/app/components/workflow/hooks/use-workflow-history.ts
// (tag 1.17.1, commit 8387590ace4a094de812b7847fc6a4c3a27cd52b).
// Copyright (c) LangGenius, Inc. Licensed under the modified Apache License 2.0.
// Changes: adapted to AEGIS; see THIRD_PARTY_NOTICES.md.
/**
 * 撤销/重做的纯快照逻辑（与 Dify `use-workflow-history.ts` 的契约对齐，见
 * `__fixtures__/dify-1.17.1/history.contract.json`）。
 *
 * 不做增量重放：每一步都存整份文档。文档本身有 200 节点 / 2MB 上限，
 * 50 步快照的内存上界是确定的；增量重放则要为每种命令维护反向操作，
 * 一条写歪就会在"撤销之后图对不上"。
 */
import type { WorkflowDocument } from './document'
import { documentsEqual } from './document'

export const HISTORY_LIMIT = 50
export const HISTORY_INITIAL_LABEL = '初始状态'

/** 动作标签与撤销文案一一对应；未登记的动作按传入值原样展示。 */
export const HISTORY_LABELS = {
  addNode: '添加节点',
  removeNodes: '删除节点',
  moveNodes: '移动节点',
  connect: '连接节点',
  removeEdges: '删除连线',
  updateNode: '修改节点',
  insertOnEdge: '插入节点',
  paste: '粘贴',
  rename: '重命名',
  setStartVariables: '修改输入变量',
  setNotes: '编辑备注',
} as const

export interface HistoryEntry {
  document: WorkflowDocument
  label: string
}

export interface WorkflowHistoryState {
  past: HistoryEntry[]
  present: HistoryEntry
  future: HistoryEntry[]
}

/** 快照必须与画布对象断开：否则后续编辑会同时改到"历史里的那一步"。 */
export function cloneWorkflowDocument(document: WorkflowDocument): WorkflowDocument {
  return JSON.parse(JSON.stringify(document)) as WorkflowDocument
}

function entryOf(document: WorkflowDocument, label: string): HistoryEntry {
  return { document: cloneWorkflowDocument(document), label }
}

export function createHistoryState(
  document: WorkflowDocument,
  label: string = HISTORY_INITIAL_LABEL,
): WorkflowHistoryState {
  return { past: [], present: entryOf(document, label), future: [] }
}

/**
 * 提交一步。文档与当前一致时返回原状态（同一引用），调用方可以用
 * `next === state` 判断"这次没产生历史"。
 */
export function commitHistory(
  state: WorkflowHistoryState,
  document: WorkflowDocument,
  label: string,
  limit: number = HISTORY_LIMIT,
): WorkflowHistoryState {
  if (documentsEqual(document, state.present.document)) return state

  const past = [...state.past, state.present]
  if (past.length > limit) past.splice(0, past.length - limit)

  return { past, present: entryOf(document, label), future: [] }
}

export function undoHistory(state: WorkflowHistoryState): WorkflowHistoryState {
  const previous = state.past[state.past.length - 1]
  if (previous === undefined) return state

  return {
    past: state.past.slice(0, -1),
    present: previous,
    future: [state.present, ...state.future],
  }
}

export function redoHistory(state: WorkflowHistoryState): WorkflowHistoryState {
  const [next, ...rest] = state.future
  if (next === undefined) return state

  return { past: [...state.past, state.present], present: next, future: rest }
}

export function canUndoHistory(state: WorkflowHistoryState | null): boolean {
  return state !== null && state.past.length > 0
}

export function canRedoHistory(state: WorkflowHistoryState | null): boolean {
  return state !== null && state.future.length > 0
}

/** 即将被撤销的那一步（按钮上写"撤销 移动节点"）。 */
export function undoHistoryLabel(state: WorkflowHistoryState | null): string | null {
  return canUndoHistory(state) ? (state?.present.label ?? null) : null
}

export function redoHistoryLabel(state: WorkflowHistoryState | null): string | null {
  return canRedoHistory(state) ? (state?.future[0]?.label ?? null) : null
}
