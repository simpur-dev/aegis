// Ported from Dify web/app/components/workflow/utils/clipboard.ts
// (tag 1.17.1, commit 8387590ace4a094de812b7847fc6a4c3a27cd52b).
// Copyright (c) LangGenius, Inc. Licensed under the modified Apache License 2.0.
// Changes: adapted to AEGIS; see THIRD_PARTY_NOTICES.md.

/**
 * `navigator.clipboard` 适配层。上游在这里 `catch` 后返回空结果，于是"浏览器拒绝读剪贴板"
 * 和"剪贴板真的空着"长得一模一样；AEGIS 抛出带 code 的错误，UI 才能说清原因。
 */
import {
  ClipboardError,
  clipboardErrorMessage,
  parseWorkflowClipboardText,
  stringifyWorkflowClipboardData,
  type ClipboardParseResult,
  type ClipboardPayload,
} from '@/utils/graph/clipboard'

export interface ClipboardTextAdapter {
  readText(): Promise<string>
  writeText(text: string): Promise<void>
}

function browserClipboard(code: 'read-failed' | 'write-failed'): ClipboardTextAdapter {
  const clipboard = globalThis.navigator?.clipboard
  if (clipboard === undefined) {
    throw new ClipboardError(code, '当前环境没有剪贴板接口（需要 HTTPS 或 localhost）')
  }
  return clipboard
}

export async function readWorkflowClipboard(
  adapter: ClipboardTextAdapter = browserClipboard('read-failed'),
): Promise<ClipboardParseResult> {
  let text: string
  try {
    text = await adapter.readText()
  } catch (error) {
    throw new ClipboardError('read-failed', `读剪贴板失败：${clipboardErrorMessage(error)}`)
  }
  return parseWorkflowClipboardText(text)
}

export async function writeWorkflowClipboard(
  payload: ClipboardPayload,
  adapter: ClipboardTextAdapter = browserClipboard('write-failed'),
): Promise<void> {
  const text = stringifyWorkflowClipboardData(payload)
  try {
    await adapter.writeText(text)
  } catch (error) {
    throw new ClipboardError('write-failed', `写剪贴板失败：${clipboardErrorMessage(error)}`)
  }
}
