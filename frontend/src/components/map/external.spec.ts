/**
 * 底图档状态机的判据。
 *
 * 通用默认档为 'offline'。配置了 Key 的吉隆案例可使用高德作为默认档。
 * 高德官方渠道曾有一条**无 key 的内部瓦片端点**（autonavi 瓦），2026-10-17 已退役：
 * 全库禁入由 `guard.spec.ts` 钉住，这里不再有"在线源元数据"可测——它们随端点一起删了。
 */
import { describe, expect, it } from 'vitest'

import { readBasemapMode, writeBasemapMode } from './external'

describe('底图档状态机', () => {
  it('读取已保存的底图选择，缺失或存储不可读取时返回对应默认值', () => {
    const store = new Map<string, string>()
    const storage = {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => void store.set(key, value),
    }
    expect(readBasemapMode(storage)).toBe('offline')
    expect(readBasemapMode(storage, 'amap')).toBe('amap')
    writeBasemapMode(storage, 'amap')
    expect(readBasemapMode(storage)).toBe('amap')
    writeBasemapMode(storage, 'offline')
    expect(readBasemapMode(storage)).toBe('offline')
    expect(readBasemapMode(storage, 'amap')).toBe('offline')
    expect(readBasemapMode(null)).toBe('offline')
    const broken = { getItem: () => { throw new Error('隐私模式') }, setItem: () => { throw new Error('隐私模式') } }
    expect(readBasemapMode(broken)).toBe('offline')
    expect(() => writeBasemapMode(broken, 'offline')).not.toThrow()
  })
})
