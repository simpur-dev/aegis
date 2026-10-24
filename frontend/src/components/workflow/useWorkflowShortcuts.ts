// Ported from Dify web/app/components/workflow/hooks/use-workflow-shortcuts.ts
// (tag 1.17.1, commit 8387590ace4a094de812b7847fc6a4c3a27cd52b).
// Copyright (c) LangGenius, Inc. Licensed under the modified Apache License 2.0.
// Changes: adapted to AEGIS; see THIRD_PARTY_NOTICES.md.

/**
 * 全局键盘分发：表格里的动作优先，画布单键（1/0/+/-）兜底，最后才是方向键微移。
 *
 * 输入框守卫放在 `resolveWorkflowKey` 之前：在名称框里按 Ctrl+Z 必须撤销文字，
 * 不能把画布上一步撤了；唯一的例外是保存——那是全局动作，输入时也常按。
 */
import { onBeforeUnmount, onMounted } from 'vue'

import type { Position } from '@/utils/graph'

import type { WorkflowCanvasShortcutKey } from './shortcuts/definitions'
import { isTypingTarget, matchesWorkflowHotkey, resolveWorkflowKey } from './shortcuts/match'

export interface WorkflowShortcutActions {
  save: () => void | Promise<void>
  deleteSelection: () => boolean
  copy: () => Promise<boolean>
  cut: () => Promise<boolean>
  paste: () => Promise<boolean>
  duplicate: () => boolean
  undo: () => boolean
  redo: () => boolean
  setMode: (mode: 'pointer' | 'hand') => void
  organize: () => void
  zoomToFit: () => void
  resetZoom: () => void
  zoomTo100: () => void
  zoomTo50: () => void
  zoomIn: () => void
  zoomOut: () => void
  help: () => void
  locate: () => void
  moveSelection: (delta: Position) => boolean
}

export function useWorkflowShortcuts(actions: WorkflowShortcutActions) {
  function runShortcut(id: WorkflowCanvasShortcutKey): void {
    switch (id) {
      case 'workflow.delete':
        actions.deleteSelection()
        return
      case 'workflow.copy':
        void actions.copy()
        return
      case 'workflow.cut':
        void actions.cut()
        return
      case 'workflow.paste':
        void actions.paste()
        return
      case 'workflow.duplicate':
        actions.duplicate()
        return
      case 'workflow.undo':
        actions.undo()
        return
      case 'workflow.redo':
        actions.redo()
        return
      case 'workflow.pointer-mode':
        actions.setMode('pointer')
        return
      case 'workflow.hand-mode':
        actions.setMode('hand')
        return
      case 'workflow.organize':
        actions.organize()
        return
      case 'workflow.zoom-to-fit':
        actions.zoomToFit()
        return
      case 'workflow.zoom-to-100':
        actions.zoomTo100()
        return
      case 'workflow.zoom-to-50':
        actions.zoomTo50()
        return
      case 'workflow.zoom-in':
        actions.zoomIn()
        return
      case 'workflow.zoom-out':
        actions.zoomOut()
        return
      case 'workflow.save':
        void actions.save()
        return
      case 'workflow.help':
        actions.help()
        return
      case 'workflow.locate':
        actions.locate()
        return
      default:
        return
    }
  }

  function handleKeydown(event: KeyboardEvent): void {
    if (event.defaultPrevented) return

    const typing = isTypingTarget(event.target)
    if (typing && !matchesWorkflowHotkey('Mod+S', event)) return

    const resolved = resolveWorkflowKey(event)
    if (resolved === null) return
    event.preventDefault()

    if (resolved.kind === 'shortcut') {
      runShortcut(resolved.id)
      return
    }
    if (resolved.kind === 'move') {
      actions.moveSelection(resolved.delta)
      return
    }
    if (resolved.action === 'fit') actions.zoomToFit()
    else if (resolved.action === 'reset') actions.resetZoom()
    else if (resolved.action === 'zoom-in') actions.zoomIn()
    else actions.zoomOut()
  }

  onMounted(() => window.addEventListener('keydown', handleKeydown))
  onBeforeUnmount(() => window.removeEventListener('keydown', handleKeydown))

  return { handleKeydown }
}
