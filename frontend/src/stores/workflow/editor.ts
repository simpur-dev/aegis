/**
 * 易失的界面状态：当前模式、选中节点、页面级错误与忙碌标记。
 *
 * 这里刻意不持有文档——模式切换的职责在视图/运行 store，选择只认 node_id，
 * 文档怎么变都不会把"选中的是哪个节点"写进可序列化模型里。
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'

import { describeFailure } from './shared'

/** 与 plan §4.3 同形；instance 模式下画布只读，写入面留给草稿模式。 */
export type EditorMode =
  { kind: 'draft' } | { kind: 'version'; workflowId: string } | { kind: 'instance'; instanceId: string; readOnly: true }

function initialMode(): EditorMode {
  return { kind: 'draft' }
}

export function createEditorStore() {
  return defineStore('workflow.editor', () => {
    const mode = ref<EditorMode>(initialMode())
    const selectedNodeId = ref<string | null>(null)
    const selectedNoteId = ref<string | null>(null)
    const error = ref<string | null>(null)
    const loading = ref(false)

    function select(nodeId: string | null): void {
      selectedNodeId.value = nodeId
      selectedNoteId.value = null
    }

    function selectNote(noteId: string | null): void {
      selectedNoteId.value = noteId
      selectedNodeId.value = null
    }

    function setMode(next: EditorMode): void {
      mode.value = next
    }

    function setError(message: string | null): void {
      error.value = message
    }

    function clearError(): void {
      error.value = null
    }

    async function runAction(action: () => Promise<void>, label: string): Promise<boolean> {
      loading.value = true
      error.value = null
      try {
        await action()
        return true
      } catch (caught) {
        error.value = `${label}：${describeFailure(caught)}`
        return false
      } finally {
        loading.value = false
      }
    }

    return {
      mode,
      selectedNodeId,
      selectedNoteId,
      error,
      loading,
      select,
      selectNote,
      setMode,
      setError,
      clearError,
      runAction,
    }
  })
}

export const useEditorStore = createEditorStore()
export type EditorStore = ReturnType<typeof useEditorStore>
