import { describe, expect, it, vi } from 'vitest'

import navigationFixture from '@/utils/graph/__fixtures__/dify-1.17.1/node-navigation.json'

import { focusNode, navigationCenter, viewportForNode, type ViewportTransform } from '../useNodeNavigation'

const viewport = navigationFixture.viewport

describe('节点定位（Dify T2 端口）', () => {
  it('setCenter 目标点与 fixture 一致（视口 1280×800，节点左上 100,200）', () => {
    const node = { position: { x: 100, y: 200 } }
    expect(navigationCenter(node, viewport)).toEqual({
      center: { x: navigationFixture.expected.x, y: navigationFixture.expected.y },
      options: navigationFixture.expected.options,
    })
  })

  it('zoom=1 时节点左上角落在视口 25% / 25%', () => {
    const node = { position: { x: 100, y: 200 } }
    const transform = viewportForNode(node, viewport)
    expect(transform).toEqual({ x: 220, y: 0, zoom: 1 })

    const screenX = transform.x + node.position.x * transform.zoom
    const screenY = transform.y + node.position.y * transform.zoom
    expect(screenX).toBeCloseTo(viewport.width * 0.25, 6)
    expect(screenY).toBeCloseTo(viewport.height * 0.25, 6)
  })

  it('缩放档位按 setCenter 的等价式换算，不把像素当 flow 单位', () => {
    const node = { position: { x: 100, y: 200 } }
    const transform = viewportForNode(node, viewport, 0.5)
    expect(transform).toEqual({ x: 640 - 420 * 0.5, y: 400 - 400 * 0.5, zoom: 0.5 })
  })

  it('focusNode 找到目标就调 setViewport，并带 800ms 动画', async () => {
    const calls: { transform: ViewportTransform; options: { duration: number } }[] = []
    const setViewport = vi.fn((transform: ViewportTransform, options: { duration: number }) => {
      calls.push({ transform, options })
    })
    const nodes = [
      { id: 'a', position: { x: 0, y: 0 } },
      { id: 'b', position: { x: 100, y: 200 } },
    ]

    expect(await focusNode(nodes, 'b', viewport, setViewport)).toBe(true)
    expect(setViewport).toHaveBeenCalledTimes(1)
    expect(calls[0]).toEqual({ transform: { x: 220, y: 0, zoom: 1 }, options: { duration: 800 } })
  })

  it('目标不存在时不抛、不动视野（URL 里的旧 id 会被安静忽略）', async () => {
    const setViewport = vi.fn()
    expect(await focusNode([{ id: 'a', position: { x: 0, y: 0 } }], 'gone', viewport, setViewport)).toBe(false)
    expect(setViewport).not.toHaveBeenCalled()
  })
})
