// Ported from Dify web/app/components/workflow/utils/keyboard-movement.ts
// (tag 1.17.1, commit 8387590ace4a094de812b7847fc6a4c3a27cd52b).
// Copyright (c) LangGenius, Inc. Licensed under the modified Apache License 2.0.
// Changes: adapted to AEGIS; see THIRD_PARTY_NOTICES.md.

/** React 的 KeyboardEvent 的四个修饰键在 DOM 键盘事件里同名；这里只要这几格。 */
export interface KeyboardMovementInput {
  key: string
  shiftKey?: boolean
  altKey?: boolean
  ctrlKey?: boolean
  metaKey?: boolean
}

/**
 * 方向键微移：默认 5、按住 Shift 20 flow 单位；带 Alt/Ctrl/Meta 一律不动
 * （那些组合留给平台与浏览器）。与上游一致；AEGIS 把"没有位移"显式写成 null。
 */
export function getKeyboardMovement(event: KeyboardMovementInput): { x: number; y: number } | null {
  if (event.altKey === true || event.ctrlKey === true || event.metaKey === true) return null

  const step = event.shiftKey === true ? 20 : 5

  switch (event.key) {
    case 'ArrowLeft':
      return { x: -step, y: 0 }
    case 'ArrowRight':
      return { x: step, y: 0 }
    case 'ArrowUp':
      return { x: 0, y: -step }
    case 'ArrowDown':
      return { x: 0, y: step }
    default:
      return null
  }
}
