/**
 * UI 不变量门禁（对标 NexusMind 第三批，2026-10-05 立）。
 *
 * 为什么要有这一份：真机体检量出过四类肉眼会漏、但一改样式就会复发的缺陷——
 * ① 顶栏三组文字在 ≤1280 互相压字（绝对居中的步进器盖住副标题与状态胶囊）；
 * ② 状态胶囊被网格列裁掉半颗（box 还在、字看不见，重叠检查看不出来，所以另加 clip 检查）；
 * ③ 落地页装配事实条那条长串（dataset=内置预案模板…）一路顶出卡片、压到右邻那一行；
 * ④ 灰字对比度不到 4.5:1（步进器未选态 3.17、页脚 3.05、绿色标签 3.37）。
 * 判据全部来自包围盒与计算样式；底色会解析渐变（横幅/徽标/主按钮都是渐变，
 * 只读 backgroundColor 会得到 transparent，白字就被误判成"白底白字"）。
 *
 * 跑法（与地图视觉门禁同一档：构建产物 + 本机 chromium）：
 *   npx playwright test e2e/ui-overlap.spec.ts
 */
import { expect, test } from '@playwright/test'

const PAGES: Array<[string, string]> = [
  ['dashboard', '/dashboard'],
  ['monitor', '/monitor'],
  ['warnings', '/warnings'],
  ['map', '/map'],
  ['workflow', '/workflow'],
  ['metrics', '/metrics'],
  ['assistant', '/assistant'],
]
const WIDTHS = [1440, 1280, 1200, 1100, 1000, 900]

/** 对比度豁免：这些位置的字按 antd/交互语义本就该是"非正文"色，不是内容读不到。 */
const CONTRAST_ALLOW = [
  /* 禁用态为什么豁免：浅底上灰字＝"点不动"的提示（WCAG 1.4.11 也把"非活动组件"排除在外）。
     但**深色横幅里不成立**——那边是暗底暗字，所以 `.page-hero__actions` 另走 heroLow 一条，不豁免。 */
  '.ant-btn[disabled]',
  '.ant-btn-loading',
  '.ant-select-selection-placeholder',
  '.ant-table-placeholder',
  '.ant-empty-description',
  '.cesium-widget-credits',
].join(',')

const inspect = (opts: { contrastAllow: string; scope?: string; inOverlay?: boolean }) => {
  const { contrastAllow, scope, inOverlay } = opts
  const pathOf = (el: Element) => {
    const parts: string[] = []
    let cur: Element | null = el
    while (cur && cur !== document.body && parts.length < 4) {
      let name = cur.tagName.toLowerCase()
      const cls = (typeof cur.className === 'string' ? cur.className : '').trim().split(/\s+/).filter(Boolean).slice(0, 2)
      if (cls.length) name += '.' + cls.join('.')
      parts.unshift(name)
      cur = cur.parentElement
    }
    return parts.join('>')
  }
  /* 弹层本身也要被体检：跑在抽屉/弹窗内部时不能把 .ant-modal/.ant-drawer 一起跳过 */
  const skipSel = inOverlay
    ? '.map-frame,.vue-flow,.cesium-viewer,.ant-message,.ant-notification,canvas'
    : '.map-frame,.vue-flow,.cesium-viewer,.ant-modal,.ant-drawer,.ant-message,.ant-notification,canvas'
  const skip = (el: Element) => !!(el as HTMLElement).closest(skipSel)
  /* 真正"被画到屏幕上"的那块矩形：外壳改成"正文自己滚"之后，滚出滚动视口的节点
     包围盒仍在文档坐标里躺着，会和页脚之类的东西几何相交却没真的叠上去。
     逐层裁到最近的可滚动祖先的可视区，完全在外面就判 null（不参与重叠/出屏判定）。 */
  type Rect = { left: number; top: number; right: number; bottom: number }
  const clip = (box: Rect, a: Rect): Rect | null => {
    const next = {
      left: Math.max(box.left, a.left),
      top: Math.max(box.top, a.top),
      right: Math.min(box.right, a.right),
      bottom: Math.min(box.bottom, a.bottom),
    }
    return next.right - next.left <= 0 || next.bottom - next.top <= 0 ? null : next
  }
  const viewportBox = (): Rect => ({ left: 0, top: 0, right: innerWidth, bottom: innerHeight })
  const paintedRect = (el: Element): Rect | null => {
    const r0 = el.getBoundingClientRect()
    let box: Rect = { left: r0.left, top: r0.top, right: r0.right, bottom: r0.bottom }
    /* fixed 元素不受滚动容器裁切（它相对视口定位）。上一版没区分这一点，
       于是把"盖住顶栏的一条 fixed 错误条"判成没重叠——变异实验里暴露的。 */
    let anc: Element | null = el.parentElement
    while (anc) {
      const cs = getComputedStyle(anc)
      if (cs.position === 'fixed') break
      if (/auto|scroll|hidden|clip/.test(`${cs.overflowX} ${cs.overflowY}`)) {
        const a = anc.getBoundingClientRect()
        const next = clip(box, { left: a.left, top: a.top, right: a.right, bottom: a.bottom })
        if (!next) return null
        box = next
      }
      anc = anc.parentElement
    }
    return clip(box, viewportBox())
  }
  const visible = (el: Element) => {
    const cs = getComputedStyle(el)
    if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) < 0.05) return false
    const r = el.getBoundingClientRect()
    return r.width > 1 && r.height > 1
  }
  const leaves = [...document.querySelectorAll(scope ? `${scope} *` : 'body *')].filter(
    (el) => el.children.length === 0 && !skip(el) && visible(el) && (el.textContent ?? '').trim().length > 0,
  )

  const overlap: string[] = []
  const boxes = leaves
    .map((el) => ({ el, r: paintedRect(el) }))
    .filter((b): b is { el: Element; r: Rect } => b.r !== null)
  for (let i = 0; i < boxes.length; i++) {
    for (let j = i + 1; j < boxes.length; j++) {
      const a = boxes[i]
      const b = boxes[j]
      if (a.el.contains(b.el) || b.el.contains(a.el)) continue
      const ox = Math.min(a.r.right, b.r.right) - Math.max(a.r.left, b.r.left)
      const oy = Math.min(a.r.bottom, b.r.bottom) - Math.max(a.r.top, b.r.top)
      if (ox > 4 && oy > 4) {
        overlap.push(`"${(a.el.textContent ?? '').trim().slice(0, 14)}" × "${(b.el.textContent ?? '').trim().slice(0, 14)}" area=${Math.round(ox * oy)} | ${pathOf(a.el)} || ${pathOf(b.el)}`)
      }
    }
  }

  const beyond: string[] = []
  const clipped: string[] = []
  for (const el of leaves) {
    const cs = getComputedStyle(el)
    const r = paintedRect(el)
    if (!r) continue
    if (r.right > innerWidth + 1 || r.left < -1) {
      let anc = el.parentElement
      let scrollable = false
      while (anc) {
        if (/auto|scroll/.test(getComputedStyle(anc).overflowX)) { scrollable = true; break }
        anc = anc.parentElement
      }
      if (!scrollable) beyond.push(`"${(el.textContent ?? '').trim().slice(0, 20)}" ${pathOf(el)} [${Math.round(r.left)},${Math.round(r.right)}]`)
    }
    if (/hidden|clip/.test(`${cs.overflowX} ${cs.overflowY}`) && cs.textOverflow !== 'ellipsis') {
      if (el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1) {
        clipped.push(`"${(el.textContent ?? '').trim().slice(0, 20)}" ${pathOf(el)} ${el.scrollWidth}x${el.scrollHeight} vs ${el.clientWidth}x${el.clientHeight}`)
      }
    }
  }

  /* 按钮里的字被挤成两行（"打/开"竖排）：量文本节点自己的行盒，
     不用按钮高度判——带内边距的 flex 按钮会误报。 */
  const folded: string[] = []
  for (const btn of document.querySelectorAll('button')) {
    if (!visible(btn)) continue
    const node = [...btn.childNodes].find((n) => n.nodeType === 3 && (n.textContent ?? '').trim().length > 0)
    if (!node) continue
    const range = document.createRange()
    range.selectNodeContents(node)
    const tops = new Set([...range.getClientRects()].map((rect) => Math.round(rect.top)))
    if (tops.size > 1) folded.push(`"${(node.textContent ?? '').trim().slice(0, 10)}" 折成 ${tops.size} 行 | ${pathOf(btn)}`)
  }

  /* 短语被折行（"已完成"→"已完/成"、"打开"→"打/开"）：只盯"不含空格也不含天然断点的短词"，
     长短语正常换行、以及"在途/上限""触达（演练口径）"这类在斜号与括号处断开的不算缺陷。 */
  const NATURAL_BREAK = /[\s/（）()|·—-]/
  for (const el of leaves) {
    const text = (el.textContent ?? '').trim()
    if (!text || text.length > 8 || NATURAL_BREAK.test(text)) continue
    const range = document.createRange()
    range.selectNodeContents(el)
    const tops = new Set([...range.getClientRects()].map((rect) => Math.round(rect.top)))
    if (tops.size > 1) folded.push(`"${text}" 折成 ${tops.size} 行 | ${pathOf(el)}`)
  }

  /* 顶栏三列互不侵入（按**容器**包围盒量，不按文本叶子）：
     右列的胶囊是 nowrap 的，故障文案一长整列就长出 `1fr` 轨道、往左压进步进器。
     文本叶子那套重叠检查在这一处抓不到（实测：容器级压 63×28px，叶子级报 0），
     所以单独钉一条容器级的。 */
  const navIntrude: string[] = []
  {
    const pairs: [string, string][] = [
      ['.nav-brand', '.nav-center'],
      ['.immersive-stepper', '.nav-status'],
    ]
    for (const [x, y] of pairs) {
      const a = document.querySelector(x)?.getBoundingClientRect()
      const b = document.querySelector(y)?.getBoundingClientRect()
      if (!a || !b) continue
      const ox = Math.min(a.right, b.right) - Math.max(a.left, b.left)
      const oy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top)
      if (ox > 2 && oy > 2) navIntrude.push(`${x} 与 ${y} 压 ${Math.round(ox)}×${Math.round(oy)}px（${x} 右缘=${Math.round(a.right)}，${y} 左缘=${Math.round(b.left)}）`)
    }
  }

  /* 顶栏三组都被 overflow 管着：内容比列宽就"静默裁掉"，包围盒还在原处——
     重叠检查看不见它，所以单独量 scrollWidth 与 clientWidth 的差。 */
  const navClip: string[] = []
  for (const sel of ['.nav-brand', '.nav-center', '.nav-status']) {
    const el = document.querySelector(sel)
    if (el && el.scrollWidth > el.clientWidth + 1) navClip.push(`${sel} scrollWidth=${el.scrollWidth} > clientWidth=${el.clientWidth}`)
  }

  type RGBA = { r: number; g: number; b: number; a: number }
  const parse = (c: string): RGBA | null => {
    const m = /rgba?\(([^)]+)\)/.exec(c)
    if (!m) return null
    const p = m[1].split(/[,\s/]+/).filter(Boolean).map(Number)
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 }
  }
  const gradFirst = (el: Element): RGBA | null => {
    const img = getComputedStyle(el).backgroundImage
    if (!img || img === 'none' || !img.includes('gradient(')) return null
    const rgb = /rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:[,\s/]+([\d.]+))?\s*\)/.exec(img)
    if (rgb) return { r: +rgb[1], g: +rgb[2], b: +rgb[3], a: rgb[4] === undefined ? 1 : +rgb[4] }
    const hex = /#([0-9a-f]{3,8})/i.exec(img)
    if (hex) {
      const h = hex[1].length === 3 ? hex[1].split('').map((c) => c + c).join('') : hex[1]
      return { r: parseInt(h.slice(0, 2), 16), g: parseInt(h.slice(2, 4), 16), b: parseInt(h.slice(4, 6), 16), a: 1 }
    }
    return null
  }
  const lum = (c: { r: number; g: number; b: number }) => {
    const f = (v: number) => ((v /= 255) <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4)
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b)
  }
  const ratio = (fg: { r: number; g: number; b: number }, bg: { r: number; g: number; b: number }) => {
    const l1 = lum(fg)
    const l2 = lum(bg)
    return Number(((Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05)).toFixed(2))
  }
  const bgOf = (el: Element): RGBA => {
    const layers: RGBA[] = []
    let cur: Element | null = el
    while (cur) {
      const solid = parse(getComputedStyle(cur).backgroundColor)
      const layer = solid && solid.a > 0 ? solid : gradFirst(cur)
      if (layer && layer.a > 0) {
        layers.push(layer)
        if (layer.a >= 1) break
      }
      cur = cur.parentElement
    }
    if (!layers.length) return { r: 255, g: 255, b: 255, a: 1 }
    let base = layers[layers.length - 1]
    for (let i = layers.length - 2; i >= 0; i--) {
      const l = layers[i]
      base = { r: l.r * l.a + base.r * (1 - l.a), g: l.g * l.a + base.g * (1 - l.a), b: l.b * l.a + base.b * (1 - l.a), a: 1 }
    }
    return base
  }
  const lowContrast: string[] = []
  for (const el of leaves) {
    if (contrastAllow && (el as HTMLElement).closest(contrastAllow)) continue
    if ((el as HTMLElement).closest('[aria-hidden="true"]')) continue
    const cs = getComputedStyle(el)
    const fg = parse(cs.color)
    if (!fg) continue
    const bg = bgOf(el)
    // 前景色自带 alpha（如白 72%）要先合成到底色上，否则算出来的是虚高的对比度
    const eff = fg.a < 1 ? { r: fg.r * fg.a + bg.r * (1 - fg.a), g: fg.g * fg.a + bg.g * (1 - fg.a), b: fg.b * fg.a + bg.b * (1 - fg.a) } : fg
    const size = parseFloat(cs.fontSize)
    const bold = Number(cs.fontWeight) >= 700
    const need = size >= 24 || (size >= 18.66 && bold) ? 3 : 4.5
    const got = ratio(eff, bg)
    if (got < need) lowContrast.push(`ratio=${got}<${need} "${(el.textContent ?? '').trim().slice(0, 16)}" ${cs.color} on rgb(${Math.round(bg.r)},${Math.round(bg.g)},${Math.round(bg.b)}) ${size}px/${cs.fontWeight} | ${pathOf(el)}`)
  }

  /* 深色横幅里的按钮**不吃**上面那条"禁用态豁免"。豁免的理由是"浅底上灰一点=点不动"，
     但 antd 的禁用样式是"浅底 + 25% 黑字"，搬进藏青横幅就成了暗底暗字——不是弱化，是读不到。
     横幅里每个按钮（含禁用）都得过 4.5:1。 */
  const heroLow: string[] = []
  let heroBtnCount = 0
  for (const el of leaves) {
    if (!(el as HTMLElement).closest('.page-hero__actions')) continue
    heroBtnCount += 1
    const cs = getComputedStyle(el)
    const fg = parse(cs.color)
    if (!fg) continue
    const bg = bgOf(el)
    const eff = fg.a < 1 ? { r: fg.r * fg.a + bg.r * (1 - fg.a), g: fg.g * fg.a + bg.g * (1 - fg.a), b: fg.b * fg.a + bg.b * (1 - fg.a) } : fg
    const got = ratio(eff, bg)
    if (got < 4.5) {
      const btn = (el as HTMLElement).closest('button')
      heroLow.push(`hero ratio=${got}<4.5 "${(el.textContent ?? '').trim().slice(0, 16)}" ${cs.color} on rgb(${Math.round(bg.r)},${Math.round(bg.g)},${Math.round(bg.b)})${btn?.disabled ? ' [disabled]' : ''}`)
    }
  }

  /* 顶栏状态文字被省略号截断＝那条真话少了一半。上面两条都看不见它：
     navClip 判的是容器 scrollWidth>clientWidth，而子项自己缩了、容器没超；
     clipped 判据对"带 text-overflow:ellipsis"是放过的（那是有意的）。
     ≥1280 档顶栏有位置，出现省略号就是缺陷；更窄的档是有意收字，不判。 */
  const navTruncated: string[] = []
  if (innerWidth >= 1280) {
    for (const el of [...document.querySelectorAll('.nav-status .status-pill__text')]) {
      if (el.scrollWidth > el.clientWidth + 1) {
        navTruncated.push(`"${(el.textContent ?? '').trim()}" 需要 ${el.scrollWidth}px 只有 ${el.clientWidth}px`)
      }
    }
  }

  /* 横幅里的动作钮是这一页的主入口。antd 的 small 只有 24px 高，在值班台上偏小
     （NexusMind 给主操作的是 46px 全宽钮）；判据取 ≥34px，钉住"别再退回 small"。 */
  const heroShortBtns: string[] = []
  for (const btn of [...document.querySelectorAll('.page-hero__actions .ant-btn')]) {
    const r = (btn as HTMLElement).getBoundingClientRect()
    if (r.height > 0 && r.height < 34) heroShortBtns.push(`"${(btn.textContent ?? '').trim().slice(0, 10)}" 高=${Math.round(r.height)}px`)
  }

  /* 中文值班台上不该出现 antd 默认的英文空态（"No data" 这类）。
     这一整类缺陷此前是靠人一张张截图发现的（链路表、预警表、智能体表各修过一次），
     现在钉成判据：任何一档状态下，空态文案里没有纯英文句子就算中。 */
  const englishEmpty = [...document.querySelectorAll('.ant-empty-description, .ant-table-placeholder, .ant-empty-normal')]
    .map((el) => (el.textContent ?? '').trim())
    .filter((t) => t.length > 0 && !/[\u4e00-\u9fa5]/.test(t))

  const center = document.querySelector('.nav-center')?.getBoundingClientRect()
  return {
    url: location.pathname,
    vw: innerWidth,
    /* 体检自身也得可证伪：判据跑在几个节点上要说得出数，
       否则"选择器写错了 → 一个都没看 → 全绿"这种空跑没人发现。 */
    leafCount: leaves.length,
    overlap: overlap.slice(0, 8),
    overlapCount: overlap.length,
    beyond: beyond.slice(0, 8),
    beyondCount: beyond.length,
    clipped: clipped.slice(0, 8),
    clippedCount: clipped.length,
    folded: folded.slice(0, 8),
    foldedCount: folded.length,
    navClip,
    navIntrude,
    navTruncated,
    lowContrast: lowContrast.slice(0, 10),
    lowContrastCount: lowContrast.length,
    heroLow: heroLow.slice(0, 6),
    heroLowCount: heroLow.length,
    heroBtnCount,
    heroShortBtns,
    englishEmpty: englishEmpty.slice(0, 6),
    englishEmptyCount: englishEmpty.length,
    centerOffset: center ? Number(Math.abs(center.left + center.width / 2 - innerWidth / 2).toFixed(2)) : null,
  }
}

for (const width of WIDTHS) {
  test.describe(`顶栏与正文不变量 @${width}`, () => {
    test.beforeEach(async ({ page }) => {
      await page.setViewportSize({ width, height: 900 })
    })
    for (const [name, path] of PAGES) {
      test(`七页之一 ${name}：无重叠、不出屏、无裁字、顶栏不静默裁切、对比度达标、步进器居中`, async ({ page }) => {
        await page.goto(path)
        await page.waitForSelector('.page-hero', { timeout: 45_000 })
        /* 先等网络静 + 再"量到内容够了才继续"：整批跑（71 项）时前面的地图项要吃十几秒 GPU，
           监测页的台账表会晚于固定 sleep 才落地，leafCount 下限就被误触发
           （隔离跑全绿、整批偶发红，量的都是同一个页面）。这不是页面缺陷，是探针的等待方式。
           重试到 12 秒还起不来，那就当真有问题处理。 */
        await page.waitForLoadState('networkidle').catch(() => {})
        await page.waitForTimeout(name === 'map' ? 12_000 : 2_500)
        let r = await page.evaluate(inspect, { contrastAllow: CONTRAST_ALLOW })
        for (let attempt = 0; attempt < 6 && r.leafCount <= 30; attempt++) {
          console.log(`RETRY ${name}@${width} leafCount=${r.leafCount} 还没落地，等 2s 再量`)
          await page.waitForTimeout(2_000)
          r = await page.evaluate(inspect, { contrastAllow: CONTRAST_ALLOW })
        }
        console.log(`UI ${name}@${width} leaves=${r.leafCount} overlap=${r.overlapCount} beyond=${r.beyondCount} clipped=${r.clippedCount} folded=${r.foldedCount} navClip=${r.navClip.length} navIntrude=${r.navIntrude.length} navTrunc=${r.navTruncated.length} lowContrast=${r.lowContrastCount} heroBtn=${r.heroBtnCount} heroLow=${r.heroLowCount} heroShort=${r.heroShortBtns.length} centerOffset=${r.centerOffset}`)
        expect(r.leafCount, `${name}@${width} 页面一个文本节点都没看到——判据在空跑`).toBeGreaterThan(30)
        expect(r.overlapCount, `${name}@${width} 重叠明细：\n${r.overlap.join('\n')}`).toBe(0)
        expect(r.beyondCount, `${name}@${width} 有内容出屏：\n${r.beyond.join('\n')}`).toBe(0)
        expect(r.clippedCount, `${name}@${width} 有文字被裁：\n${r.clipped.join('\n')}`).toBe(0)
        expect(r.foldedCount, `${name}@${width} 有按钮文字折行（"打/开"竖排）：\n${r.folded.join('\n')}`).toBe(0)
        expect(r.navClip, `${name}@${width} 顶栏把内容裁掉了（列宽不够）：\n${r.navClip.join('\n')}`).toHaveLength(0)
        expect(r.navTruncated, `${name}@${width} 顶栏状态文字被省略号截断：
${r.navTruncated.join('\n')}`).toHaveLength(0)
        expect(r.navIntrude, `${name}@${width} 顶栏三列互相侵入：\n${r.navIntrude.join('\n')}`).toHaveLength(0)
        expect(r.lowContrastCount, `${name}@${width} 有字对比度不足：\n${r.lowContrast.join('\n')}`).toBe(0)
        expect(r.heroLowCount, `${name}@${width} 横幅里的按钮在深底上读不清：\n${r.heroLow.join('\n')}`).toBe(0)
        expect(r.heroShortBtns, `${name}@${width} 横幅主入口按钮太矮（<34px）：\n${r.heroShortBtns.join('\n')}`).toHaveLength(0)
        expect(r.englishEmptyCount, `${name}@${width} 出现纯英文空态文案：\n${r.englishEmpty.join('\n')}`).toBe(0)
        if (r.centerOffset !== null) expect(r.centerOffset, `${name}@${width} 步进器偏离中线`).toBeLessThanOrEqual(1)
      })
    }
  })
}

/**
 * 弹层内的同一套判据。此前 `skip()` 把 .ant-modal/.ant-drawer 整块跳过——
 * 也就是说抽屉/弹窗里的排版从来没有机器证据，而值班员恰恰是在这些地方做决定的。
 * `minLeaves` 按各弹层实际有的文本节点定（placeholder 是伪元素、不算节点），
 * 作用只有一个：选择器写错导致一个都没看时，不许悄悄全绿。
 */
const OVERLAYS: Array<{ name: string; path: string; trigger: string; scope: string; minLeaves: number }> = [
  { name: 'monitor-report', path: '/monitor', trigger: '[data-testid="open-report"]', scope: '.ant-modal-content', minLeaves: 8 },
  { name: 'assistant-report', path: '/assistant', trigger: '[data-testid="open-report"]', scope: '.ant-modal-content', minLeaves: 8 },
  { name: 'warnings-detail', path: '/warnings', trigger: 'role=button[name="详情"]', scope: '.ant-drawer-content', minLeaves: 40 },
]

for (const width of [1440, 1280, 1100, 1000]) {
  test.describe(`弹层内不变量 @${width}`, () => {    test.beforeEach(async ({ page }) => {
      await page.setViewportSize({ width, height: 900 })
    })
    for (const c of OVERLAYS) {
      test(`${c.name}：弹层内无重叠、不出屏、无裁字、按钮不折行、对比度达标`, async ({ page }) => {
        await page.goto(c.path)
        await page.waitForSelector('.page-hero', { timeout: 45_000 })
        await page.waitForLoadState('networkidle')
        await page.waitForTimeout(2_500)
        await page.locator(c.trigger).first().click()
        await page.waitForSelector(c.scope, { timeout: 15_000 })
        await page.waitForTimeout(1_200)
        const r = await page.evaluate(inspect, { contrastAllow: CONTRAST_ALLOW, scope: c.scope, inOverlay: true })
        console.log(`OVERLAY ${c.name}@${width} leaves=${r.leafCount} overlap=${r.overlapCount} beyond=${r.beyondCount} clipped=${r.clippedCount} folded=${r.foldedCount} lowContrast=${r.lowContrastCount}`)
        await page.screenshot({ path: `test-results/ui-audit/overlay-${c.name}-${width}.png` })
        expect(r.leafCount, `${c.name}@${width} 弹层里只看到 ${r.leafCount} 个文本节点（应 ≥${c.minLeaves}）——判据在空跑`).toBeGreaterThanOrEqual(c.minLeaves)
        expect(r.overlapCount, `${c.name}@${width} 弹层内重叠：\n${r.overlap.join('\n')}`).toBe(0)
        expect(r.beyondCount, `${c.name}@${width} 弹层内出屏：\n${r.beyond.join('\n')}`).toBe(0)
        expect(r.clippedCount, `${c.name}@${width} 弹层内文字被裁：\n${r.clipped.join('\n')}`).toBe(0)
        expect(r.foldedCount, `${c.name}@${width} 弹层内按钮折行：\n${r.folded.join('\n')}`).toBe(0)
        expect(r.lowContrastCount, `${c.name}@${width} 弹层内对比度不足：\n${r.lowContrast.join('\n')}`).toBe(0)
      })
    }
  })
}

/**
 * 弹层里的键盘焦点：此前"焦点态"那一项只走正文（Tab 七页），弹层打开后焦点去哪儿没人管。
 * 键盘用户开一个抽屉，若 Tab 会溜到背后的表格上，就再也回不到弹层里（也关不掉）。
 * 判两条：Tab 十二次焦点始终在弹层内；Esc 能关掉。
 */
test.describe('弹层焦点不外泄 @1440', () => {
  for (const c of OVERLAYS) {
    test(`${c.name}：Tab 十二步焦点不逃出弹层，Esc 关得掉`, async ({ page }) => {
      await page.setViewportSize({ width: 1440, height: 900 })
      await page.goto(c.path)
      await page.waitForSelector('.page-hero', { timeout: 45_000 })
      await page.waitForLoadState('networkidle').catch(() => {})
      await page.waitForTimeout(2_500)
      await page.locator(c.trigger).first().click()
      await page.waitForSelector(c.scope, { timeout: 15_000 })
      await page.waitForTimeout(1_200)
      let inside = 0
      const escapes: string[] = []
      for (let i = 0; i < 12; i += 1) {
        await page.keyboard.press('Tab')
        const a = await page.evaluate(() => {
          const el = document.activeElement as HTMLElement | null
          if (!el || el === document.body) return { inOverlay: false, what: 'body' }
          return {
            /* 判"有没有跑到页面背后去"，不是"是否严格在 content 里"：rc-dialog 的焦点锁
               会在 .ant-modal-wrap 里放哨兵节点，落到哨兵上不算逃出去。 */
            inOverlay: !!el.closest('.ant-modal, .ant-modal-wrap, .ant-drawer'),
            what: `${el.tagName.toLowerCase()}.${String(el.className ?? '').split(/\s+/)[0]}`,
          }
        })
        if (a.inOverlay) inside += 1
        else escapes.push(`第 ${i + 1} 次 Tab 落在弹层外：${a.what}`)
      }
      await page.keyboard.press('Escape')
      await page.waitForTimeout(700)
      const stillOpen = await page.locator(c.scope).first().isVisible().catch(() => false)
      console.log(`FOCUSTRAP ${c.name} inside=${inside}/12 escapes=${escapes.length} stillOpen=${stillOpen}`)
      expect(inside, `${c.name}：弹层里一次都没落到焦点——判据在空跑`).toBeGreaterThan(0)
      expect(escapes, `${c.name}：焦点逃出弹层\n${escapes.join('\n')}`).toHaveLength(0)
      expect(stillOpen, `${c.name}：Esc 关不掉，键盘用户出不去`).toBe(false)
    })
  }
})

/**
 * 故障态与空数据态过同一套判据。这两档恰恰是文案最长的时候（错误横幅、"取不到"的说明），
 * 而此前 54 项全在"后端正常 + 有数据"下跑——降级画面没人看过一眼。
 * 判据不放宽：重叠/出屏/裁字/折行/对比度一律为 0；只把 leafCount 下限降到 12
 * （降级页本就少几块内容，但仍必须有顶栏 + 横幅 + 那句"为什么是空的"）。
 */
const killBackend = async (page: import('@playwright/test').Page) => {
  await page.route('**/readyz', (route) => route.abort())
  await page.route('**/api/**', (route) => route.abort())
}

for (const width of [1440, 1100]) {
  test.describe(`故障态不变量 @${width}`, () => {
    test.beforeEach(async ({ page }) => {
      await page.setViewportSize({ width, height: 900 })
      await killBackend(page)
    })
    for (const [name, path] of PAGES) {
      test(`后端不可达时 ${name} 仍不重叠、不出屏、不裁字、对比度达标`, async ({ page }) => {
        await page.goto(path)
        await page.waitForSelector('.page-hero', { timeout: 45_000 })
        /* 先等顶栏真翻成**最长那句**故障文案再量：故障态的胶囊比在线态长
           （"后端不可达" + "事件流已中断"），只等 health 翻脸是不够的——SSE 那条先是
           "事件流重连中"（短 65px），此时右列已经长出轨道、但还没长到压住步进器，
           量到的就是"假绿"。变异实测：只等 /不可达/ 时这条判据抓不到右列侵入 78px。 */
        await page
          .waitForFunction(
            () =>
              /不可达/.test(document.querySelector('.nav-status')?.textContent ?? '') &&
              /已中断/.test(document.querySelector('.nav-status')?.textContent ?? ''),
            undefined,
            { timeout: 25_000 },
          )
          .catch(() => {})
        await page.waitForTimeout(1_000)
        const r = await page.evaluate(inspect, { contrastAllow: CONTRAST_ALLOW })
        console.log(`FAULT ${name}@${width} leaves=${r.leafCount} overlap=${r.overlapCount} beyond=${r.beyondCount} clipped=${r.clippedCount} folded=${r.foldedCount} navClip=${r.navClip.length} navIntrude=${r.navIntrude.length} navTrunc=${r.navTruncated.length} lowContrast=${r.lowContrastCount} heroLow=${r.heroLowCount}`)
        await page.screenshot({ path: `test-results/ui-audit/fault-${name}-${width}.png` })
        expect(r.leafCount, `${name}@${width} 故障态只看到 ${r.leafCount} 个文本节点——判据在空跑`).toBeGreaterThanOrEqual(12)
        expect(r.overlapCount, `${name}@${width} 故障态重叠：\n${r.overlap.join('\n')}`).toBe(0)
        expect(r.beyondCount, `${name}@${width} 故障态出屏：\n${r.beyond.join('\n')}`).toBe(0)
        expect(r.clippedCount, `${name}@${width} 故障态文字被裁：\n${r.clipped.join('\n')}`).toBe(0)
        expect(r.foldedCount, `${name}@${width} 故障态按钮折行：\n${r.folded.join('\n')}`).toBe(0)
        expect(r.lowContrastCount, `${name}@${width} 故障态对比度不足：\n${r.lowContrast.join('\n')}`).toBe(0)
        expect(r.heroLowCount, `${name}@${width} 故障态横幅按钮读不清：\n${r.heroLow.join('\n')}`).toBe(0)
        expect(r.navClip, `${name}@${width} 故障态顶栏静默裁字：\n${r.navClip.join('\n')}`).toHaveLength(0)
        expect(r.navTruncated, `${name}@${width} 故障态顶栏状态文字被截断：
${r.navTruncated.join('\n')}`).toHaveLength(0)
        expect(r.navIntrude, `${name}@${width} 故障态顶栏三列互相侵入：\n${r.navIntrude.join('\n')}`).toHaveLength(0)
        expect(r.heroShortBtns, `${name}@${width} 故障态横幅主入口按钮太矮：\n${r.heroShortBtns.join('\n')}`).toHaveLength(0)
        expect(r.englishEmptyCount, `${name}@${width} 故障态出现纯英文空态：\n${r.englishEmpty.join('\n')}`).toBe(0)
      })
    }
  })
}

test.describe('空数据态不变量 @1440', () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.route('**/api/v1/warnings*', (route) => route.fulfill({ json: { items: [], total: 0, count: 0 } }))
    await page.route('**/api/v1/events*', (route) => route.fulfill({ json: { items: [], count: 0 } }))
    await page.route('**/api/v1/telemetry*', (route) => route.fulfill({ json: { items: [], count: 0 } }))
    await page.route('**/api/v1/agents*', (route) => route.fulfill({ json: { items: [], count: 0 } }))
    await page.route('**/api/v1/collaboration*', (route) => route.fulfill({ json: { items: [], count: 0 } }))
    await page.route('**/api/v1/stations*', (route) => route.fulfill({ json: { items: [], count: 0 } }))
  })
  for (const [name, path] of [
    ['warnings', '/warnings'],
    ['dashboard', '/dashboard'],
    ['monitor', '/monitor'],
    ['map', '/map'],
  ] as const) {
    test(`没有数据时 ${name} 的空态排版达标`, async ({ page }) => {
      await page.goto(path)
      await page.waitForSelector('.page-hero', { timeout: 45_000 })
      await page.waitForTimeout(2_500)
      const r = await page.evaluate(inspect, { contrastAllow: CONTRAST_ALLOW })
      const empty = await page.evaluate(() => {
        /* 只认 .empty-hero：上一版写了 '.empty-hero, .ant-empty'，于是态势页其实是
           匹配到了风险网格那块 a-empty 才过的——空态判据不能认"页面上随便哪块空态" */
        const heroes = [...document.querySelectorAll('.empty-hero')].map((el) => ({
          text: (el.textContent ?? '').replace(/\s+/g, ' ').trim().slice(0, 30),
          action: !!el.querySelector('.empty-hero__action'),
        }))
        /* antd 那句默认的"暂无数据"不算空态：它只说"这里没有东西"，
           不说为什么没有、下一步去哪。地图页真机就量到过它和"全部要素均已落到图上"叠在一起。 */
        const bare = [...document.querySelectorAll('.ant-empty-description')].filter(
          (el) => (el.textContent ?? '').trim() === '暂无数据',
        ).length
        /* 自己写了说明的那类空态（地图的"未选中要素"）也算空态，只是它不需要"下一步去哪"——
           它是选择提示，不是"数据缺席"。 */
        const custom = [...document.querySelectorAll('.ant-empty')].filter((el) => {
          const d = (el.querySelector('.ant-empty-description')?.textContent ?? '').trim()
          return d !== '' && d !== '暂无数据'
        }).length
        return { heroes, bare, custom }
      })
      console.log(
        `EMPTY ${name} leaves=${r.leafCount} overlap=${r.overlapCount} clipped=${r.clippedCount} folded=${r.foldedCount} lowContrast=${r.lowContrastCount} heroes=${empty.heroes.length} withAction=${empty.heroes.filter((h) => h.action).length} bare=${empty.bare} custom=${empty.custom}`,
      )
      await page.screenshot({ path: `test-results/ui-audit/empty-${name}.png` })
      expect(empty.heroes.length + empty.bare + empty.custom, `${name} 一个空态都没渲染出来——这项目判据就没了对象`).toBeGreaterThan(0)
      expect(empty.bare, `${name} 还有 ${empty.bare} 处 antd 默认"暂无数据"（不说为什么没有、也不说下一步去哪）`).toBe(0)
      if (empty.heroes.length > 0) {
        expect(empty.heroes.some((h) => h.action), `${name} 的空态没有"下一步去哪"那个出口`).toBe(true)
      }
      expect(r.overlapCount, `${name} 空态重叠：\n${r.overlap.join('\n')}`).toBe(0)
      expect(r.clippedCount, `${name} 空态文字被裁：\n${r.clipped.join('\n')}`).toBe(0)
      expect(r.foldedCount, `${name} 空态按钮折行：\n${r.folded.join('\n')}`).toBe(0)
      expect(r.lowContrastCount, `${name} 空态对比度不足：\n${r.lowContrast.join('\n')}`).toBe(0)
      expect(r.englishEmptyCount, `${name} 空态出现纯英文文案：\n${r.englishEmpty.join('\n')}`).toBe(0)
    })
  }
})

/**
 * 画布是体检的死角：`skip()` 整块跳过了 `.vue-flow`（里面有 canvas 与大量绝对定位），
 * 于是节点卡互相压叠、节点标题出框这些"真机上最扎眼"的问题没有任何判据。
 * 这里单独量两件事：①节点卡之间不重叠；②节点标题不被挤出自己的卡。
 * 打开的是一份有 7 个节点的已存定义（不是空画布），并处理"未保存改动"的确认弹窗。
 */
test.describe('画布节点不变量 @1440', () => {
  test('打开有节点的定义：节点卡不互相压叠、标题不出框', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.goto('/workflow')
    await page.waitForSelector('.page-hero', { timeout: 45_000 })
    await page.waitForLoadState('networkidle').catch(() => {})
    await page.waitForTimeout(2_000)
    /* 刚进页面时是"新建画布"那份空定义：① 画布上没有节点，小地图里什么都没有，不许把它画出来
       （但它得**挂在 DOM 里**——落点让位要量它的真实尺寸当禁落区，见下面那条拖拽用例）；
       ② 横幅里"保存定义"此时是禁用态——antd 的禁用样式是"浅底 + 25% 黑字"，落在藏青横幅上
       就是暗底暗字。全局对比度判据对 `.ant-btn[disabled]` 是豁免的（浅底上那是"点不动"的提示，
       不算读不到），横幅里不能豁免，所以单独量一次，并钉住"这里真的有禁用按钮"。 */
    await page.waitForTimeout(1_500)
    const empty = await page.evaluate(() => ({
      nodes: document.querySelectorAll('.wf-node').length,
      minimap: (() => {
        const el = document.querySelector('.vue-flow__minimap')
        if (!el) return false
        const s = getComputedStyle(el)
        return s.visibility !== 'hidden' && s.display !== 'none' && el.getBoundingClientRect().width > 0
      })(),
      minimapMounted: !!document.querySelector('.vue-flow__minimap'),
      disabled: document.querySelectorAll('.page-hero__actions button:disabled').length,
    }))
    const heroC = await page.evaluate(inspect, { contrastAllow: CONTRAST_ALLOW })
    console.log(`CANVAS[empty] nodes=${empty.nodes} minimap=${empty.minimap} mounted=${empty.minimapMounted} heroBtn=${heroC.heroBtnCount} heroDisabled=${empty.disabled} heroLow=${heroC.heroLowCount}`)
    expect(empty.nodes, '这条要量的是"空画布"，但画布上已经有节点了').toBe(0)
    expect(empty.minimap, '空画布上还画着一个小地图（里面什么都没有，只是块白框）').toBe(false)
    expect(empty.minimapMounted, '空画布上小地图整个不在 DOM 里——落点让位就拿不到它的尺寸，第一颗节点会被它盖住').toBe(true)
    expect(empty.disabled, '横幅里没有禁用态按钮——这条判据在空跑（"保存定义"此时该是禁用的）').toBeGreaterThan(0)
    expect(heroC.heroBtnCount, '横幅里一个按钮文本都没量到——判据在空跑').toBeGreaterThanOrEqual(2)
    expect(heroC.heroLowCount, `横幅里的按钮在深底上读不清：\n${heroC.heroLow.join('\n')}`).toBe(0)

    /* 右栏是 pill 切换、默认停在"运行态"：要看定义列表得先切档 */
    await page.locator('[data-testid="rail-def"]').click()
    await page.waitForTimeout(800)
    /* 挑"节点最多的那份定义"来量：第一行那份只有 2 个节点，压不出布局问题 */
    const idx = await page.evaluate(() => {
      const rows = [...document.querySelectorAll('.wf-def__list-item')]
      let best = 0
      let bestCount = -1
      rows.forEach((row, i) => {
        const m = /(\d+)\s*节点/.exec(row.textContent ?? '')
        const n = m ? Number(m[1]) : 0
        if (n > bestCount) {
          bestCount = n
          best = i
        }
      })
      return { best, bestCount }
    })
    expect(idx.bestCount, '服务端定义列表里没有带节点的定义——判据在空跑').toBeGreaterThan(1)
    await page.locator('[data-testid^="open-"]').nth(idx.best).click()
    const confirm = page.locator('.ant-modal-confirm .ant-btn-primary')
    if (await confirm.isVisible().catch(() => false)) await confirm.click()
    await page.waitForSelector('.wf-node', { timeout: 20_000 })
    await page.waitForTimeout(2_500)
    const measure = () => page.evaluate(() => {
      const nodes = [...document.querySelectorAll('.wf-node')]
      const rect = (el: Element) => el.getBoundingClientRect()
      const overlap: string[] = []
      for (let i = 0; i < nodes.length; i++) {
        for (let j = i + 1; j < nodes.length; j++) {
          const a = rect(nodes[i])
          const b = rect(nodes[j])
          const ox = Math.min(a.right, b.right) - Math.max(a.left, b.left)
          const oy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top)
          if (ox > 4 && oy > 4) {
            overlap.push(`${(nodes[i].querySelector('.wf-node__title')?.textContent ?? '').trim()} × ${(nodes[j].querySelector('.wf-node__title')?.textContent ?? '').trim()} 压叠 ${Math.round(ox)}×${Math.round(oy)}`)
          }
        }
      }
      const spill: string[] = []
      for (const n of nodes) {
        const title = n.querySelector('.wf-node__title')
        if (!title) continue
        const tr = rect(title)
        const nr = rect(n)
        /* 只判"画到框外"。scrollWidth > clientWidth 不算缺陷——那是省略号在起作用，
           第一版把这条也判成出框，等于把修好的东西报成 bug */
        if (tr.right > nr.right + 1 || tr.left < nr.left - 1 || tr.bottom > nr.bottom + 1) {
          spill.push(`"${(title.textContent ?? '').trim().slice(0, 18)}" 画到框外（右缘差=${Math.round(tr.right - nr.right)} 左缘差=${Math.round(tr.left - nr.left)}）`)
        }
      }
      const zoom = Number(document.querySelector('.vue-flow__viewport')?.getAttribute('style')?.match(/zoom\(([\d.]+)\)/)?.[1] ?? 1)
      /* 画布浮层（小地图 / 缩放控件）此前是判据死角：@vue-flow 默认只给 minimap 一个
         `background-color:#fff`，落进我们的圆角玻璃画布就是一块生硬白方块（真机截图里那个
         "空白矩形"）。判据：要么不出现，要么就得是卡片族的样子（圆角 + 描边或投影）且不出画布。 */
      const canvasRect = document.querySelector('.wf__canvas')?.getBoundingClientRect()
      const panel = (sel: string) => {
        const el = document.querySelector(sel) as HTMLElement | null
        if (!el) return null
        const cs = getComputedStyle(el)
        const r = el.getBoundingClientRect()
        return {
          radius: parseFloat(cs.borderTopLeftRadius) || 0,
          border: parseFloat(cs.borderTopWidth) || 0,
          shadow: cs.boxShadow !== 'none',
          w: Math.round(r.width),
          h: Math.round(r.height),
          inside:
            !!canvasRect &&
            r.left >= canvasRect.left - 1 &&
            r.top >= canvasRect.top - 1 &&
            r.right <= canvasRect.right + 1 &&
            r.bottom <= canvasRect.bottom + 1,
        }
      }
      return {
        count: nodes.length,
        zoom,
        overlap,
        spill,
        minimap: panel('.vue-flow__minimap'),
        controls: panel('.vue-flow__controls'),
      }
    })

    const check = async (phase: string) => {
      const r = await measure()
      console.log(`CANVAS[${phase}] nodes=${r.count} overlap=${r.overlap.length} spill=${r.spill.length} minimap=${JSON.stringify(r.minimap)} controls=${JSON.stringify(r.controls)}`)
      await page.screenshot({ path: `test-results/ui-audit/canvas-${phase}.png` })
      expect(r.count, `${phase}：画布上没有节点——判据在空跑`).toBeGreaterThan(1)
      expect(r.overlap, `${phase} 节点卡互相压叠：\n${r.overlap.join('\n')}`).toHaveLength(0)
      expect(r.spill, `${phase} 节点标题出框：\n${r.spill.join('\n')}`).toHaveLength(0)
      for (const [which, p] of [
        ['小地图', r.minimap],
        ['缩放控件', r.controls],
      ] as const) {
        expect(p, `${phase}：画布上有节点时${which}没渲染`).not.toBeNull()
        if (!p) continue
        expect(p.inside, `${phase}：${which}跑到画布外`).toBe(true)
        expect(p.w, `${phase}：${which}宽 0`).toBeGreaterThan(0)
        expect(p.h, `${phase}：${which}高 0`).toBeGreaterThan(0)
        /* 圆角 + 描边/投影是"卡片族"的最低门槛：库里默认那版是 0 圆角 + 无边框的裸白块 */
        expect(p.radius, `${phase}：${which}是直角裸块（圆角=${p.radius}）`).toBeGreaterThanOrEqual(8)
        expect(p.border > 0 || p.shadow, `${phase}：${which}没有描边也没有投影，和画布糊成一片`).toBe(true)
      }
    }

    await check('opened')
    await page.getByRole('button', { name: '自动布局' }).click()
    await page.waitForTimeout(1_800)
    await check('auto-layout')
  })

  /**
   * 拖拽落点：第一颗贴着画布右下角落，第二颗几乎压在它上面，第三颗落在别处。
   * 真机量到四类"看不见"，各自变异验过一条判据：
   * - 压叠：第二张压住第一张 **389×234px**（落点原样取光标位置，而节点卡有 208px 宽）。
   * - 出画布：只躲压叠不躲可视区时落到 y=704..957（画布下沿 880）、x 到 1128（画布右 1068）。
   * - 被浮层盖住：收进框内后又压在小地图那块不透明卡片下（实测 171×102px）。
   * - **空画布上的第一颗**：那一刻小地图还没挂载 ⇒ 禁落区为空，节点落好后立刻被刚挂载的
   *   小地图盖住（实测 171×114px）——所以小地图改成"空画布不画但仍在 DOM 里"。
   * 根因还有 init-fit：空画布时它不跑，第一颗节点落下才 fit 且不带参数，zoom 被顶到 maxZoom=2，
   * 可视区只剩 414×339 flow 单位（不到两个节点宽）——所以先钉 zoom，再钉症状。
   */
  test('连拖三颗节点：不压叠、不出画布、不被浮层盖住、画布不被 fit 放大', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.goto('/workflow')
    await page.waitForSelector('.page-hero', { timeout: 45_000 })
    await page.waitForLoadState('networkidle').catch(() => {})
    await page.waitForTimeout(3_000)
    const items = page.locator('.wf-palette__item')
    expect(await items.count(), '节点面板一个都没有——判据在空跑').toBeGreaterThan(3)
    const canvas = page.locator('.wf__canvas')
    const frame = (await canvas.boundingBox()) as { width: number; height: number }
    /* 第一颗就贴着右下角落：那一刻小地图还是"空画布不画"的状态，
       禁落区必须量得到它的尺寸，否则节点落好之后立刻被刚挂载的小地图盖住。 */
    await items.nth(0).dragTo(canvas, { targetPosition: { x: frame.width - 40, y: frame.height - 20 } })
    await page.waitForTimeout(700)
    await items.nth(1).dragTo(canvas, { targetPosition: { x: frame.width - 28, y: frame.height - 6 } })
    await page.waitForTimeout(700)
    await items.nth(2).dragTo(canvas, { targetPosition: { x: 220, y: 200 } })
    await page.waitForTimeout(900)
    const m = await page.evaluate(() => {
      const nodes = [...document.querySelectorAll('.wf-node')]
      const rect = (el: Element) => el.getBoundingClientRect()
      const frame = document.querySelector('.wf__canvas')?.getBoundingClientRect()
      const overlap: string[] = []
      for (let i = 0; i < nodes.length; i += 1) {
        for (let j = i + 1; j < nodes.length; j += 1) {
          const a = rect(nodes[i])
          const b = rect(nodes[j])
          const ox = Math.min(a.right, b.right) - Math.max(a.left, b.left)
          const oy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top)
          if (ox > 4 && oy > 4) {
            overlap.push(`${(nodes[i].querySelector('.wf-node__title')?.textContent ?? '').trim()} × ${(nodes[j].querySelector('.wf-node__title')?.textContent ?? '').trim()} 压 ${Math.round(ox)}×${Math.round(oy)}px`)
          }
        }
      }
      const outside = nodes
        .map((n, i) => {
          const r = rect(n)
          if (!frame) return ''
          const bad = r.left < frame.left - 1 || r.top < frame.top - 1 || r.right > frame.right + 1 || r.bottom > frame.bottom + 1
          return bad ? `第 ${i + 1} 张出画布（${Math.round(r.left)},${Math.round(r.top)},${Math.round(r.right)},${Math.round(r.bottom)} vs 画布 ${Math.round(frame.left)},${Math.round(frame.top)},${Math.round(frame.right)},${Math.round(frame.bottom)}）` : ''
        })
        .filter((s) => s !== '')
      const titles = nodes.map((n) => (n.querySelector('.wf-node__title')?.textContent ?? '').trim())
      /* 小地图/缩放控件是画布上的不透明浮层：节点被它们盖住＝看不见，和出画布同一类缺陷。 */
      const overlays = [...document.querySelectorAll('.vue-flow__minimap, .vue-flow__controls')].map(rect)
      const covered: string[] = []
      for (const n of nodes) {
        const r = rect(n)
        for (const o of overlays) {
          const ox = Math.min(r.right, o.right) - Math.max(r.left, o.left)
          const oy = Math.min(r.bottom, o.bottom) - Math.max(r.top, o.top)
          if (ox > 4 && oy > 4) {
            covered.push(`${(n.querySelector('.wf-node__title')?.textContent ?? '').trim()} 被浮层盖住 ${Math.round(ox)}×${Math.round(oy)}px`)
          }
        }
      }
      const pane = document.querySelector('.vue-flow__transformationpane')
      const t = pane ? getComputedStyle(pane).transform : ''
      const zoom = t.startsWith('matrix') ? Number(t.slice(t.indexOf('(') + 1).split(',')[0]) : Number.NaN
      return { count: nodes.length, overlap, outside, covered, titles, zoom }
    })
    console.log(`DROPCOLLIDE nodes=${m.count} titles=${JSON.stringify(m.titles)} overlap=${m.overlap.length} outside=${m.outside.length} covered=${JSON.stringify(m.covered)} zoom=${m.zoom}`)
    await page.screenshot({ path: 'test-results/ui-audit/canvas-drop.png' })
    expect(m.count, '三次拖拽后画布上应当有三张节点卡（拖拽没生效＝判据在空跑）').toBe(3)
    /* 先钉 zoom：init-fit 一旦被顶到 maxZoom，可视区就只有两个节点宽，
       后面"出画布/被盖住"都是它的下游症状——报根因比报症状有用。 */
    expect(m.zoom, `拖完三张之后画布 zoom=${m.zoom}，fit 把画布放大了`).toBeLessThanOrEqual(1.001)
    expect(m.overlap, `拖拽落点压叠：\n${m.overlap.join('\n')}`).toHaveLength(0)
    expect(m.outside, `让位把节点推出了画布：\n${m.outside.join('\n')}`).toHaveLength(0)
    expect(m.covered, `落点被画布浮层盖住：\n${m.covered.join('\n')}`).toHaveLength(0)
  })
})

/**
 * 提示条（antd message）落点：库里默认 top:8px，而顶栏定高 68px——报错提示正好糊在顶栏上。
 * 真机截图实测过一条 500 提示把步进器第 7 档和右侧状态胶囊整个盖住，而那两处是
 * "我在哪一步 / 后端通不通"的唯一入口。重叠判据看不见它（`.ant-message` 在 skip 名单里，
 * 弹层本来就该浮在正文上），所以单独钉一条"不许盖住顶栏"。
 */
test.describe('提示条落点 @1440', () => {
  test('报错提示落在顶栏下方，不吃掉步进器与状态胶囊', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.route('**/api/**', (route) =>
      route.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"probe boom"}' }),
    )
    await page.goto('/workflow')
    await page.waitForSelector('.page-hero', { timeout: 45_000 })
    await page.waitForTimeout(1_500)
    await page.getByRole('button', { name: '刷新实例' }).click()
    await page.waitForSelector('.ant-message-notice', { timeout: 15_000 })
    const m = await page.evaluate(() => {
      const box = document.querySelector('.ant-message')?.getBoundingClientRect()
      const nav = document.querySelector('.navbar')?.getBoundingClientRect()
      const notice = document.querySelector('.ant-message-notice')?.getBoundingClientRect()
      return {
        top: box ? Math.round(box.top) : null,
        noticeTop: notice ? Math.round(notice.top) : null,
        noticeText: (document.querySelector('.ant-message-notice')?.textContent ?? '').trim().slice(0, 40),
        navBottom: nav ? Math.round(nav.bottom) : null,
        navH: nav ? Math.round(nav.height) : null,
      }
    })
    console.log(`TOAST top=${m.top} noticeTop=${m.noticeTop} navBottom=${m.navBottom} text="${m.noticeText}"`)
    expect(m.noticeText, '提示条没出现——判据在空跑').not.toBe('')
    expect(m.noticeTop, '顶栏高度没量到').not.toBeNull()
    if (m.noticeTop !== null && m.navBottom !== null) {
      expect(m.noticeTop, `提示条压在顶栏上（提示顶边=${m.noticeTop}，顶栏底边=${m.navBottom}）`).toBeGreaterThanOrEqual(m.navBottom)
    }
  })
})

/**
 * 横幅状态位（PageHero 的 `status`）：那句"现在怎么样了"是深色横幅最贵的一行字，
 * 圆点必须真的按 tone 变色（不是所有档都画同一个灰点），文字必须读得清。
 * 目前只有编排页与预警页给了 status——断言按页来，别拿"每页都得有"当判据。
 */
test.describe('横幅状态位 @1440', () => {
  for (const [name, path] of [
    ['workflow', '/workflow'],
    ['warnings', '/warnings'],
  ] as const) {
    test(`${name} 的状态语有彩色圆点且字读得清`, async ({ page }) => {
      await page.setViewportSize({ width: 1440, height: 900 })
      await page.goto(path)
      await page.waitForSelector('.page-hero', { timeout: 45_000 })
      await page.waitForTimeout(3_000)
      const m = await page.evaluate(() => {
        const el = document.querySelector('.page-hero__status')
        const dot = el?.querySelector('.page-hero__status-dot') as HTMLElement | null
        const cs = el ? getComputedStyle(el) : null
        const r = dot?.getBoundingClientRect()
        return {
          text: (el?.textContent ?? '').trim(),
          cls: el ? [...el.classList].filter((c) => c.startsWith('is-')).join(',') : '',
          dotBg: dot ? getComputedStyle(dot).backgroundColor : '',
          dotW: r ? Math.round(r.width) : 0,
          dotH: r ? Math.round(r.height) : 0,
          fontSize: cs ? parseFloat(cs.fontSize) : 0,
        }
      })
      console.log(`HEROSTATUS ${name} cls=${m.cls} dot=${m.dotBg} ${m.dotW}x${m.dotH} text="${m.text}"`)
      expect(m.text, `${name}：横幅没有状态语——这条判据在空跑`).not.toBe('')
      expect(m.cls, `${name}：状态语没带 tone 类（is-ok/is-warn/is-bad/is-idle）`).not.toBe('')
      expect(m.dotW, `${name}：圆点宽 0`).toBeGreaterThanOrEqual(6)
      expect(m.dotH, `${name}：圆点高 0`).toBeGreaterThanOrEqual(6)
      /* tone 类要真的落到颜色上：除 idle 外，圆点不许还是那个 50% 白的默认底 */
      if (!m.cls.includes('is-idle')) {
        expect(m.dotBg, `${name}：${m.cls} 的圆点还是默认灰（tone 类没起作用）`).not.toBe('rgba(255, 255, 255, 0.5)')
        expect(m.dotBg).not.toBe('rgb(255, 255, 255)')
      }
      expect(m.fontSize, `${name}：状态语字号`).toBeGreaterThanOrEqual(12)
    })
  }
})

/**
 * 新鲜度条（FreshnessBar）：左端口径、右端"取数 HH:MM:SS ｜ N 秒前"，中间一道线。
 * 判据盯三件：两端都得有字（少一端就是这条判据在空跑）、两端不许压在一起、
 * 右端必须真的报出"多久之前"——只写绝对时间戳的话，页面挂两小时也看不出来。
 */
test.describe('新鲜度条 @1440', () => {
  for (const [name, path] of [
    ['warnings', '/warnings'],
    ['monitor', '/monitor'],
    ['metrics', '/metrics'],
    ['workflow', '/workflow'],
  ] as const) {
    test(`${name} 说得出"几点取的"与"多久之前"，两端不重叠`, async ({ page }) => {
      await page.setViewportSize({ width: 1440, height: 900 })
      await page.goto(path)
      await page.waitForSelector('[data-testid="freshness"]', { timeout: 45_000 })
      await page.waitForTimeout(3_000)
      const m = await page.evaluate(() => {
        const bar = document.querySelector('[data-testid="freshness"]')
        const left = bar?.querySelector('.fresh__left')?.getBoundingClientRect()
        const right = bar?.querySelector('.fresh__right')?.getBoundingClientRect()
        /* 两端各自有没有被省略号截掉：编排页右栏 ~316px，横排时"每 15 秒…"会被切成"每 1…" */
        const cut = ['.fresh__left', '.fresh__right']
          .map((sel) => {
            const el = bar?.querySelector(sel) as HTMLElement | null
            return el && el.scrollWidth > el.clientWidth + 1 ? `${sel} 需要 ${el.scrollWidth}px 只有 ${el.clientWidth}px` : ''
          })
          .filter((s) => s !== '')
        return {
          left: (bar?.querySelector('.fresh__left')?.textContent ?? '').trim(),
          right: (bar?.querySelector('.fresh__right')?.textContent ?? '').trim(),
          cut,
          stacked: bar ? bar.classList.contains('is-stacked') : false,
          gap: left && right ? Math.round(right.left - left.right) : null,
          ruleH: Math.round(bar?.querySelector('.fresh__rule')?.getBoundingClientRect().height ?? -1),
        }
      })
      console.log(`FRESH ${name} left="${m.left}" right="${m.right}" gap=${m.gap} ruleH=${m.ruleH} stacked=${m.stacked} cut=${JSON.stringify(m.cut)}`)
      await page.screenshot({ path: `test-results/ui-audit/freshness-${name}.png`, clip: { x: 0, y: 190, width: 1440, height: 140 } })
      expect(m.left, `${name}：左端口径是空的`).not.toBe('')
      expect(m.right, `${name}：右端没报新鲜度——判据在空跑`).not.toBe('')
      expect(m.right, `${name}：右端要同时报"取数时刻"和"多久之前"，实测：${m.right}`).toMatch(/取数 .*｜ .*(秒前|分前|比预期周期慢)|尚未取到数据/)
      expect(m.cut, `${name}：有一端被省略号截掉了（这条说的就是"数据多新"，截掉就等于没说）：\n${m.cut.join('\n')}`).toHaveLength(0)
      if (m.stacked) {
        expect(m.gap, `${name}：竖排时两端不该并排`).toBeLessThan(0)
      } else if (m.gap !== null) {
        expect(m.gap, `${name}：两端重叠（间隙=${m.gap}px）`).toBeGreaterThan(0)
      }
      expect(m.ruleH, `${name}：中间那道线没画出来`).toBeGreaterThanOrEqual(1)
    })
  }
})

/**
 * 竖向运行时间线（RunTimeline）：24px 的轴 + knockout 圆点 + 未开始一档用 dashed。
 * 这条判据盯的是"轴不能塌、点与线要在同一竖轴上、dashed 档不许被画成实心"——
 * 三样都是肉眼容易滑过去、截图又只在窄栏里看得出问题的东西。
 */
test.describe('运行时间线 @1440', () => {
  test('选中实例后：轴宽 24、点线同轴、dashed 档保持虚线', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.goto('/workflow')
    await page.waitForSelector('.page-hero', { timeout: 45_000 })
    await page.waitForLoadState('networkidle').catch(() => {})
    await page.waitForTimeout(2_500)
    const picks = page.locator('.wf-run__pick')
    expect(await picks.count(), '运行态里没有可选实例——判据在空跑').toBeGreaterThan(0)
    /* 第一段用真数据：几何（轴宽、点线同轴、最后一行不画线）与"名字回定义里取"。 */
    await picks.first().click()
    await page.waitForSelector('[data-testid="run-timeline"] .wf-tl__step', { timeout: 20_000 })
    await page.waitForTimeout(900)
    const real = await page.evaluate(() => {
      const rows = [...document.querySelectorAll('.wf-tl__step')]
      return {
        rows: rows.length,
        axisW: rows.map((r) => Math.round((r.querySelector('.wf-tl__axis') as HTMLElement).getBoundingClientRect().width)),
        lines: document.querySelectorAll('.wf-tl__line').length,
        named: [...document.querySelectorAll('.wf-tl__name')].map((el) => ({
          name: (el.textContent ?? '').trim(),
          id: el.getAttribute('title') ?? '',
        })),
      }
    })
    console.log(`TIMELINE-real rows=${real.rows} axis=${JSON.stringify(real.axisW)} lines=${real.lines} named=${JSON.stringify(real.named.slice(0, 2))}`)
    expect(real.rows, '时间线一行都没有').toBeGreaterThan(0)
    expect(new Set(real.axisW).size, `轴宽不统一：${JSON.stringify(real.axisW)}`).toBe(1)
    expect(real.axisW[0], '轴列宽必须是 24px（连接线要靠它定位）').toBe(24)
    expect(real.lines, '最后一行不该再画一截悬空的轴').toBe(real.rows - 1)
    expect(real.named.some((row) => row.name !== '' && row.name !== row.id), '节点名一律退回 ID——没回定义里取名字').toBe(true)
    /* 第二段灌一份实例详情：本机种子数据里的实例都跑完了（真机试过，前六条一条"未开始"的行都没有），
       那样 dashed 那一档根本没被验到。五种状态各一行，四档底色与错误行都能确定性地量。
       走 route 而不是真启动一条实例——启动会往台账里落真数据（探针消耗现场这条坑记在台账里）。 */
    const node = (id: string, state: string, extra: Record<string, unknown> = {}) => ({
      node_id: id,
      type: 'api_call',
      state,
      attempts: 1,
      schedule_latency_ms: null,
      duration_ms: null,
      output: {},
      error: null,
      notes: [],
      ...extra,
    })
    await page.route(/\/api\/v1\/workflow\/instances\/[^/]+$/, (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          instance_id: 'wfi_probe_timeline',
          workflow_id: 'wf_probe',
          workflow_version: 1,
          trace_id: 'trc_probe',
          status: 'running',
          error: null,
          nodes: [
            node('gate', 'succeeded', { duration_ms: 1_250 }),
            node('notify_ok', 'failed', { error: '上游 502：网关无响应', attempts: 3 }),
            node('review', 'awaiting_human'),
            node('archive', 'pending'),
            node('report', 'ready'),
          ],
        }),
      }),
    )
    await picks.first().click()
    await page.waitForSelector('[data-testid="run-timeline"] .wf-tl__step', { timeout: 20_000 })
    const sawTodo = await page.locator('.wf-tl__step.is-todo').count()
    await page.waitForTimeout(900)
    const m = await page.evaluate(() => {
      const rows = [...document.querySelectorAll('.wf-tl__step')]
      const axisW = rows.map((r) => Math.round((r.querySelector('.wf-tl__axis') as HTMLElement).getBoundingClientRect().width))
      const offAxis: string[] = []
      for (const r of rows) {
        const dot = r.querySelector('.wf-tl__dot')?.getBoundingClientRect()
        const line = r.querySelector('.wf-tl__line')?.getBoundingClientRect()
        if (!dot || !line) continue
        const dotCx = dot.left + dot.width / 2
        const lineCx = line.left + line.width / 2
        if (Math.abs(dotCx - lineCx) > 1) offAxis.push(`第 ${rows.indexOf(r) + 1} 行点与线偏心 ${Math.round(dotCx - lineCx)}px`)
        if (dot.width < 8) offAxis.push(`第 ${rows.indexOf(r) + 1} 行圆点只有 ${Math.round(dot.width)}px`)
      }
      const dashed: string[] = []
      for (const r of rows) {
        const cs = getComputedStyle(r)
        const todo = r.classList.contains('is-todo')
        if (todo && cs.borderTopStyle !== 'dashed') dashed.push(`第 ${rows.indexOf(r) + 1} 行是待办档却画成 ${cs.borderTopStyle}`)
        if (!todo && cs.borderTopStyle === 'dashed') dashed.push(`第 ${rows.indexOf(r) + 1} 行不是待办档却用了虚线`)
      }
      return {
        rows: rows.length,
        axisW,
        offAxis,
        dashed,
        tones: rows.map((r) => [...r.classList].find((c) => c.startsWith('is-')) ?? 'none'),
        errLines: document.querySelectorAll('.wf-tl__err').length,
        metaLines: document.querySelectorAll('.wf-tl__meta').length,
        lines: document.querySelectorAll('.wf-tl__line').length,
        named: [...document.querySelectorAll('.wf-tl__step')].map((r) => {
          const el = r.querySelector('.wf-tl__name') as HTMLElement | null
          return { name: (el?.textContent ?? '').trim(), id: el?.getAttribute('title') ?? '' }
        }),
      }
    })
    console.log(`TIMELINE rows=${m.rows} axis=${JSON.stringify(m.axisW)} lines=${m.lines} named=${JSON.stringify(m.named.slice(0, 2))} offAxis=${m.offAxis.length} dashed=${m.dashed.length} todoRows=${sawTodo}`)
    await page.screenshot({ path: 'test-results/ui-audit/run-timeline.png', clip: { x: 1040, y: 190, width: 400, height: 700 } })
    expect(m.rows, '时间线一行都没有').toBeGreaterThan(0)
    expect(new Set(m.axisW).size, `轴宽不统一：${JSON.stringify(m.axisW)}`).toBe(1)
    expect(m.axisW[0], '轴列宽必须是 24px（连接线要靠它定位）').toBe(24)
    expect(m.offAxis, m.offAxis.join('\n')).toHaveLength(0)
    expect(m.dashed, m.dashed.join('\n')).toHaveLength(0)
    expect(m.lines, '最后一行不该再画一截悬空的轴').toBe(m.rows - 1)
    /* 空判防护：这一段灌的是"还有一步没走"的详情，dashed 那一档必须真被验到 */
    expect(sawTodo, '前六条实例都没有"未开始"的一行——dashed 那档没被验到').toBeGreaterThan(0)
    /* 灌进去的五种状态要各归各的档：succeeded→done、failed→bad、awaiting_human→active、pending/ready→todo */
    expect(m.tones, `五行的底色档不对：${JSON.stringify(m.tones)}`).toEqual(['is-done', 'is-bad', 'is-active', 'is-todo', 'is-todo'])
    expect(m.errLines, '失败那一行的原因没渲染出来').toBe(1)
    expect(m.metaLines, '耗时/重试那行小字没渲染出来').toBe(2)
  })
})

/**
 * 系统输出浮层（SystemTerminal）：收起态是一枚芯片，展开态是一块深色面板。
 * 第一版按 NexusMind 那样 `position:fixed` 挂在右下角，门禁当场量出它压住分页控件
 * （"10 条/页" × "系统输出" 418px²，六档全中）——改成走文档流之后这条判据守着不再回退：
 * 展开的面板不许与分页相交、深色底上的每一行字都要过对比度、芯片本身要够点。
 */
test.describe('系统输出浮层 @1440', () => {
  test('展开后：面板在视口内、不压分页、深底文字读得清', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.goto('/monitor')
    await page.waitForSelector('.page-hero', { timeout: 45_000 })
    await page.waitForLoadState('networkidle').catch(() => {})
    await page.waitForTimeout(2_500)
    const chip = await page.evaluate(() => {
      const el = document.querySelector('.sys-term__chip')?.getBoundingClientRect()
      return { h: el ? Math.round(el.height) : 0, w: el ? Math.round(el.width) : 0 }
    })
    console.log(`SYSTERM chip=${chip.w}×${chip.h}`)
    expect(chip.h, '收起态的芯片太矮，点不准').toBeGreaterThanOrEqual(28)
    await page.locator('[data-testid="sys-term-toggle"]').click()
    await page.waitForSelector('.sys-term__body', { timeout: 15_000 })
    await page.waitForTimeout(800)
    /* 它是走文档流的最后一块，不在首屏里是正常的——先滚到它，再判"能不能整个看到"
       （判的是没被某个 overflow:hidden 的祖先裁掉，不是"必须浮在屏幕上"）。 */
    await page
      .locator('.sys-term__body')
      .evaluate((el) => el.scrollIntoView({ block: 'end', inline: 'nearest' }))
      .catch(() => {})
    await page.waitForTimeout(400)
    const geo = await page.evaluate(() => {
      const b = document.querySelector('.sys-term__body')?.getBoundingClientRect()
      const pag = document.querySelector('.ant-pagination')?.getBoundingClientRect()
      const rows = document.querySelectorAll('.sys-term__row').length
      const empty = document.querySelector('.sys-term__empty') ? 1 : 0
      return {
        h: b ? Math.round(b.height) : -1,
        bottom: b ? Math.round(b.bottom) : -1,
        right: b ? Math.round(b.right) : -1,
        iw: window.innerWidth,
        top: b ? Math.round(b.top) : -1,
        ih: window.innerHeight,
        scrollY: Math.round(window.scrollY),
        bodyH: document.body.scrollHeight,
        inView: !!b && b.bottom <= window.innerHeight + 1 && b.right <= window.innerWidth + 1,
        hitsPager: !!b && !!pag && Math.min(b.right, pag.right) - Math.max(b.left, pag.left) > 4 && Math.min(b.bottom, pag.bottom) - Math.max(b.top, pag.top) > 4,
        rows,
        empty,
      }
    })
    const r = await page.evaluate(inspect, { contrastAllow: CONTRAST_ALLOW, scope: '.sys-term' })
    console.log(`SYSTERM-open h=${geo.h} top=${geo.top} bottom=${geo.bottom} ih=${geo.ih} right=${geo.right} iw=${geo.iw} scrollY=${geo.scrollY} bodyH=${geo.bodyH} inView=${geo.inView} hitsPager=${geo.hitsPager} rows=${geo.rows} empty=${geo.empty} leaves=${r.leafCount} overlap=${r.overlapCount} low=${r.lowContrastCount}`)
    await page.screenshot({ path: 'test-results/ui-audit/sys-term-open.png', clip: { x: 640, y: 500, width: 800, height: 400 } })
    expect(geo.h, '展开态没高度').toBeGreaterThan(60)
    expect(geo.inView, '面板跑出视口了').toBe(true)
    expect(geo.hitsPager, '面板压住分页控件了（第一版 fixed 就是这么翻车的）').toBe(false)
    expect(geo.rows + geo.empty, '面板里既没有日志行也没有空态说明').toBeGreaterThan(0)
    expect(r.leafCount, '面板里一个文本节点都没量到——判据在空跑').toBeGreaterThan(2)
    expect(r.overlapCount, `面板内部重叠：\n${r.overlap.join('\n')}`).toBe(0)
    expect(r.lowContrastCount, `深色面板上的字读不清：\n${r.lowContrast.join('\n')}`).toBe(0)
  })
})

/**
 * 键盘焦点态：第四批补的 `:focus-visible` 焦点环只有样式、没有证据。
 * 这里按 Tab 走一遍，要求每落一次焦点：①焦点元素确实有可见指示
 * （box-shadow 或 outline，不是靠颜色）；②它没被滚动容器裁到看不见。
 *
 * 变异量出来的口径：把我们的 `body :focus-visible` 规则改名后，"有可见指示"这条**不会红**——
 * antd 自己给按钮/输入框也画了焦点框。所以这条判据守的是"用户看得见焦点"这件事，
 * 不是"我们的规则生效"；后者另用 brandRing（至少一处焦点环用主色蓝）钉住。
 */
test.describe('键盘焦点态 @1440', () => {
  test('Tab 走七页：每个焦点都有可见指示且不被裁到看不见', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    const problems: string[] = []
    let brandRings = 0
    for (const [, path] of PAGES) {
      await page.goto(path)
      await page.waitForSelector('.page-hero', { timeout: 45_000 })
      await page.waitForTimeout(2_000)
      for (let i = 0; i < 10; i++) {
        await page.keyboard.press('Tab')
        const info = await page.evaluate(() => {
          const el = document.activeElement as HTMLElement | null
          if (!el || el === document.body) return null
          const cs = getComputedStyle(el)
          const r = el.getBoundingClientRect()
          return {
            tag: el.tagName.toLowerCase(),
            cls: (typeof el.className === 'string' ? el.className : '').trim().split(/\s+/).slice(0, 2).join('.'),
            text: (el.textContent ?? '').trim().slice(0, 14),
            ring: cs.boxShadow !== 'none' || parseFloat(cs.outlineWidth) > 0,
            brand: cs.boxShadow.includes('37, 99, 235'),
            inView: r.top >= -1 && r.left >= -1 && r.bottom <= innerHeight + 1 && r.right <= innerWidth + 1,
            zero: r.width < 2 || r.height < 2,
          }
        })
        if (!info || info.zero) continue
        if (info.brand) brandRings++
        if (!info.ring) problems.push(`${path} 第${i + 1}次 Tab：<${info.tag}.${info.cls}> "${info.text}" 没有可见焦点指示`)
        if (!info.inView) problems.push(`${path} 第${i + 1}次 Tab：<${info.tag}.${info.cls}> "${info.text}" 焦点落在视口外`)
      }
    }
    console.log(`FOCUS problems=${problems.length} brandRings=${brandRings}`)
    expect(problems, `键盘焦点态问题：\n${problems.slice(0, 12).join('\n')}`).toHaveLength(0)
    expect(brandRings, '70 次 Tab 里一处主色焦点环都没有——`:focus-visible` 那条规则可能整块失效了').toBeGreaterThan(0)
  })
})
