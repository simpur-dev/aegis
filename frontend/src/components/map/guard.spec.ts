/**
 * 架构守卫单测：把"纯逻辑不碰 Cesium、全页不碰外部服务"从口头约定变成 CI 断言。
 *
 * 为什么需要它：本目录的分层约定（entities/offline 纯、viewer 独占 WebGL）一旦破口，
 * 后果是**测试跑不动**（jsdom 无 WebGL）与**现场断网白屏**（偷偷引了外链）。
 * 这两种退化都很安静，所以用读源码的方式直接把它们钉住，而不是靠 review 时人眼扫。
 */

import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

/** 读取本模块（或跨目录）文件的源码文本。 */
function source(path: string): string {
  return readFileSync(new URL(path, import.meta.url), 'utf8')
}

/** 剥掉块注释与行注释：守卫只看代码，不看"文档里提到了某个禁令"。 */
function withoutComments(code: string): string {
  return code.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
}

const PURE_MODULES = ['./entities.ts', './offline.ts', './terrain.ts', './basemap.ts', './external.ts', './gcj02.ts']
const RENDER_MODULES = ['./viewer.ts']
const COMPONENTS = ['./MapStatusPanel.vue', './MapLayersPanel.vue', '../../views/MapView.vue']
const ALL_SOURCES = [...PURE_MODULES, ...RENDER_MODULES, ...COMPONENTS, '../../api/map.ts']
const SPEC_FILES = [
  './entities.spec.ts',
  './offline.spec.ts',
  './terrain.spec.ts',
  './basemap.spec.ts',
  './api.spec.ts',
  './external.spec.ts',
  './gcj02.spec.ts',
]

/** 静态导入语句：`import ... from 'x'`（含多行）；带 type 关键字的被排除。 */
const STATIC_VALUE_IMPORTS = /(^|\n)\s*import\s+(?!type\s)[\s\S]*?from\s+['"]([^'"]+)['"]/g

describe('分层边界：谁可以碰 Cesium', () => {
  it.each(PURE_MODULES)('%s 不产生任何 cesium 运行时导入（含动态 import）', (path) => {
    const code = withoutComments(source(path))
    const valueImports = [...code.matchAll(STATIC_VALUE_IMPORTS)].map((match) => match[2])
    expect(valueImports.filter((target) => target === 'cesium' || target.startsWith('cesium/'))).toEqual([])
    expect(code).not.toMatch(/import\(\s*['"]cesium/)
    expect(code).not.toMatch(/require\(\s*['"]cesium/)
  })

  it('entities.ts / offline.ts 在注释之外零提及 cesium（连类型都不借道）', () => {
    for (const path of ['./entities.ts', './offline.ts']) {
      expect(withoutComments(source(path)).match(/cesium/gi) ?? [], path).toEqual([])
    }
  })

  it('terrain.ts / basemap.ts 借道 cesium 的方式只有一种：`import type`（编译期即抹掉）', () => {
    for (const path of ['./terrain.ts', './basemap.ts']) {
      const code = withoutComments(source(path))
      // 反向兜底：确明确实存在 type-only 说明符，否则上一条断言会退化成"永真"。
      expect(/import type\s*\{[\s\S]*?\}\s*from\s*['"]cesium['"]/.test(code), path).toBe(true)
    }
  })

  it('viewer.ts 只有一处运行时导入：createMapScene 内部的 await import，因此 cesium 独立分块', () => {
    const code = withoutComments(source('./viewer.ts'))
    const runtime = code.match(/await\s+import\(\s*['"]cesium['"]\s*\)/g) ?? []
    const typeOnly = code.match(/typeof\s+import\(\s*['"]cesium['"]\s*\)/g) ?? []
    const all = code.match(/import\(\s*['"]cesium['"]\s*\)/g) ?? []
    expect(runtime).toHaveLength(1)
    expect(typeOnly).toHaveLength(1)
    expect(all).toHaveLength(runtime.length + typeOnly.length)
    expect(code).not.toMatch(/require\(\s*['"]cesium/)
  })

  it.each(COMPONENTS)('%s 不引用 cesium 模块、不直接调用引擎 API（页面只动态拿 viewer）', (path) => {
    const code = withoutComments(source(path))
    expect(code).not.toMatch(/['"]cesium/)
    expect(code).not.toMatch(/Cesium\.[A-Za-z]/)
    expect(code).not.toMatch(/new\s+[A-Za-z]*Viewer\s*\(/)
  })

  it('MapView 走动态 import 拿 viewer：地图页不进首屏包', () => {
    expect(source('../../views/MapView.vue')).toContain("await import('@/components/map/viewer')")
  })

  it.each(SPEC_FILES)('%s 不导入 cesium（jsdom 无 WebGL，测试因此可跑）', (path) => {
    const code = source(path)
    expect(code).not.toMatch(/from\s+['"]cesium['"]/)
    expect(code).not.toMatch(/import\(\s*['"]cesium['"]\s*\)/)
    expect(code).not.toMatch(/require\(\s*['"]cesium/)
  })

  it('entities/offline 连类型都不借道 api 层以外的运行时依赖', () => {
    for (const path of ['./entities.ts', './offline.ts']) {
      const targets = [...withoutComments(source(path)).matchAll(STATIC_VALUE_IMPORTS)].map((match) => match[2])
      // entities.ts 允许 @/api/types（纯常量与类型）；offline.ts 零依赖。
      expect(targets.every((target) => target === '@/api/types' || target.startsWith('./') || target.startsWith('@/components/map/'))).toBe(true)
    }
  })
})

describe('零外部服务：无托管瓦片、无令牌、无 CDN', () => {
  const FORBIDDEN = [
    'defaultAccessToken',
    'ion.cesium',
    'fromWorldImagery',
    'fromWorldImageryAsync',
    'createWorldImageryAsync',
    'fromWorldTerrain',
    'createWorldTerrain',
    'Cesium ion',
    'IonResource',
    'googleapis',
    'gstatic',
    'jsdelivr',
    'unpkg',
    'maps.google',
  ]

  it.each(ALL_SOURCES)('%s 不含任何第三方服务标识', (path) => {
    const code = source(path)
    for (const token of FORBIDDEN) expect(code.includes(token), `${path} 出现 ${token}`).toBe(false)
  })

  /**
   * 外部主机只准出现在 `amap.ts` 一个文件里（高德 JS API 官方脚本，key 走环境变量）。
   *
   * 口径沿革：原来这条是"任何 `https://` 都不许有"；2026-10-17 接入高德在线底图时改成
   * "外部源集中在 external.ts 且自报联网与坐标系"；同日官方 JS API 落地、**无 key 的内部瓦片
   * 端点（autonavi）退役**，口径收敛成今天的三条可测判据：
   * ① 离线链 + 场景层（basemap/offline/terrain/entities/gcj02/external/viewer）必须零外部主机；
   * ② 唯一的外部主机住 `amap.ts`，且必须是带 key 的官方通道（`VITE_AMAP_KEY`）；
   * ③ 退役的 autonavi 端点全库禁入（剥掉注释后查——文档里可以提它，代码里不许有）。
   * "默认档 offline"这件事由 `readBasemapMode` 承担（external.spec.ts 钉住）。
   */
  const OFFLINE_CHAIN = ['./basemap.ts', './offline.ts', './terrain.ts', './entities.ts', './gcj02.ts', './external.ts', './viewer.ts']
  it.each(OFFLINE_CHAIN)('%s 不含任何外部主机 URL（离线链不借道第三方）', (path) => {
    const code = withoutComments(source(path))
    const offenders = (code.match(/https?:\/\/[^\s'"`]+/g) ?? []).filter((url) => !url.startsWith('http://localhost'))
    expect(offenders, `${path} 出现外部主机：${offenders.join(' ')}`).toEqual([])
  })

  it('外部主机只集中在 amap.ts，且走带 key 的官方 JS API', () => {
    const code = withoutComments(source('./amap.ts'))
    const hosts = [...new Set((code.match(/https:\/\/[^/'"`]+/g) ?? []).filter((url) => !url.startsWith('http://localhost')))]
    expect(hosts.length, 'amap.ts 里一个外部主机都没有——这条在空跑').toBeGreaterThanOrEqual(1)
    for (const host of hosts) {
      expect(host, `amap.ts 出现了官方脚本之外的主机：${host}`).toBe('https://webapi.amap.com')
    }
    expect(code, '官方通道必须带 key（从环境变量取），不许退回无 key 端点').toContain('VITE_AMAP_KEY')
  })

  it('退役的无 key 瓦片端点（autonavi）全库禁入', () => {
    const scanned = [...ALL_SOURCES, './amap.ts', './AmapMap.vue']
    for (const path of scanned) {
      expect(withoutComments(source(path)), `${path} 里 autonavi 端点回来了`).not.toContain('autonavi.com')
    }
  })
})
