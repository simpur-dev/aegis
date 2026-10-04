/**
 * 后端校验错误的统一还原处。三个出口（上报 / 工作流 / 助手）都走这里，
 * 所以这份规格钉的是"三页必须长成同一种人话"的底线，不是某一页的偏好。
 *
 * 逐个形状来自实测踩过的坑：`[object Object]`（数组被 String() 出来）、
 * 整段原因被丢（只落一句"HTTP 422"）、裸对象值里的数字被吞成空串。
 */

import { describe, expect, it } from 'vitest'

import { describeValidationDetail } from './validationDetail'

const LABELS = { note: '险情描述', message: '对话内容' }

describe('describeValidationDetail', () => {
  it('字符串 detail（400 的业务原因）原样给出，不加前缀不翻译', () => {
    expect(describeValidationDetail('工作流图存在环', LABELS)).toBe('工作流图存在环')
  })

  it('FastAPI 数组：单层字段配中文标签，规则原文照抄', () => {
    expect(
      describeValidationDetail([{ loc: ['body', 'note'], msg: 'String should have at least 4 characters', type: 'tool_long' }], LABELS),
    ).toBe('险情描述（note）：String should have at least 4 characters')
  })

  it('多条原因按后端顺序拼成一整句', () => {
    const text = describeValidationDetail(
      [
        { loc: ['body', 'note'], msg: ' too short' },
        { loc: ['body', 'message'], msg: 'String should have at most 2000 characters' },
      ],
      LABELS,
    )
    expect(text).toBe('险情描述（note）： too short；对话内容（message）：String should have at most 2000 characters')
  })

  it('嵌套路径保持原形：下标是要看的定位线索，不翻译', () => {
    expect(
      describeValidationDetail([{ loc: ['body', 'nodes', 0, 'config', 'limit'], msg: 'Input should be a valid integer' }], LABELS),
    ).toBe('nodes.0.config.limit：Input should be a valid integer')
  })

  it('loc 的 body/query 起点段被去掉，path 段留着（它是真实信息）', () => {
    expect(describeValidationDetail([{ loc: ['query', 'limit'], msg: '必填' }], {})).toBe('limit：必填')
    expect(describeValidationDetail([{ loc: ['path', 'session_id'], msg: '格式不对' }], {})).toBe('path.session_id：格式不对')
  })

  it('{detail:{...}} 套娃递归到里层，读出 message', () => {
    expect(describeValidationDetail({ detail: { code: 'E_ASSISTANT_UNAVAILABLE', message: '语义交互服务未装配' } }, LABELS)).toBe(
      '语义交互服务未装配',
    )
  })

  it('裸对象摊成 k=v，值里的数字不许被吞成空串', () => {
    expect(describeValidationDetail({ region_code: 'bad', limit: 0 }, LABELS)).toBe('region_code=bad limit=0')
    expect(describeValidationDetail({ repeated: false }, LABELS)).toBe('repeated=false')
  })

  it('认不出的形状返回空串：由调用方回落自己的兜底，这里不编造原因', () => {
    expect(describeValidationDetail(undefined, LABELS)).toBe('')
    expect(describeValidationDetail(null, LABELS)).toBe('')
    expect(describeValidationDetail([], LABELS)).toBe('')
    expect(describeValidationDetail([{}, {}], LABELS)).toBe('')
  })

  it('任何形状都不许漏出 [object Object] 或 undefined', () => {
    const shapes: unknown[] = [
      undefined,
      null,
      42,
      true,
      '纯文本',
      ['a', 'b'],
      { foo: { bar: 1 } },
      { detail: { detail: '递归两层' } },
      [{ loc: ['q', 'x'], msg: '必填' }],
      [{ loc: [], msg: '' }],
      [{ msg: '没有 loc' }, { loc: ['body', 'note'] }],
      { region_code: 'bad', limit: 0 },
    ]
    for (const shape of shapes) {
      const text = describeValidationDetail(shape, LABELS)
      expect(text, JSON.stringify(shape)).not.toContain('[object Object]')
      expect(text, JSON.stringify(shape)).not.toContain('undefined')
    }
  })
})
