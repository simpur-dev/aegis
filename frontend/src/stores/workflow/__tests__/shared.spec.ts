import { describe, expect, it } from 'vitest'

import { WorkflowError } from '@/api/workflow/contract'

import { describeFailure, isAbortError, nowIso } from '../shared'

describe('store 共用错误文案', () => {
  it('WorkflowError 直接用自己的中文文案，不再拼 HTTP 码', () => {
    expect(describeFailure(new WorkflowError('revision_conflict', 409, '草稿已被其他会话修改'))).toBe(
      '草稿已被其他会话修改',
    )
  })

  it('普通 Error 取 message；认不出的抛掷物说“未知错误”', () => {
    expect(describeFailure(new Error('boom'))).toBe('boom')
    expect(describeFailure('扔了个字符串')).toBe('未知错误')
    expect(describeFailure(undefined)).toBe('未知错误')
  })

  it('只有 aborted 码算“请求已取消”', () => {
    expect(isAbortError(new WorkflowError('aborted', 0, '请求已取消'))).toBe(true)
    expect(isAbortError(new WorkflowError('network_error', 0, '断网'))).toBe(false)
    expect(isAbortError(new Error('aborted'))).toBe(false)
  })

  it('nowIso 产出可被 Date 解析的 ISO 串', () => {
    expect(Number.isNaN(new Date(nowIso()).getTime())).toBe(false)
  })
})
