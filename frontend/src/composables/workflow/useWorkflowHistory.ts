// Ported from Dify web/app/components/workflow/hooks/use-workflow-history.ts
// (tag 1.17.1, commit 8387590ace4a094de812b7847fc6a4c3a27cd52b).
// Copyright (c) LangGenius, Inc. Licensed under the modified Apache License 2.0.
// Changes: adapted to AEGIS; see THIRD_PARTY_NOTICES.md.
/**
 * 画布撤销/重做的状态壳：历史本身是纯函数（`utils/graph/history.ts`），
 * 这里只负责在"改文档"前后取快照、以及把撤销目标写回文档层。
 *
 * 刻意不直接依赖 Pinia：文档层从 props 注入，既能在组件里接 document store，
 * 也能在单测里用两个函数就搭出完整画布行为。
 */
import { computed, ref } from 'vue'

import {
  canRedoHistory,
  canUndoHistory,
  cloneWorkflowDocument,
  commitHistory,
  createHistoryState,
  HISTORY_LIMIT,
  redoHistory,
  redoHistoryLabel,
  undoHistory,
  undoHistoryLabel,
  type WorkflowHistoryState,
} from '@/utils/graph/history'
import type { WorkflowDocument } from '@/utils/graph/document'

export interface WorkflowHistoryOptions {
  /** 当前文档；加载中返回 null。 */
  read: () => WorkflowDocument | null
  /** 把历史快照写回文档层（自动保存由文档层自己触发）。 */
  write: (document: WorkflowDocument) => void | Promise<void>
  limit?: number
}

export function useWorkflowHistory(options: WorkflowHistoryOptions) {
  const limit = options.limit ?? HISTORY_LIMIT
  const state = ref<WorkflowHistoryState | null>(null)

  const canUndo = computed(() => canUndoHistory(state.value))
  const canRedo = computed(() => canRedoHistory(state.value))
  const undoLabel = computed(() => undoHistoryLabel(state.value))
  const redoLabel = computed(() => redoHistoryLabel(state.value))
  const pastCount = computed(() => state.value?.past.length ?? 0)
  const futureCount = computed(() => state.value?.future.length ?? 0)

  /** 换了一份文档（首次加载、恢复版本、冲突后重载）时重开历史。 */
  function reset(document: WorkflowDocument | null = options.read()): void {
    state.value = document === null ? null : createHistoryState(document)
  }

  function syncBaseline(): boolean {
    if (state.value !== null) return true
    const document = options.read()
    if (document === null) return false
    state.value = createHistoryState(document)
    return true
  }

  function commitCurrent(label: string): boolean {
    if (!syncBaseline()) return false
    const document = options.read()
    if (document === null || state.value === null) return false

    const next = commitHistory(state.value, document, label, limit)
    const changed = next !== state.value
    state.value = next
    return changed
  }

  /**
   * 包住一次文档修改：先确保基线是"改之前"的那份，再统一提交一步。
   * 拖动这类连续动作只在落点调用一次，因此天然合并成一条历史。
   */
  function record(label: string, mutate: () => void): boolean {
    if (!syncBaseline()) return false
    mutate()
    return commitCurrent(label)
  }

  async function recordAsync(label: string, mutate: () => Promise<void>): Promise<boolean> {
    if (!syncBaseline()) return false
    await mutate()
    return commitCurrent(label)
  }

  async function undo(): Promise<boolean> {
    if (state.value === null) return false
    const next = undoHistory(state.value)
    if (next === state.value) return false

    await options.write(cloneWorkflowDocument(next.present.document))
    state.value = next
    return true
  }

  async function redo(): Promise<boolean> {
    if (state.value === null) return false
    const next = redoHistory(state.value)
    if (next === state.value) return false

    await options.write(cloneWorkflowDocument(next.present.document))
    state.value = next
    return true
  }

  return {
    state,
    canUndo,
    canRedo,
    undoLabel,
    redoLabel,
    pastCount,
    futureCount,
    reset,
    record,
    recordAsync,
    commitCurrent,
    undo,
    redo,
  }
}
