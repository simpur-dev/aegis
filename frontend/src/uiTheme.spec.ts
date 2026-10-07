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

import { existsSync, readdirSync, readFileSync } from 'node:fs'
import { join, sep } from 'node:path'
import { describe, expect, it } from 'vitest'

import { readRepoFile, repoRoot } from '@/testing/repoSource'

/** 棘轮基线：2026-10-07 阶段 0 由探针量得（458 = 31 个文件里的色值总数）。 */
const COLOR_BASELINE = 458

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

  /**
   * 主色这条从"App.vue 里写着 #2563EB"改成了"派生链在位"（阶段 0 重写，不是删断言）：
   * 真源挪到 `src/theme/tokens.ts`，页面/组件里再出现 hex 就是往回退。
   */
  it('主色来自令牌真源：tokens → antdTheme → ConfigProvider，App.vue 不再自己写 hex', () => {
    const text = appVue()
    expect(text, 'App.vue 里还写着字面 hex：颜色只能来自 src/theme/tokens.ts').not.toMatch(/colorPrimary:\s*'#/)
    expect(text, 'App.vue 没从派生层取主题').toMatch(/import \{ antdTheme \} from '@\/theme\/antd'/)
    expect(text).toMatch(/antdTheme\(resolved\.value\)/)
    expect(text).toMatch(/:theme="theme"/)
    const tokens = readRepoFile('frontend', 'src', 'theme', 'tokens.ts')
    expect(tokens, '主色不再是科技蓝 #2563EB（令牌被改了要说明理由）').toMatch(/base: '#2563EB'/)
    expect(tokens).toMatch(/'brand': brand\.light\.base/)
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

  /**
   * 循环动画必须逐条有减动效兜底（第二十六批立的）。
   *
   * 起因是实测量到的：横幅徽标挂了 `page-hero-pulse … infinite`，而 theme.css 那个
   * `@media (prefers-reduced-motion: reduce)` 块里只写了卡片、顶栏 live 点和轮询点——
   * 新增的那条没人想起来补，于是"开了减少动效"的机器上 7/7 页还在无限循环。
   * 真机门禁管的是屏幕上此刻在跑什么的，这条管的是**新写的动画有没有配套**：
   * 漏一条就红，而不是等下一次体检。
   */
  it('每一支 infinite 动画都在 reduce 块里有名字：新增脉冲却漏兜底，这条先红', () => {
    const theme = readRepoFile('frontend', 'src', 'styles', 'theme.css').replace(/\/\*[\s\S]*?\*\//g, '')
    const calm = /@media \(prefers-reduced-motion: reduce\)\s*\{([\s\S]*?)\n\}/.exec(theme)
    if (!calm) throw new Error('theme.css 里找不到 prefers-reduced-motion 那块兜底——减动效口径整条没了')

    const srcDir = join(repoRoot(), 'frontend', 'src')
    const files = readdirSync(srcDir, { recursive: true })
      .map((p) => String(p))
      .filter((p) => /\.(vue|css)$/.test(p) && !p.includes('fonts.css'))
      .sort()
    const offenders: string[] = []
    for (const rel of files) {
      const text = readFileSync(join(srcDir, rel), 'utf-8').replace(/\/\*[\s\S]*?\*\//g, '')
      for (const match of text.matchAll(/animation:[^;]*infinite[^;]*/g)) {
        const head = text.slice(0, match.index)
        const open = head.lastIndexOf('{')
        const selector = open < 0 ? '' : head.slice(head.lastIndexOf('}', open) + 1, open).trim()
        const tokens = [...selector.matchAll(/\.[\w-]+/g)].map((t) => t[0])
        if (tokens.length === 0) {
          offenders.push(`${rel}：animation 声明找不到所属选择器（写法变了，这条得跟着改）`)
          continue
        }
        /* 伪元素要连 ::before/::after 一起对上，否则写了 .wf__polling 却没关那颗点。 */
        const pseudo = /::?(before|after)(?![\w-])/.exec(selector.slice(-12))?.[1]
        const suffix = pseudo ? `::${pseudo}` : ''
        for (const token of tokens) {
          if (!calm[1].includes(token + suffix)) {
            offenders.push(`${rel}：${selector} 挂着 ${match[0].trim()}，reduce 块里没有 ${token}${suffix}`)
          }
        }
      }
    }
    expect(offenders, `漏了减动效兜底：\n${offenders.join('\n')}`).toHaveLength(0)
  })

  /**
   * 微动效只有一档基准（第二十七批）。
   *
   * 改之前 `src` 里手写的 9 条 `transition` 散在 0.15/0.16/0.18/0.2/0.24 五档，
   * 加上 antd 默认的 0.2/0.3 就是"每一页都在按自己的节奏动"。
   * 现在时长全部走 theme.css 的 `--motion-*` 令牌：**源码里不许再出现字面时长**，
   * 新增一条 0.35s 在这条就红，不用等真机门禁去数屏上的档数（那条管库内部的默认值，这条管我们写的）。
   */
  it('过渡时长只许走 --motion-* 令牌：src 里不许出现字面秒数', () => {
    const theme = readRepoFile('frontend', 'src', 'styles', 'theme.css')
    for (const token of ['--motion-fast', '--motion-base', '--motion-draw']) {
      expect(theme, `theme.css 里的微动效令牌 ${token} 没了——基准无从谈起`).toContain(`${token}:`)
    }
    const srcDir = join(repoRoot(), 'frontend', 'src')
    const files = readdirSync(srcDir, { recursive: true })
      .map((p) => String(p))
      .filter((p) => /\.(vue|css)$/.test(p))
      .sort()
    const strays: string[] = []
    let tokened = 0
    for (const rel of files) {
      const text = readFileSync(join(srcDir, rel), 'utf-8').replace(/\/\*[\s\S]*?\*\//g, '')
      for (const match of text.matchAll(/transition:[^;]*;/g)) {
        const decl = match[0]
        if (decl.includes('var(--motion-')) {
          tokened += 1
          continue
        }
        if (/\d+(\.\d+)?m?s/.test(decl)) strays.push(`${rel}：${decl.trim()}`)
      }
    }
    expect(tokened, '一条走令牌的 transition 都没有——这条在空跑').toBeGreaterThanOrEqual(9)
    expect(strays, `写死了时长而没走令牌：\n${strays.join('\n')}`).toHaveLength(0)
  })

  /**
   * 颜色棘轮（阶段 0）。
   *
   * 基线 458 不是估的：`E:\tmp\color-ratchet-probe.mjs` 用与本条**同一套**走目录 + 剥注释 + 正则量出来
   * （69 个文件、31 个文件有色值，`styles/theme.css` 一支占 103）。为什么不一次清零：逐页换肤的批次
   * 每改一处就往下掉一个数，**只许减不许增**——既不打断页面改造节奏，也不许把硬编码当默认写法。
   *
   * 计数范围：`src/theme/**`（真源，hex 就该只活在那里）与 `*.spec.ts`（要写 hex 才能算对比度）不参与；
   * `api/types.ts` 那 5 支风险基色照样计入——它是语义真源但不给开后门，阶段 3 收进令牌层。
   * 派生层反过来必须零字面色：`antd.ts` 一旦自己写 hex，"改了令牌组件不跟着变"的两张皮就回来了。
   */
  it('颜色字面量只减不增：真源之外不许新增硬编码，派生层一格 hex 都不许有', () => {
    const COUNT = /#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b(?![0-9a-fA-F])|\brgba?\(|\bhsla?\(/g
    /* 判"有没有"要另起一支不带 g 的：带 g 的 .test() 会记住上次 lastIndex，第二、三个文件就成了漏网之鱼。 */
    const HIT = /#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b(?![0-9a-fA-F])|\brgba?\(|\bhsla?\(/
    const strip = (text: string) => text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/<!--[\s\S]*?-->/g, '')

    const srcDir = join(repoRoot(), 'frontend', 'src')
    const files = readdirSync(srcDir, { recursive: true })
      .map((p) => String(p).split(sep).join('/'))
      .filter((p) => /\.(vue|css|ts)$/.test(p))
      .filter((p) => !p.endsWith('.spec.ts') && !p.startsWith('theme/'))
      .sort()
    expect(files.length, 'src 一条都没走到——这条在空跑').toBeGreaterThanOrEqual(60)

    const counted = new Map<string, number>()
    for (const rel of files) {
      const hits = strip(readFileSync(join(srcDir, rel), 'utf-8')).match(COUNT) ?? []
      if (hits.length > 0) counted.set(rel, hits.length)
    }
    const total = [...counted.values()].reduce((sum, n) => sum + n, 0)
    expect(total, '真源外的色值一个都没数到——判据失效').toBeGreaterThanOrEqual(100)
    const heaviest = [...counted.entries()]
      .sort((a, b) => b[1] - a[1])
      .slice(0, 5)
      .map(([rel, n]) => `  ${String(n).padStart(4)} ${rel}`)
      .join('\n')
    expect(
      total,
      `硬编码颜色从基线 ${COLOR_BASELINE} 涨到 ${total}。色值只能写进 src/theme/tokens.ts，` +
        `页面里用 var(--aeg-color-*)。确认是合理减少就把基线改成新值，并在 ` +
        `docs/UI_UX_REDESIGN_BRIEF.md 记一笔。\n最重的五个文件：\n${heaviest}`,
    ).toBeLessThanOrEqual(COLOR_BASELINE)

    for (const rel of ['theme/antd.ts', 'theme/vars.ts', 'theme/useTheme.ts']) {
      expect(strip(readFileSync(join(srcDir, rel), 'utf-8')), `${rel} 出现了字面色：派生层只能从 tokens.ts 取`).not.toMatch(HIT)
    }
  })
})
