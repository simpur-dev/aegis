/**
 * debugHandle.ts 单测：开关判定是纯函数，挂/摘是可观察的副作用。
 * 另附一条装配面门禁——句柄要是哪天被静默去掉，视觉烟雾门禁就会退回"看截图猜"，
 * 所以这里钉住"装配处确实调用了 publish/clear"。
 */

import { beforeEach, describe, expect, it } from 'vitest'

import { MAP_DEBUG_QUERY_FLAG, MAP_DEBUG_WINDOW_KEY, clearMapDebugHandle, mapDebugEnabled, publishMapDebugHandle } from './debugHandle'
import { readRepoFile } from '@/testing/repoSource'

describe('mapDebug 开关判定', () => {
  it('只有显式带上标记才开', () => {
    expect(mapDebugEnabled('')).toBe(false)
    expect(mapDebugEnabled('?')).toBe(false)
    expect(mapDebugEnabled('?tab=stations')).toBe(false)
    expect(mapDebugEnabled(`?${MAP_DEBUG_QUERY_FLAG}=1`)).toBe(true)
    expect(mapDebugEnabled(MAP_DEBUG_QUERY_FLAG)).toBe(true)
    expect(mapDebugEnabled(`?tab=layers&${MAP_DEBUG_QUERY_FLAG}=`)).toBe(true)
  })

  it('不是把 mapDebugXYZ 这种前缀相似的名字也放行', () => {
    expect(mapDebugEnabled('?mapDebugPanel=1')).toBe(false)
  })
})

describe('句柄的挂与摘', () => {
  beforeEach(() => {
    clearMapDebugHandle()
  })

  it('未开启时什么都不挂', () => {
    const viewer = { __probe: 'viewer' }
    expect(publishMapDebugHandle(viewer, '')).toBeNull()
    expect((window as unknown as Record<string, unknown>)[MAP_DEBUG_WINDOW_KEY]).toBeUndefined()
  })

  it('开启时挂的是同一个只读引用，clear 之后不再留旧 Viewer', () => {
    const viewer = { __probe: 'viewer' }
    const handle = publishMapDebugHandle(viewer, `?${MAP_DEBUG_QUERY_FLAG}=1`)
    expect(handle).not.toBeNull()
    expect((handle as { viewer: unknown }).viewer).toBe(viewer)
    expect((window as unknown as Record<string, { viewer: unknown }>)[MAP_DEBUG_WINDOW_KEY]?.viewer).toBe(viewer)

    clearMapDebugHandle()
    expect((window as unknown as Record<string, unknown>)[MAP_DEBUG_WINDOW_KEY]).toBeUndefined()
    // 重复清不能炸：destroy 路径可能被走两次。
    expect(() => clearMapDebugHandle()).not.toThrow()
  })
})

describe('装配面真的用了这个句柄', () => {
  const source = readRepoFile('frontend', 'src', 'components', 'map', 'viewer.ts')

  it('建场时 publish、销毁时 clear，顺序也对', () => {
    const publish = source.indexOf('publishMapDebugHandle(viewer')
    const clear = source.indexOf('clearMapDebugHandle()')
    expect(publish).toBeGreaterThan(-1)
    expect(clear).toBeGreaterThan(-1)
    expect(clear).toBeGreaterThan(publish)
  })
})
