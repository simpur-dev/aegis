/**
 * UI 基座契约（对标 NexusMind 改版，批次一、二）。
 *
 * 钉住四件会静默回归、肉眼又难以及时发现的东西：
 * 1) 字体自托管——“弱网离线可用”是部署形态 P0，外链字体 CDN 在离线环境会掉字；
 * 2) 样式入口——fonts.css / theme.css 不进 main.ts，整套改造等于没做；
 * 3) 顶部导航覆盖——新增页面路由却不进顶栏，值班员就点不到那一页；
 * 4) 逐页深色横幅——漏挂的页在七页动线里一眼是"另一个系统"；
 * 5) 调色板只走蓝系——NexusMind 内部并存五支调色板，漂回紫青那一支肉眼在截图里不容易发现。
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
      expect(text, `${name} 的横幅没给单字形图标`).toMatch(/<PageHero[^>]*icon="[^"]+"/s)
    }
  })

  it('调色板只走蓝系：滚动条不许漂回 NexusMind 的紫青那一支，标签是"雾底 + 同色描边"三件套', () => {
    // 注释里会提到被禁的色值（说明为什么禁），判据只看真的生效声明
    const css = readRepoFile('frontend', 'src', 'styles', 'theme.css').replace(/\/\*[\s\S]*?\*\//g, '')
    for (const banned of ['#a7f9ff', '#887dff', '#ff68d6', '#6366f1', '#818cf8', '#a855f7']) {
      expect(css.toLowerCase(), `theme.css 里出现了非蓝系的 ${banned}`).not.toContain(banned)
    }
    expect(css, '滚动条没走蓝系（NexusMind Home.vue 的 #93c5fd→#60a5fa）').toMatch(
      /linear-gradient\(180deg, #93c5fd, #60a5fa\)/,
    )
    // NexusMind `.badge` 的三件套：12% 雾底 + 同色 1px 描边 + 压深的字（Step1GraphBuild.vue:630-662）
    for (const [hue, rgb] of [
      ['green', '34, 197, 94'],
      ['orange', '245, 158, 11'],
      ['blue', '59, 130, 246'],
      ['red', '239, 68, 68'],
    ] as const) {
      expect(css, `标签 .ant-tag-${hue} 没上雾底`).toMatch(
        new RegExp(`\\.ant-tag-${hue}[\\s\\S]{0,200}background: rgba\\(${rgb}, 0\\.12\\)`),
      )
      expect(css, `标签 .ant-tag-${hue} 没上同色描边`).toMatch(
        new RegExp(`\\.ant-tag-${hue}[\\s\\S]{0,200}border-color: rgba\\(${rgb}, 0\\.2\\)`),
      )
    }
    expect(css, '横幅里的动作钮还是 antd small 的 24px 高').toMatch(/\.page-hero \.ant-btn\s*\{[\s\S]{0,400}min-height: 36px/)
    // 浅底那套"雾底 + 压深字"搬到藏青横幅上会变成暗底暗字（真机量的 1.63:1）——深底必须反过来提亮字
    expect(css, '深色横幅里的标签没有单独的深底配色').toMatch(/\.page-hero \.ant-tag-orange[\s\S]{0,200}color: #fbbf24/)
  })

  /**
   * 深色面板上的字色：`#64748b` 压在 `rgba(15,23,42,.85)` 上只有 3.74:1（自己算过一遍才知道），
   * 而真机门禁那条判据在"还没有事件"时量不到日志行（面板里没有 `.sys-term__row`），
   * 所以这一条只能在源码里钉：深色底上的次要文字一律用 #94a3b8 及以上。
   */
  it('系统输出面板的字色都在深底上过 AA：不许出现 #64748b 这类中灰', () => {
    const vue = readRepoFile('frontend', 'src', 'components', 'SystemTerminal.vue').replace(/\/\*[\s\S]*?\*\//g, '')
    for (const banned of ['#64748b', '#475569', '#334155', '#1e293b']) {
      expect(vue.toLowerCase(), `深底面板上用了 ${banned} 当文字色（3.7:1 一档）`).not.toContain(`color: ${banned}`)
    }
    expect(vue, '面板次要文字没提到 #94a3b8').toMatch(/color: #94a3b8/)
    expect(vue, '面板主文字没提到 #cbd5e1').toMatch(/color: #cbd5e1/)
  })

  /**
   * 芯片一族：形状全站统一（NexusMind `.filter-chip` 的 999 圆角 / 4px 10px / 12px / 600），
   * 颜色只许从"数据自己的那份"取。
   *
   * 为什么在源码层钉：形状回归（漂回 4px 方角）在截图里不显眼，而颜色一旦在组件里写死字面值，
   * 图例就会和图上说的不是一回事——这种"两张皮"最难被看见，第十五批的标签三件套就是这么漂的。
   */
  it('芯片：形状统一 999 圆角，语义色一律取自数据那一份（不许就地写死）', () => {
    const assistant = readRepoFile('frontend', 'src', 'views', 'AssistantView.vue').replace(/\/\*[\s\S]*?\*\//g, '')
    expect(assistant, '助手页动作芯片不是芯片一族（圆角该是 999px）').toMatch(/\.caps__chip\s*\{[\s\S]{0,400}border-radius: 999px/)
    expect(assistant, '芯片矮于 26px，点起来是"文字链"不是"按钮"').toMatch(/\.caps__chip\s*\{[\s\S]{0,400}min-height: 26px/)

    const panel = readRepoFile('frontend', 'src', 'components', 'map', 'MapPanel.vue').replace(/\/\*[\s\S]*?\*\//g, '')
    expect(panel, '风险芯片的颜色不是从 riskLegend() 那一份取的').toMatch(/chipStyle\(entry\.colorCss\)/)
    for (const banned of ['#cf1322', '#fa8c16', '#fadb14', '#1677ff']) {
      expect(panel.toLowerCase(), `MapPanel 里写死了风险色 ${banned}（图例与图上会分成两张皮）`).not.toContain(`: ${banned}`)
    }
    // 等级未知那一格是唯一允许的字面色（它不在 RISK_COLORS 里，也没有对应的图上要素）
    expect(panel, '「等级未知」的底色写法变了（原来是 #bfbfbf）').toContain('#bfbfbf')
  })
})
