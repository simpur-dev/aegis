/**
 * 四个 store 共用的错误文案与时间戳。
 *
 * 错误对象在传输层已还原成中文（`WorkflowError.message`），这里不再拼 HTTP 码：
 * 拼一次就会出现 `HTTP 409：草稿已被其他会话修改（服务器 revision=3）` 这类双重前缀。
 */
import { WorkflowError } from '@/api/workflow/contract'

export function nowIso(): string {
  return new Date().toISOString()
}

export function describeFailure(caught: unknown): string {
  if (caught instanceof WorkflowError) return caught.message
  return caught instanceof Error ? caught.message : '未知错误'
}

export function isAbortError(caught: unknown): boolean {
  return caught instanceof WorkflowError && caught.code === 'aborted'
}
