/**
 * 能力面形状兜底：缺字段时页面读的是"读到了但这项没有"，不是白屏。
 *
 * 这里只钉一件事——`example` 会不会在归一化时被丢掉。丢掉的话建议条全部禁用，
 * 页面看起来"有 12 个动作但一个都点不动"，而这正是本次要修的缺陷的原样。
 */

import { describe, expect, it } from 'vitest'

import { capabilitiesShape } from './assistant'

function rawAction(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    action: 'query.warnings',
    title: '查询已发布预警',
    requires_confirmation: false,
    needs: ['list_warnings'],
    available: true,
    missing: [],
    example: '最近发布了哪些预警',
    ...overrides,
  }
}

describe('capabilitiesShape 的 example', () => {
  it('原样带出后端给的示例句', () => {
    const caps = capabilitiesShape({ actions: [rawAction()] })
    expect(caps.actions[0]?.example).toBe('最近发布了哪些预警')
  })

  it('后端没给时兜底成空串而不是 undefined', () => {
    const caps = capabilitiesShape({ actions: [rawAction({ example: undefined })] })
    expect(caps.actions[0]?.example).toBe('')
  })

  it('示例句类型不对也不崩（非字符串按空串处理）', () => {
    const caps = capabilitiesShape({ actions: [rawAction({ example: 12 })] })
    expect(caps.actions[0]?.example).toBe('')
  })
})
