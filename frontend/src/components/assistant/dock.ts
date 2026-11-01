/**
 * 助手悬浮图标的纯计算部分（不碰 DOM，方便单测）。
 *
 * 图标只能上下拖：水平位置由 CSS 固定（关=贴右边缘，开=贴抽屉左侧）；
 * 这里只负责"竖直位置夹紧"与"这次松手算点击还是拖拽"两条规则。
 */

/** 图标直径（px）：与 AssistantDock.vue 的图标尺寸保持一致 */
export const DOCK_SIZE = 56
/** 位移小于这个值算点击，否则算拖拽（避免拖完松手误开抽屉） */
export const CLICK_SLOP = 4
/** 图标与顶栏/视口边缘的最小留白 */
export const DOCK_MARGIN = 8

/**
 * 把图标竖直位置夹在「顶栏下方」与「视口底部」之间。
 *
 * @param nextTop    期望的 top（px）
 * @param viewportH  视口高度（window.innerHeight）
 * @param headerH    顶栏/编辑器头部高度（px）
 */
export function clampTop(nextTop: number, viewportH: number, headerH: number, size = DOCK_SIZE): number {
  const min = headerH + DOCK_MARGIN
  const max = Math.max(min, viewportH - size - DOCK_MARGIN)
  return Math.min(Math.max(Math.round(nextTop), min), max)
}

/** 松手时判断这次指针操作是不是"点击"（位移小于阈值）。 */
export function isClick(dx: number, dy: number, slop = CLICK_SLOP): boolean {
  return Math.hypot(dx, dy) < slop
}
