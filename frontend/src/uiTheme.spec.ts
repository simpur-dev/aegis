/**
 * UI 基座契约（阶段 0/1 的口径：令牌单一真源 + 双主题）。
 *
 * 钉住这些会静默回归、肉眼又难以及时发现的东西：
 * 1) 字体自托管——“弱网离线可用”是部署形态 P0，外链字体 CDN 在离线环境会掉字；
 * 2) 样式入口——fonts.css / theme.css 不进 main.ts，整套改造等于没做；
 * 3) 顶部导航覆盖——新增页面路由却不进顶栏，值班员就点不到那一页；
 * 4) 基座只读令牌——渐变/模糊/符号前缀一旦漂回来，截图里只多一点点，源码里一眼看得出；
 * 5) 动效只有一条轨——`--motion-*` 与 `--aeg-motion-*` 并存就是"改了令牌页面没改"；
 * 6) 颜色字面量棘轮——真源之外的硬编码只许减不许增（逐页换肤每改一处就往下掉一个数）；
 * 7) 循环动画的减动效兜底、深底字色过 AA（都是真机量出来的坑，写在各自那条的注释里）。
 * 观感（间距、层级、留白）靠真机截图 + e2e 几何门禁，不在这份源码门禁里硬编码像素。
 */

import { existsSync, readdirSync, readFileSync } from 'node:fs'
import { join, sep } from 'node:path'
import { describe, expect, it } from 'vitest'

import { readRepoFile, repoRoot } from '@/testing/repoSource'

/** 棘轮基线：2026-10-07 阶段 1 由探针（E:\tmp\color-count.mjs，与本条同一套走目录/剥注释/正则）量得。
 *  轨迹：458（阶段 0 摸底）→ 335（`theme.css`+`App.vue` 接令牌）→ 228（再把七页共用的灰字与几块白底接上令牌）。
 *  现在最重的三处：`PageHero.vue` 31（藏青横幅，随阶段 3 退役）、`styles/theme.css` 27（同一条横幅的两块深底面）、
 *  `SystemTerminal.vue` 21（固定深底的面板）。`App.vue` 已归零。 */
const COLOR_BASELINE = 228

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

  /**
   * 基座只读令牌（阶段 1 重写这条，原来是"滚动条必须是指定的蓝渐变 + 标签是 12% 雾底三件套"）。
   *
   * 口径换了但判据没松，四条都在：
   * 1. **装饰性写法归零**——`linear-gradient`/`radial-gradient`/`backdrop-filter`/`content: '◆'` 一处都不许有，
   *    这四样正是"糟糕、不专业"判词点名的东西（渐变细线、符号前缀、玻璃模糊）；
   * 2. **色相仍只走蓝系**——那六支紫青/品红照旧逐条禁（这一条比原来更硬：基座现在压根没有字面渐变可用）；
   * 3. **标签三件套必须来自令牌**——四档 hue × bg/line/text 十二个引用，少一个槽位深色主题就破一块；
   * 4. **两条实测硬约束原样保留**——横幅动作钮 36px 下限、藏青横幅里标签"深底反提"（真机量的 1.63:1 那一条）。
   */
  it('基座只读令牌：渐变/模糊/◆ 已退役，标签三件套走 --aeg-tag-*，色相仍只走蓝系', () => {
    const css = readRepoFile('frontend', 'src', 'styles', 'theme.css').replace(/\/\*[\s\S]*?\*\//g, '')
    for (const banned of ['#a7f9ff', '#887dff', '#ff68d6', '#6366f1', '#818cf8', '#a855f7']) {
      expect(css.toLowerCase(), `theme.css 里出现了非蓝系的 ${banned}`).not.toContain(banned)
    }
    const retired: Array<[string, RegExp]> = [
      ['linear-gradient（渐变细线/渐变钮底）', /linear-gradient\s*\(/],
      ['radial-gradient（页面底那团蓝雾）', /radial-gradient\s*\(/],
      ['backdrop-filter（玻璃模糊）', /backdrop-filter/],
      ["标题前的 ◆ 符号前缀", /content:\s*'◆'/],
    ]
    for (const [name, pattern] of retired) {
      expect(pattern.test(css), `theme.css 里还留着 ${name}`).toBe(false)
    }
    for (const hue of ['green', 'orange', 'blue', 'red']) {
      for (const part of ['bg', 'line', 'text']) {
        expect(css, `标签 .ant-tag-${hue} 没从令牌取 --aeg-tag-${hue}-${part}`).toMatch(
          new RegExp(`\\.ant-tag-${hue}[\\s\\S]{0,400}var\\(--aeg-tag-${hue}-${part}\\)`),
        )
      }
    }
    expect(css, '横幅里的动作钮还是 antd small 的 24px 高').toMatch(/\.page-hero \.ant-btn\s*\{[\s\S]{0,400}min-height: 36px/)
    // 浅底那套"浅底深字"搬到藏青横幅上会变成暗底暗字（真机量的 ratio=1.63）——深底必须反过来提亮字
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
   * 微动效只有一档基准，且只有一条轨（第二十七批立的规矩，阶段 1 把双轨合一）。
   *
   * 起因有两个：改之前 `src` 里手写的 9 条 `transition` 散在 0.15/0.16/0.18/0.2/0.24 五档；
   * 而阶段 0 建令牌层之后，`theme.css` 的 `--motion-*` 与 `tokens.ts` 派生的 `--aeg-motion-*`
   * 并存了一段时间——同一件事两个名字，改一个不动另一个就"令牌改了页面没改"。
   * 现在：**时长只能来自 `tokens.ts` 的 `motion`，引用一律 `var(--aeg-motion-*)`**，
   * 出现字面秒数或裸 `--motion-` 都直接红。
   */
  it('过渡时长只许走 --aeg-motion-* 令牌：不许字面秒数，也不许留第二套令牌名', () => {
    const tokens = readRepoFile('frontend', 'src', 'theme', 'tokens.ts')
    const vars = readRepoFile('frontend', 'src', 'theme', 'vars.ts')
    for (const step of ['fast', 'base', 'slow', 'draw']) {
      expect(tokens, `tokens.ts 的 motion 少了 ${step} 档`).toMatch(new RegExp(`\\b${step}: '\\d+(\\.\\d+)?s'`))
    }
    expect(vars, 'vars.ts 没把 motion 注成 --aeg-motion-*').toMatch(/--aeg-motion-|motion-\$\{k\}|\$\{varPrefix\}motion-/)

    const srcDir = join(repoRoot(), 'frontend', 'src')
    const files = readdirSync(srcDir, { recursive: true })
      .map((p) => String(p))
      .filter((p) => /\.(vue|css)$/.test(p))
      .sort()
    const strays: string[] = []
    const doubles: string[] = []
    let tokened = 0
    for (const rel of files) {
      const text = readFileSync(join(srcDir, rel), 'utf-8').replace(/\/\*[\s\S]*?\*\//g, '')
      for (const match of text.matchAll(/transition:[^;]*;/g)) {
        const decl = match[0]
        /* 先查字面时长再数令牌：一条 `transition:` 里常常并列四五个属性，
           只要有一个还写着 `0.35s` 就是没归一。反过来先看到 var 就放过，
           `transform 0.35s ease, background var(--aeg-motion-base) ease` 这种混写法正好漏网（变异检查抓出来的）。 */
        const stray = /\d+(\.\d+)?m?s\b/.exec(decl)
        if (stray) {
          strays.push(`${rel}：${stray[0]} ← ${decl.trim()}`)
          continue
        }
        if (decl.includes('var(--aeg-motion-')) tokened += 1
      }
      /* `--aeg-motion-` 里不含子串 `--motion-`（前面是 "aeg-"，只有一个连字符挨着 motion），
         所以这一支只抓得到旧的那条轨。 */
      if (text.includes('--motion-')) doubles.push(rel)
    }
    expect(tokened, '一条走令牌的 transition 都没有——这条在空跑').toBeGreaterThanOrEqual(11)
    expect(strays, `写死了时长而没走令牌：\n${strays.join('\n')}`).toHaveLength(0)
    expect(doubles, `还留着第二套动效令牌名 --motion-*（应统一为 --aeg-motion-*）：\n${doubles.join('\n')}`).toHaveLength(0)
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

    for (const rel of ['theme/antd.ts', 'theme/vars.ts', 'theme/useTheme.ts', 'theme/chart.ts']) {
      expect(strip(readFileSync(join(srcDir, rel), 'utf-8')), `${rel} 出现了字面色：派生层只能从 tokens.ts 取`).not.toMatch(HIT)
    }
  })
})
