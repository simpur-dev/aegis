import { describe, expect, it } from 'vitest'

import { freeDropPosition, NODE_FOOTPRINT } from './graph'

/**
 * 落点让位：连拖两个节点到几乎同一个点时不许叠住。
 * 真机量过的原始缺陷——第二张压住第一张 389×234px（落点原样取光标位置，而节点卡有 208px 宽）。
 */
describe('freeDropPosition', () => {
  it('空画布上落在哪儿就是哪儿：不做无谓的位移', () => {
    expect(freeDropPosition([], { x: 120, y: 200 })).toEqual({ x: 120, y: 200 })
  })

  it('离已有节点足够远时原样返回（不许把不冲突的落点也挪走）', () => {
    const taken = [{ x: 0, y: 0 }]
    const far = { x: NODE_FOOTPRINT.width + 24 + 10, y: 0 }
    expect(freeDropPosition(taken, far)).toEqual(far)
  })

  it('正压着已有节点时让开：新坐标与任何已存坐标都不再重叠', () => {
    const taken = [{ x: 300, y: 300 }]
    const got = freeDropPosition(taken, { x: 312, y: 314 })
    expect(got).not.toEqual({ x: 312, y: 314 })
    const stepX = NODE_FOOTPRINT.width + 24
    const stepY = NODE_FOOTPRINT.height + 24
    const overlaps = taken.some((n) => Math.abs(n.x - got.x) < stepX && Math.abs(n.y - got.y) < stepY)
    expect(overlaps, `让位之后仍然压着：${JSON.stringify(got)}`).toBe(false)
  })

  it('连让多次都成立：往同一点连投五张，两两不重叠', () => {
    const taken: { x: number; y: number }[] = []
    for (let i = 0; i < 5; i += 1) {
      const placed = freeDropPosition(taken, { x: 200, y: 200 })
      const stepX = NODE_FOOTPRINT.width + 24
      const stepY = NODE_FOOTPRINT.height + 24
      for (const other of taken) {
        expect(
          Math.abs(other.x - placed.x) < stepX && Math.abs(other.y - placed.y) < stepY,
          `第 ${i + 1} 张与已投的第 ${taken.indexOf(other) + 1} 张重叠`,
        ).toBe(false)
      }
      taken.push(placed)
    }
    expect(taken).toHaveLength(5)
  })

  it('自定义间距会改变让位步长（gap 给大，落点就更远）', () => {
    const taken = [{ x: 0, y: 0 }]
    const tight = freeDropPosition(taken, { x: 10, y: 10 }, 24)
    const loose = freeDropPosition(taken, { x: 10, y: 10 }, 200)
    /** 断位移距离而不是某个轴的坐标：让位顺序是「先下后右」，按 x 比会恒等。 */
    const away = (p: { x: number; y: number }): number => Math.hypot(p.x, p.y)
    expect(away(loose)).toBeGreaterThan(away(tight))
  })

  it('让位只往右下走：往左上让等于把节点推出画布可视区', () => {
    /* 真机第一版按 -ring 先试，第二张被推到屏幕 x=9（画布左边缘是 240）——
       不压叠但看不见，同样是要修的缺陷。 */
    const taken = [{ x: 500, y: 500 }]
    const got = freeDropPosition(taken, { x: 512, y: 512 })
    expect(got.x).toBeGreaterThanOrEqual(512)
    expect(got.y).toBeGreaterThanOrEqual(512)
  })

  it('贴着画布下沿落：卡片不许整张垂到框外（真机量到 y=704..957 而画布底 880）', () => {
    const box = { x: 0, y: 0, width: 828, height: 678 }
    const got = freeDropPosition([], { x: 300, y: 660 }, 24, box)
    expect(got.y, '应被收进框内').toBe(box.y + box.height - NODE_FOOTPRINT.height)
    expect(got.x).toBe(300)
  })

  it('有可视框时让位不许出框：连投四张挤在下沿，每张都还在框里', () => {
    const box = { x: 0, y: 0, width: 828, height: 678 }
    const taken: { x: number; y: number }[] = []
    for (let i = 0; i < 4; i += 1) {
      const placed = freeDropPosition(taken, { x: 600, y: 640 }, 24, box)
      taken.push(placed)
    }
    for (const p of taken) {
      expect(p.x + NODE_FOOTPRINT.width, `x 出框：${JSON.stringify(p)}`).toBeLessThanOrEqual(box.x + box.width)
      expect(p.y + NODE_FOOTPRINT.height, `y 出框：${JSON.stringify(p)}`).toBeLessThanOrEqual(box.y + box.height)
    }
    const stepX = NODE_FOOTPRINT.width + 24
    const stepY = NODE_FOOTPRINT.height + 24
    for (let i = 1; i < taken.length; i += 1) {
      for (const other of taken.slice(0, i)) {
        const overlap =
          Math.abs(other.x - taken[i].x) < stepX && Math.abs(other.y - taken[i].y) < stepY
        expect(overlap, `第 ${i + 1} 张与前面的重叠`).toBe(false)
      }
    }
  })

  it('落点压在不透明浮层（小地图）上时挪开：被盖住等于没显示', () => {
    const box = { x: 0, y: 0, width: 828, height: 678 }
    const mini = { x: 616, y: 506, width: 200, height: 160 }
    const got = freeDropPosition([], { x: 660, y: 540 }, 24, box, [mini])
    const hits =
      got.x < mini.x + mini.width &&
      got.x + NODE_FOOTPRINT.width > mini.x &&
      got.y < mini.y + mini.height &&
      got.y + NODE_FOOTPRINT.height > mini.y
    expect(hits, `仍然被浮层盖住：${JSON.stringify(got)}`).toBe(false)
    expect(got.x + NODE_FOOTPRINT.width).toBeLessThanOrEqual(box.width)
    expect(got.y + NODE_FOOTPRINT.height).toBeLessThanOrEqual(box.height)
  })

  it('框内确实没空位时优先保不压叠（越界还能平移画布找回来，压住了只能一颗颗拖）', () => {
    const box = { x: 0, y: 0, width: 260, height: 150 }
    const taken = [
      { x: 0, y: 0 },
      { x: 260, y: 0 },
      { x: 0, y: 200 },
      { x: 260, y: 200 },
      { x: -260, y: 0 },
      { x: 0, y: -200 },
    ]
    const got = freeDropPosition(taken, { x: 10, y: 10 }, 24, box)
    const stepX = NODE_FOOTPRINT.width + 24
    const stepY = NODE_FOOTPRINT.height + 24
    expect(
      taken.some((n) => Math.abs(n.x - got.x) < stepX && Math.abs(n.y - got.y) < stepY),
      `框内无空位时仍压着：${JSON.stringify(got)}`,
    ).toBe(false)
  })
})
