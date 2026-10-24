import { describe, expect, it } from 'vitest'

import { desiredWorkflowQuery, nextWorkflowQuery, readWorkflowDeepLink, type WorkflowUrlState } from '../urlState'

function state(overrides: Partial<WorkflowUrlState> = {}): WorkflowUrlState {
  return {
    lineageId: 'wf_0123456789ab',
    publishedWorkflowId: 'wf_0123456789cd',
    mode: { kind: 'draft' },
    instance: null,
    ...overrides,
  }
}

describe('深链读取：只认字符串', () => {
  it('三个 ID 都取字符串；数组、null、缺失一律当没给', () => {
    expect(
      readWorkflowDeepLink({
        lineage: 'wf_0123456789ab',
        workflow: ['wf_0123456789cd', 'wf_ffffffffffff'],
        instance: null,
        tab: 'run',
      }),
    ).toEqual({ lineage: 'wf_0123456789ab', workflow: '', instance: '' })
    expect(readWorkflowDeepLink({})).toEqual({ lineage: '', workflow: '', instance: '' })
  })
})

describe('地址栏状态：按模式决定写哪几个 ID', () => {
  it('草稿模式写 lineage；没有实例时再带上当前发布版', () => {
    expect(desiredWorkflowQuery(state())).toEqual({
      lineage: 'wf_0123456789ab',
      workflow: 'wf_0123456789cd',
    })
  })

  it('草稿模式没有 lineage 也没有发布版时地址栏为空', () => {
    expect(desiredWorkflowQuery(state({ lineageId: '', publishedWorkflowId: null }))).toEqual({})
  })

  it('草稿模式有实例时写 instance + workflow，不把实例的版本号丢掉', () => {
    expect(
      desiredWorkflowQuery(state({ instance: { instanceId: 'wfi_0123456789ab', workflowId: 'wf_0123456789ef' } })),
    ).toEqual({
      lineage: 'wf_0123456789ab',
      instance: 'wfi_0123456789ab',
      workflow: 'wf_0123456789ef',
    })
  })

  it('版本模式只写 workflow——带 lineage 会在 F5 时被草稿抢走', () => {
    expect(desiredWorkflowQuery(state({ mode: { kind: 'version', workflowId: 'wf_0123456789cd' } }))).toEqual({
      workflow: 'wf_0123456789cd',
    })
    expect(desiredWorkflowQuery(state({ mode: { kind: 'version', workflowId: '' } }))).toEqual({})
    expect(desiredWorkflowQuery(state({ mode: { kind: 'version' } }))).toEqual({})
  })

  it('实例模式只写 instance + 它绑定的版本', () => {
    expect(
      desiredWorkflowQuery(
        state({
          mode: { kind: 'instance' },
          instance: { instanceId: 'wfi_0123456789ab', workflowId: 'wf_0123456789ef' },
        }),
      ),
    ).toEqual({ instance: 'wfi_0123456789ab', workflow: 'wf_0123456789ef' })
    expect(desiredWorkflowQuery(state({ mode: { kind: 'instance' } }))).toEqual({})
  })
})

describe('地址栏合并：其余参数保留，旧 ID 该删就删', () => {
  it('地址已经一致时不触发 replace', () => {
    expect(
      nextWorkflowQuery(
        { lineage: 'wf_0123456789ab', workflow: 'wf_0123456789cd' },
        { lineage: 'wf_0123456789ab', workflow: 'wf_0123456789cd' },
      ),
    ).toEqual({ changed: false, query: {} })
  })

  it('保留与本页无关的字符串参数', () => {
    expect(
      nextWorkflowQuery(
        { lineage: 'wf_0123456789ab', tab: 'run', notes: ['a', 'b'] },
        { lineage: 'wf_0123456789ab', workflow: 'wf_0123456789cd' },
      ),
    ).toEqual({
      changed: true,
      query: { tab: 'run', lineage: 'wf_0123456789ab', workflow: 'wf_0123456789cd' },
    })
  })

  it('新建画布后旧的 lineage 必须从地址栏删掉，否则 F5 又把它打开', () => {
    expect(nextWorkflowQuery({ lineage: 'wf_0123456789ab', tab: 'run' }, {})).toEqual({
      changed: true,
      query: { tab: 'run' },
    })
  })

  it('切换实例时删掉上一个 instance，换成新的', () => {
    expect(
      nextWorkflowQuery({ instance: 'wfi_old', workflow: 'wf_old' }, { instance: 'wfi_new', workflow: 'wf_new' }),
    ).toEqual({
      changed: true,
      query: { instance: 'wfi_new', workflow: 'wf_new' },
    })
  })
})
