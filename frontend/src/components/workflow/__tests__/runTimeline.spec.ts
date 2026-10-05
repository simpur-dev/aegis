import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { InstanceNodeRun } from '@/api/workflow'

import RunTimeline from '../RunTimeline.vue'

function run(overrides: Partial<InstanceNodeRun> = {}): InstanceNodeRun {
  return {
    node_id: 'n1',
    type: 'api_call',
    state: 'pending',
    attempts: 1,
    schedule_latency_ms: null,
    duration_ms: null,
    output: {},
    error: null,
    notes: [],
    ...overrides,
  }
}

const names = { n1: '调取公路雪况实况', n2: '生成预警' }

describe('RunTimeline', () => {
  it('按引擎给的顺序逐行列出，序号连续，名字回定义里取', () => {
    const wrapper = mount(RunTimeline, {
      props: { runs: [run({ node_id: 'n1' }), run({ node_id: 'n2' })], names },
    })
    const rows = wrapper.findAll('.wf-tl__step')
    expect(rows).toHaveLength(2)
    expect(rows[0]!.find('.wf-tl__name').text()).toBe('调取公路雪况实况')
    expect(rows[1]!.find('.wf-tl__name').text()).toBe('生成预警')
    expect(rows.map((row) => row.find('.wf-tl__index').text())).toEqual(['1', '2'])
  })

  it('定义里没有这个节点时退回 node_id，不猜一个名字给人看', () => {
    const wrapper = mount(RunTimeline, { props: { runs: [run({ node_id: 'n9' })], names } })
    expect(wrapper.find('.wf-tl__name').text()).toBe('n9')
  })

  it('状态文案取自画布那一份表（NODE_STATE_STYLES），不另立第二套说法', () => {
    const wrapper = mount(RunTimeline, { props: { runs: [run({ state: 'awaiting_human' })], names } })
    expect(wrapper.find('.wf-tl__state').text()).toBe('待人工核签')
  })

  it('四档底色：没走到的用 dashed 透明，正在等的上主色，失败上红，走完的干净', () => {
    const wrapper = mount(RunTimeline, {
      props: {
        runs: [
          run({ node_id: 'a', state: 'succeeded' }),
          run({ node_id: 'b', state: 'running' }),
          run({ node_id: 'c', state: 'awaiting_human' }),
          run({ node_id: 'd', state: 'failed' }),
          run({ node_id: 'e', state: 'pending' }),
          run({ node_id: 'f', state: 'ready' }),
        ],
        names: {},
      },
    })
    const tones = wrapper.findAll('.wf-tl__step').map((row) => {
      for (const tone of ['done', 'active', 'bad', 'todo']) {
        if (row.classes().includes(`is-${tone}`)) return tone
      }
      return 'none'
    })
    expect(tones).toEqual(['done', 'active', 'active', 'bad', 'todo', 'todo'])
  })

  it('耗时/排队/重试合成一行小字；三样都没有时不占行', () => {
    const withMeta = mount(RunTimeline, {
      props: {
        runs: [run({ duration_ms: 1_250, schedule_latency_ms: 300, attempts: 2 })],
        names,
      },
    })
    expect(withMeta.find('.wf-tl__meta').text()).toBe('耗时 1.3s ｜ 排队 300ms ｜ 第 2 次尝试')
    const bare = mount(RunTimeline, { props: { runs: [run()], names } })
    expect(bare.find('.wf-tl__meta').exists()).toBe(false)
  })

  it('失败原因要出现在这一行上（接口台账里有而页面看不见，是这一条的来由）', () => {
    const wrapper = mount(RunTimeline, {
      props: { runs: [run({ state: 'failed', error: '上游 502：网关无响应' })], names },
    })
    const err = wrapper.find('.wf-tl__err')
    expect(err.text()).toContain('上游 502')
    expect(err.attributes('title')).toBe('上游 502：网关无响应')
  })

  it('最后一行不画连接线（画到底会多出一截悬空的轴）', () => {
    const wrapper = mount(RunTimeline, {
      props: { runs: [run({ node_id: 'n1' }), run({ node_id: 'n2' })], names },
    })
    expect(wrapper.findAll('.wf-tl__line')).toHaveLength(1)
  })

  it('没选中实例时说一句下一步去哪，而不是留一个空列表', () => {
    const wrapper = mount(RunTimeline, { props: { runs: [], names } })
    expect(wrapper.find('ol').exists()).toBe(false)
    expect(wrapper.text()).toContain('点一条实例')
  })
})
