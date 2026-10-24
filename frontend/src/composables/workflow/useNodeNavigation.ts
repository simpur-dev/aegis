// Ported from Dify web/app/components/workflow/utils/node-navigation.ts
// (tag 1.17.1, commit 8387590ace4a094de812b7847fc6a4c3a27cd52b).
// Copyright (c) LangGenius, Inc. Licensed under the modified Apache License 2.0.
// Changes: adapted to AEGIS; see THIRD_PARTY_NOTICES.md.

/**
 * 定位一个节点：上游用 `reactflow.setCenter(x + innerWidth*0.25, y + innerHeight*0.25)`，
 * 即把"节点左上角 + 视口 1/4 宽高"这个点放到屏幕中心，于是节点落在左上 1/4 处。
 * AEGIS 换成 Vue Flow 的 `setViewport`（左上角平移量），数学逐项对得上。
 */
import type { Position } from '@/utils/graph'

export interface ViewportSize {
  width: number
  height: number
}

export interface ViewportTransform {
  x: number
  y: number
  zoom: number
}

export const NODE_NAVIGATION_ZOOM = 1
export const NODE_NAVIGATION_DURATION_MS = 800

/** 上游 setCenter 的目标点：节点左上角 + 视口 25%。 */
export function navigationCenter(
  node: { position: Position },
  viewport: ViewportSize,
): { center: Position; options: { zoom: number; duration: number } } {
  return {
    center: {
      x: node.position.x + viewport.width * 0.25,
      y: node.position.y + viewport.height * 0.25,
    },
    options: { zoom: NODE_NAVIGATION_ZOOM, duration: NODE_NAVIGATION_DURATION_MS },
  }
}

/**
 * setCenter(cx, cy, zoom) ≡ setViewport({ x: width/2 - cx*zoom, y: height/2 - cy*zoom, zoom })。
 * zoom=1 时节点左上角正好落在视口的 (25%, 25%)。
 */
export function viewportForNode(
  node: { position: Position },
  viewport: ViewportSize,
  zoom: number = NODE_NAVIGATION_ZOOM,
): ViewportTransform {
  const { center } = navigationCenter(node, viewport)
  return {
    x: viewport.width / 2 - center.x * zoom,
    y: viewport.height / 2 - center.y * zoom,
    zoom,
  }
}

export type SetViewportFn = (transform: ViewportTransform, options: { duration: number }) => void | Promise<void>

/**
 * 找不到节点（已删、URL 里的旧 id）返回 false，不抛也不乱移视野。
 * 上游同样在 `if (node)` 里静默跳过。
 */
export async function focusNode(
  nodes: readonly { id: string; position: Position }[],
  nodeId: string,
  viewport: ViewportSize,
  setViewport: SetViewportFn,
  zoom: number = NODE_NAVIGATION_ZOOM,
): Promise<boolean> {
  const node = nodes.find((candidate) => candidate.id === nodeId)
  if (node === undefined) return false

  await setViewport(viewportForNode(node, viewport, zoom), { duration: NODE_NAVIGATION_DURATION_MS })
  return true
}
