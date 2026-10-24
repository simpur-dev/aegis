// Ported from Dify web/app/components/workflow/shortcuts (hotkey matching)
// (tag 1.17.1, commit 8387590ace4a094de812b7847fc6a4c3a27cd52b).
// Copyright (c) LangGenius, Inc. Licensed under the modified Apache License 2.0.
// Changes: adapted to AEGIS; see THIRD_PARTY_NOTICES.md.

/**
 * 按键描述串（`Mod+Shift+Z` / `Shift+1` / `?`）→ 键盘事件。
 *
 * 上游用 `@tanstack/react-hotkeys`；AEGIS 只保留画布需要的匹配语义：
 * - `Mod` 在 Windows/Linux 是 Ctrl、macOS 是 Meta（两者任一都算，便于跨平台配置共用）；
 * - 没写的修饰键必须**没按**，否则 `Mod+Z` 会在 `Mod+Shift+Z` 时先抢走；
 * - Shift 数字键的 `event.key` 是 `!`、`@` 等符号，所以数字优先按 `event.code` 判。
 */
import type { CanvasShortcut, Position } from '@/utils/graph'
import { canvasShortcut } from '@/utils/graph'

import { getKeyboardMovement } from '../editor/keyboard-movement'
import { WORKFLOW_CANVAS_SHORTCUTS, type WorkflowCanvasShortcutKey } from './definitions'

export interface WorkflowKeyEventLike {
  key: string
  code?: string
  ctrlKey?: boolean
  metaKey?: boolean
  altKey?: boolean
  shiftKey?: boolean
}

export type ResolvedWorkflowKey =
  | { kind: 'shortcut'; id: WorkflowCanvasShortcutKey }
  | { kind: 'canvas'; action: CanvasShortcut }
  | { kind: 'move'; delta: Position }

const SHIFTED_DIGITS: Readonly<Record<string, string>> = {
  '!': '1',
  '@': '2',
  '#': '3',
  $: '4',
  '%': '5',
  '^': '6',
  '&': '7',
  '*': '8',
  '(': '9',
  ')': '0',
}

function keyTokenMatches(token: string, event: WorkflowKeyEventLike): boolean {
  if (token === '?') return event.key === '?' || event.code === 'Slash'
  if (token === '-') return event.key === '-' || event.key === '_' || event.code === 'Minus'
  if (token === '=') return event.key === '=' || event.key === '+' || event.code === 'Equal'
  if (/^[0-9]$/.test(token)) {
    return event.code === `Digit${token}` || event.key === token || SHIFTED_DIGITS[event.key] === token
  }
  if (/^[a-z]$/i.test(token)) {
    return event.key.toUpperCase() === token.toUpperCase() || event.code === `Key${token.toUpperCase()}`
  }
  return event.key === token
}

export function matchesWorkflowHotkey(hotkey: string, event: WorkflowKeyEventLike): boolean {
  const tokens = hotkey.split('+').filter((token) => token !== '')
  const keyToken = tokens.pop()
  if (keyToken === undefined) return false

  const mods = new Set(tokens)
  const ctrl = event.ctrlKey === true
  const meta = event.metaKey === true
  const alt = event.altKey === true
  const shift = event.shiftKey === true

  if (mods.has('Mod') ? !(ctrl || meta) : ctrl || meta) return false
  if (mods.has('Ctrl') && !ctrl) return false
  if (mods.has('Meta') && !meta) return false
  if (mods.has('Alt') !== alt) return false

  /* '?' 只能靠 Shift 打出来，但快捷键表里不写 Shift；按住 Shift 也该能打开帮助。 */
  if (mods.has('Shift') ? !shift : keyToken !== '?' && shift) return false

  return keyTokenMatches(keyToken, event)
}

export function findWorkflowShortcut(event: WorkflowKeyEventLike): WorkflowCanvasShortcutKey | null {
  for (const definition of Object.values(WORKFLOW_CANVAS_SHORTCUTS)) {
    if (!('hotkeys' in definition)) continue
    if (definition.hotkeys.some((hotkey) => matchesWorkflowHotkey(hotkey, event))) return definition.id
  }
  return null
}

/**
 * 表格优先、画布单键兜底、最后是方向键微移。
 * 返回 null 表示这键不归画布管（留给浏览器/输入框）。
 */
export function resolveWorkflowKey(event: WorkflowKeyEventLike): ResolvedWorkflowKey | null {
  const shortcut = findWorkflowShortcut(event)
  if (shortcut !== null) return { kind: 'shortcut', id: shortcut }

  const action = canvasShortcut(event.key, event.ctrlKey === true || event.metaKey === true || event.altKey === true)
  if (action !== null) return { kind: 'canvas', action }

  const delta = getKeyboardMovement(event)
  if (delta !== null) return { kind: 'move', delta }

  return null
}

/** 输入框/可编辑区域里除了显式全局键（保存）以外都不抢。 */
export function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  if (target.isContentEditable) return true
  /* happy-dom / 部分 WebView 不实现 isContentEditable，属性形式仍要认。 */
  const editable = target.getAttribute('contenteditable')
  if (editable === '' || editable === 'true') return true
  return ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)
}
