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

const PURE_MODULES = ['./entities.ts', './offline.ts', './terrain.ts', './basemap.ts']
const RENDER_MODULES = ['./viewer.ts']
const COMPONENTS = ['./MapPanel.vue', '../../views/MapView.vue']
const ALL_SOURCES = [...PURE_MODULES, ...RENDER_MODULES, ...COMPONENTS, '../../api/map.ts']
const SPEC_FILES = ['./entities.spec.ts', './offline.spec.ts', './terrain.spec.ts', './basemap.spec.ts', './api.spec.ts']

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

  it.each(ALL_SOURCES)('%s 不含外部主机 URL（同源路径或 http://localhost 例外）', (path) => {
    const code = withoutComments(source(path))
    const offenders = (code.match(/https?:\/\/[^\s'"`]+/g) ?? []).filter((url) => !url.startsWith('http://localhost'))
    expect(offenders).toEqual([])
  })
})
