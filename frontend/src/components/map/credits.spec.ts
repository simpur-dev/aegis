/**
 * 署名口径用例：画面上的默认署名必须是"自托管、无第三方"，不能是 Cesium ion 的 logo。
 *
 * 为什么单独钉这一条：P0 的对外承诺是"不依赖 Cesium Ion / 谷歌"，而 Cesium 会把
 * `<a href="https://cesium.com/…"><img … title="Cesium ion"/></a>` 固定画在左下角
 * （Playwright 截图实测看到过）。代码里没引任何 ion 资产，但**画面上写着 ion**，
 * 考核时这就是自相矛盾的证据——所以它得由测试拦，而不是靠人记得看截图。
 */

import { describe, expect, it } from 'vitest'

import { BASEMAP_ATTRIBUTION } from './basemap'
import { applyOfflineCredits } from './viewer'
import { cesiumSourceFlat } from '@/testing/cesiumSource'
import { readRepoFile } from '@/testing/repoSource'

interface FakeCredit {
  readonly html: string
  readonly showOnScreen: boolean
}

function fakeCesium() {
  const created: FakeCredit[] = []
  const cesium = {
    Credit: class {
      readonly html: string
      readonly showOnScreen: boolean
      constructor(html: string, showOnScreen = false) {
        this.html = html
        this.showOnScreen = showOnScreen
        created.push(this)
      }
    },
    CreditDisplay: { cesiumCredit: undefined as unknown },
  }
  return { cesium, created }
}

describe('默认署名换成自托管说明', () => {
  it('挂上去的就是本项目的出处文案，且不再占屏幕一角', () => {
    const { cesium, created } = fakeCesium()
    applyOfflineCredits(cesium as unknown as Parameters<typeof applyOfflineCredits>[0])

    const credit = cesium.CreditDisplay.cesiumCredit as FakeCredit
    expect(credit).toBeDefined()
    expect(credit.html).toBe(BASEMAP_ATTRIBUTION)
    expect(credit.showOnScreen).toBe(false)
    expect(created).toHaveLength(1)
  })

  it('替换文案里不含任何第三方主机（离线页面不留外链表单）', () => {
    expect(BASEMAP_ATTRIBUTION).not.toMatch(/https?:\/\//)
    expect(BASEMAP_ATTRIBUTION).toMatch(/自托管/)
  })

  it('装配在建 Viewer 之前就换掉署名', () => {
    const source = readRepoFile('frontend', 'src', 'components', 'map', 'viewer.ts')
    const apply = source.indexOf('applyOfflineCredits(cesium)')
    const build = source.indexOf('new cesium.Viewer(')
    expect(apply).toBeGreaterThan(-1)
    expect(build).toBeGreaterThan(-1)
    expect(apply).toBeLessThan(build)
  })

  it('Cesium 侧前提没漂移：默认署名仍是 ion logo，且公开访问器还能改', () => {
    const flat = cesiumSourceFlat('Scene', 'CreditDisplay.js')
    expect(flat).toContain('title="Cesium ion"')
    expect(flat).toContain('cesiumCredit: {')
  })
})
