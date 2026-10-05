/**
 * UI 基座契约（对标 NexusMind 改版，批次一、二）。
 *
 * 钉住四件会静默回归、肉眼又难以及时发现的东西：
 * 1) 字体自托管——“弱网离线可用”是部署形态 P0，外链字体 CDN 在离线环境会掉字；
 * 2) 样式入口——fonts.css / theme.css 不进 main.ts，整套改造等于没做；
 * 3) 顶部导航覆盖——新增页面路由却不进顶栏，值班员就点不到那一页；
 * 4) 逐页深色横幅——漏挂的页在七页动线里一眼是"另一个系统"。
 * 观感（渐变、间距、层级）靠真机截图，不在这份源码门禁里硬编码像素。
 */

import { existsSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

import { readRepoFile, repoRoot } from '@/testing/repoSource'

function fontsCss(): string {
  return readRepoFile('frontend', 'src', 'styles', 'fonts.css')
}

function appVue(): string {
  return readRepoFile('frontend', 'src', 'App.vue')
}

describe('UI 基座', () => {
  it('字体全部自托管：@font-face 不外链，引到的 woff2 都在磁盘上', () => {
    const css = fontsCss()
    expect(css).not.toMatch(/https?:\/\//)
    const srcs = [...css.matchAll(/url\('([^']+)'\)/g)].map((match) => match[1] as string)
    expect(srcs.length).toBe(7)
    for (const src of srcs) {
      const target = join(repoRoot(), 'frontend', 'src', 'styles', src)
      expect(existsSync(target), `字体文件不在位：${src}`).toBe(true)
    }
  })

  it('两个字族的字重声明齐：Space Grotesk 400–700、JetBrains Mono 400/500/700', () => {
    const blocks = [...fontsCss().matchAll(/@font-face\s*{[^}]*}/g)].map((match) => match[0])
    const has = (family: string, weight: string) =>
      blocks.some(
        (block) => block.includes(`font-family: '${family}'`) && block.includes(`font-weight: ${weight}`),
      )
    for (const weight of ['400', '500', '600', '700']) {
      expect(has('Space Grotesk', weight), `Space Grotesk ${weight} 缺失`).toBe(true)
    }
    for (const weight of ['400', '500', '700']) {
      expect(has('JetBrains Mono', weight), `JetBrains Mono ${weight} 缺失`).toBe(true)
    }
  })

  it('样式入口挂在 main.ts：reset 之后依次引 fonts.css、theme.css', () => {
    const text = readRepoFile('frontend', 'src', 'main.ts')
    const reset = text.indexOf('ant-design-vue/dist/reset.css')
    const fonts = text.indexOf('./styles/fonts.css')
    const theme = text.indexOf('./styles/theme.css')
    expect(reset).toBeGreaterThanOrEqual(0)
    expect(fonts).toBeGreaterThan(reset)
    expect(theme).toBeGreaterThan(fonts)
  })

  it('主色令牌钉在 ConfigProvider：科技蓝 #2563EB', () => {
    const text = appVue()
    expect(text).toMatch(/colorPrimary:\s*'#2563EB'/)
    expect(text).toMatch(/:theme="theme"/)
  })

  it('顶部导航覆盖全部页面路由：router 里的每一页在顶栏都点得到', () => {
    const routerText = readRepoFile('frontend', 'src', 'router.ts')
    const routePaths = [...routerText.matchAll(/path:\s*'\/([a-z]+)'/g)].map((match) => `/${match[1]}`)
    expect(routePaths.length).toBeGreaterThanOrEqual(7)
    const navTargets = [...appVue().matchAll(/to:\s*'\/([a-z]+)'/g)].map((match) => `/${match[1]}`)
    expect(new Set(navTargets)).toEqual(new Set(routePaths))
  })

  it('每个页面都挂 PageHero 深色横幅：新增页面漏挂，就掉出同一套观感', () => {
    const routerText = readRepoFile('frontend', 'src', 'router.ts')
    const names = [...routerText.matchAll(/path:\s*'\/([a-z]+)'/g)].map(
      (match) => `${(match[1] as string)[0]!.toUpperCase()}${(match[1] as string).slice(1)}View.vue`,
    )
    expect(names.length).toBeGreaterThanOrEqual(7)
    for (const name of names) {
      const text = readRepoFile('frontend', 'src', 'views', name)
      expect(text, `${name} 没接 PageHero`).toContain("import PageHero from '@/components/PageHero.vue'")
      expect(text, `${name} 模板里没用 PageHero`).toMatch(/<PageHero[\s>]/)
    }
  })
})
