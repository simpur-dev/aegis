// Ported from Dify web/app/components/workflow/utils/node.ts
// (tag 1.17.1, commit 8387590ace4a094de812b7847fc6a4c3a27cd52b).
// Copyright (c) LangGenius, Inc. Licensed under the modified Apache License 2.0.
// Changes: adapted to AEGIS; see THIRD_PARTY_NOTICES.md.
import type { Position } from '../graph'

const TITLE_SUFFIX = /^(.+?)\s*\((\d+)\)\s*$/

/** 标题递增规则与上游一致；基名由调用方给（AEGIS 用中文节点名）。 */
export function genNewNodeTitleFromOld(oldTitle: string): string {
  const match = TITLE_SUFFIX.exec(oldTitle)
  if (match) {
    const title = match[1] ?? ''
    const num = Number.parseInt(match[2] ?? '0', 10)
    return `${title} (${num + 1})`
  }
  return `${oldTitle} (1)`
}

/**
 * 上游对空数组返回 `{ x: Infinity, y: Infinity }`，序列化后会变成 null；
 * AEGIS 显式返回 null，让调用方决定"没有节点时落在哪里"。
 */
export function getTopLeftNodePosition(nodes: readonly { position: Position }[]): Position | null {
  let minX = Infinity
  let minY = Infinity

  for (const node of nodes) {
    if (node.position.x < minX) minX = node.position.x
    if (node.position.y < minY) minY = node.position.y
  }

  return Number.isFinite(minX) && Number.isFinite(minY) ? { x: minX, y: minY } : null
}
