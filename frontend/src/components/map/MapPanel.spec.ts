/**
 * MapPanel 的「风险芯片」单测。
 *
 * 这一排芯片同时承担两件事：图例（不点也要看得出每级什么色）与筛选（点下去只看这一级）。
 * 所以最贵的退化是**图例说谎**——芯片上的色和图上画的色不是同一份；这里直接拿
 * `riskLegend()`/`RISK_COLORS` 当基准钉住它。第二贵的是筛选之后数字变小却不说为什么，
 * 于是图层计数必须写成"筛后 / 全量"。
 */

import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import { RISK_COLORS } from '@/api/types'
import type { RiskLevel } from '@/api/types'
import MapPanel from '@/components/map/MapPanel.vue'
import { DEFAULT_LAYER_VISIBILITY, riskLegend } from '@/components/map/entities'

const STUBS = {
  'a-card': { props: ['title'], template: '<div class="card"><slot /><slot name="extra" /></div>' },
  'a-space': { template: '<div><slot /></div>' },
  'a-switch': { props: ['checked'], emits: ['update:checked'], template: '<button class="switch" />' },
  'a-divider': { template: '<hr />' },
  'a-alert': { props: ['message', 'description'], template: '<div class="alert" />' },
  'a-tag': { props: ['color'], template: '<span><slot /></span>' },
  'a-empty': { props: ['description'], template: '<div class="empty" />' },
  'a-descriptions': { props: ['title'], template: '<div><slot /></div>' },
  'a-descriptions-item': { props: ['label'], template: '<div><slot /></div>' },
  'a-button': { props: ['loading', 'size'], template: '<button><slot /></button>' },
  'a-list': { template: '<div><slot /></div>' },
  'a-list-item': { template: '<div><slot /></div>' },
}

/** 全量：站点 6 / 预警 3 / 触达 2 / 面 1。筛后只留等级 1：站点 2 / 预警 1。 */
const TOTAL = { stations: 6, warnings: 3, reach: 2, hazards: 1 }
const FILTERED = { stations: 2, warnings: 1, reach: 2, hazards: 0 }
const RISK_COUNTS: Record<RiskLevel, number> = { 1: 3, 2: 0, 3: 2, 4: 0, 5: 0 }

function mountPanel(riskFilter: RiskLevel[] = [], filtered = false) {
  return mount(MapPanel, {
    props: {
      status: 'online' as never,
      statusReason: '全部资源可达',
      basemapMessage: '本地矢量瓦片可用',
      terrainMessage: '本地地形可用',
      counts: filtered ? FILTERED : TOTAL,
      countsTotal: TOTAL,
      visibility: { ...DEFAULT_LAYER_VISIBILITY },
      unlocated: [],
      rejected: [],
      detail: null,
      clustered: false,
      zoom: 6,
      loading: false,
      sceneError: null,
      riskFilter,
      riskCounts: RISK_COUNTS,
    },
    global: { stubs: STUBS },
  })
}

describe('风险等级芯片（兼图例）', () => {
  it('五级各一枚，未筛选时全都不按下', () => {
    const chips = mountPanel().findAll('.risk-chip')
    expect(chips).toHaveLength(5)
    for (const chip of chips) expect(chip.attributes('aria-pressed')).toBe('false')
  })

  it('芯片的颜色就是图上那一级的颜色（同一份 RISK_COLORS，不许另立调色板）', () => {
    const chips = mountPanel().findAll('.risk-chip')
    for (const entry of riskLegend()) {
      const chip = chips[entry.level - 1]
      expect(chip.attributes('style') ?? '', `等级 ${entry.level} 的芯片没带 --chip`).toContain(RISK_COLORS[entry.level])
      expect(chip.text()).toContain(entry.label)
    }
  })

  it('选中的等级按下，且图层计数写成"筛后 / 全量"', () => {
    const wrapper = mountPanel([1], true)
    expect(wrapper.findAll('.risk-chip')[0].attributes('aria-pressed')).toBe('true')
    const counts = wrapper.findAll('.count').map((node) => node.text())
    expect(counts).toContain('2 / 6')
    expect(counts).toContain('1 / 3')
    /* 触达层不受风险筛选影响（它没有评级），但同样要写成 x / y 才不显得数字在乱跳 */
    expect(counts).toContain('2 / 2')
  })

  it('不筛选时只报一个数，不凭空多出斜杠', () => {
    const counts = mountPanel().findAll('.count').map((node) => node.text())
    expect(counts).toContain('6')
    expect(counts.every((text) => !text.includes('/'))).toBe(true)
  })

  it('点芯片是把该级交给页面去筛（面板自己不判断）', async () => {
    const wrapper = mountPanel()
    await wrapper.findAll('.risk-chip')[2].trigger('click')
    expect(wrapper.emitted('toggleRisk')).toEqual([[3]])
  })

  it('筛选生效时说明行要出现，把"数字为什么变小"讲清楚', () => {
    expect(mountPanel([1]).text()).toContain('已按风险等级筛选')
    expect(mountPanel().text()).not.toContain('已按风险等级筛选')
  })

  it('芯片上报的是各级要素数（图例带量化，不只是色块）', () => {
    const chips = mountPanel().findAll('.risk-chip')
    expect(chips.map((chip) => chip.find('.risk-chip__count').text())).toEqual(['3', '0', '2', '0', '0'])
  })
})
