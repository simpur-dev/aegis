/**
 * 底图档状态机的判据。
 *
 * 默认档必须是 'offline'：交付形态不依赖任何外部主机；'amap' 是带 key 的官方 JS API 档
 * （见 `amap.ts`），必须显式选过才生效并记忆。
 * 高德官方渠道曾有一条**无 key 的内部瓦片端点**（autonavi 瓦），2026-10-17 已退役：
 * 全库禁入由 `guard.spec.ts` 钉住，这里不再有"在线源元数据"可测——它们随端点一起删了。
 */
import { describe, expect, it } from 'vitest'

import { readBasemapMode, writeBasemapMode } from './external'

describe('底图档状态机', () => {
  it('底图档持久化：默认离线，只有显式选过高德才用它，存储抛异常也不白屏', () => {
    const store = new Map<string, string>()
    const storage = {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => void store.set(key, value),
    }
    // 默认必须是 offline：'amap' 依赖官方服务与配额，不能成为"克隆下来就跑得到"的形态
    expect(readBasemapMode(storage)).toBe('offline')
    writeBasemapMode(storage, 'amap')
    expect(readBasemapMode(storage)).toBe('amap')
    writeBasemapMode(storage, 'offline')
    expect(readBasemapMode(storage)).toBe('offline')
    expect(readBasemapMode(null)).toBe('offline')
    const broken = { getItem: () => { throw new Error('隐私模式') }, setItem: () => { throw new Error('隐私模式') } }
    expect(readBasemapMode(broken)).toBe('offline')
    expect(() => writeBasemapMode(broken, 'offline')).not.toThrow()
  })
})
