/**
 * UI 不变量门禁（对标 NexusMind 第三批，2026-10-05 立）。
 *
 * 为什么要有这一份：真机体检量出过四类肉眼会漏、但一改样式就会复发的缺陷——
 * ① 顶栏三组文字在 ≤1280 互相压字（绝对居中的步进器盖住副标题与状态胶囊）；
 * ② 状态胶囊被网格列裁掉半颗（box 还在、字看不见，重叠检查看不出来，所以另加 clip 检查）；
 * ③ 落地页装配事实条那条长串（dataset=内置预案模板…）一路顶出卡片、压到右邻那一行；
 * ④ 灰字对比度不到 4.5:1（步进器未选态 3.17、页脚 3.05、绿色标签 3.37）。
 * 判据全部来自包围盒与计算样式；底色会解析渐变（旧横幅那几支渐变已退役，解析器留着是为了回退时立刻红，
 * 只读 backgroundColor 会得到 transparent，白字就被误判成"白底白字"）。
 *
 * 跑法（与地图视觉门禁同一档：构建产物 + 本机 chromium）：
 *   npx playwright test e2e/ui-overlap.spec.ts
 */
import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

/**
 * "让数据落地再量"的辅助等待。**必须带 timeout**：`networkidle` 在健康后端上永远等不到——
 * 顶栏的 EventSource 是一条常驻连接，Playwright 的 networkidle 判据是"500ms 内没有网络活动"。
 * 原先 17 处直等（无超时）→ 每项跑到 180s 测试超时才红，而且**在 main 上同样复现**
 * （2026-10-17 实测：未改动的 /monitor 六档全超时）。真正的落地判据是各自的
 * `waitForSelector` 与 `leafCount` 下限，这里只是给一次机会。
 */
const settle = async (page: Page): Promise<void> => {
  await page.waitForLoadState('networkidle', { timeout: 8_000 }).catch(() => {})
}

/**
 * 页面就绪信号：普通页是 `.page-head`，`/workflow` 从 2026-10-23 起是**沉浸式**——
 * 应用侧栏/顶栏/页脚整块不渲染（`App.vue:immersive`），页面自己的 56px 编辑器头部才是外壳。
 * 用一支联合选择器，让所有"等页面起来"的既有判据在两套外壳下都成立。
 */
const READY = '.page-head, [data-testid="editor-header"]'
const immersivePath = (path: string): boolean => path === '/workflow'

/**
 * 全部可达页面（2026-10-17 信息架构收并后）：侧栏只挂 5 个一级入口
 * （值班主线 4 + 系统 1），但旧入口 /warnings /simulation /assistant 仍可访问
 * （书签、门禁、合并完成前的过渡），几何与对比度判据对它们一样生效。
 */
const PAGES: Array<[string, string]> = [
  ['dashboard', '/dashboard'],
  ['monitor', '/monitor'],
  ['warnings', '/warnings'],
  ['map', '/map'],
  ['workflow', '/workflow'],
  ['metrics', '/metrics'],
  ['simulation', '/simulation'],
  ['assistant', '/assistant'],
]
const WIDTHS = [1440, 1280, 1200, 1100, 1000, 900]

/** 对比度豁免：这些位置的字按 antd/交互语义本就该是"非正文"色，不是内容读不到。 */
const CONTRAST_ALLOW = [
  /* 禁用态为什么豁免：浅底上灰字＝"点不动"的提示（WCAG 1.4.11 也把"非活动组件"排除在外）。
     页头退役成亮面之后，动作区里的禁用钮回到"浅底灰字=点不动"这一档，跟全站同一口径。 */
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
      const cls = (typeof cur.className === 'string' ? cur.className : '')
        .trim()
        .split(/\s+/)
        .filter(Boolean)
        .slice(0, 2)
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
        overlap.push(
          `"${(a.el.textContent ?? '').trim().slice(0, 14)}" × "${(b.el.textContent ?? '').trim().slice(0, 14)}" area=${Math.round(ox * oy)} | ${pathOf(a.el)} || ${pathOf(b.el)}`,
        )
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
        if (/auto|scroll/.test(getComputedStyle(anc).overflowX)) {
          scrollable = true
          break
        }
        anc = anc.parentElement
      }
      if (!scrollable)
        beyond.push(
          `"${(el.textContent ?? '').trim().slice(0, 20)}" ${pathOf(el)} [${Math.round(r.left)},${Math.round(r.right)}]`,
        )
    }
    if (/hidden|clip/.test(`${cs.overflowX} ${cs.overflowY}`) && cs.textOverflow !== 'ellipsis') {
      if (el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1) {
        clipped.push(
          `"${(el.textContent ?? '').trim().slice(0, 20)}" ${pathOf(el)} ${el.scrollWidth}x${el.scrollHeight} vs ${el.clientWidth}x${el.clientHeight}`,
        )
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
    if (tops.size > 1)
      folded.push(`"${(node.textContent ?? '').trim().slice(0, 10)}" 折成 ${tops.size} 行 | ${pathOf(btn)}`)
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

  /* 外壳互不侵入（按**容器**包围盒量，不按文本叶子）：
     右列的胶囊是 nowrap 的，故障文案一长整列就长出轨道、往左压进主题切换器；
     侧栏与正文是 flex 同胞，宽度算错时会互相压（文本叶子那套在容器级压叠上可能报 0，
     所以单独钉一条容器级的）。 */
  const navIntrude: string[] = []
  {
    const pairs: [string, string][] = [
      ['.topbar-left', '.topbar-right'],
      ['.nav-status', '.theme-switch'],
      ['.sidebar', '.main-col'],
    ]
    for (const [x, y] of pairs) {
      const a = document.querySelector(x)?.getBoundingClientRect()
      const b = document.querySelector(y)?.getBoundingClientRect()
      if (!a || !b) continue
      const ox = Math.min(a.right, b.right) - Math.max(a.left, b.left)
      const oy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top)
      if (ox > 2 && oy > 2)
        navIntrude.push(
          `${x} 与 ${y} 压 ${Math.round(ox)}×${Math.round(oy)}px（${x} 右缘=${Math.round(a.right)}，${y} 左缘=${Math.round(b.left)}）`,
        )
    }
  }

  /* 外壳各部分都被 overflow 管着：内容比列宽就"静默裁掉"，包围盒还在原处——
     重叠检查看不见它，所以单独量 scrollWidth 与 clientWidth 的差。
     侧栏收起态短名是有意收字（display 管不到 scrollWidth），所以这里量的是展开态默认形状。 */
  const navClip: string[] = []
  for (const sel of ['.sidebar', '.nav-status', '.topbar-right']) {
    const el = document.querySelector(sel)
    if (el && el.scrollWidth > el.clientWidth + 1)
      navClip.push(`${sel} scrollWidth=${el.scrollWidth} > clientWidth=${el.clientWidth}`)
  }

  type RGBA = { r: number; g: number; b: number; a: number }
  const parse = (c: string): RGBA | null => {
    const m = /rgba?\(([^)]+)\)/.exec(c)
    if (!m) return null
    const p = m[1]
      .split(/[,\s/]+/)
      .filter(Boolean)
      .map(Number)
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 }
  }
  const gradFirst = (el: Element): RGBA | null => {
    const img = getComputedStyle(el).backgroundImage
    if (!img || img === 'none' || !img.includes('gradient(')) return null
    const rgb = /rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:[,\s/]+([\d.]+))?\s*\)/.exec(img)
    if (rgb) return { r: +rgb[1], g: +rgb[2], b: +rgb[3], a: rgb[4] === undefined ? 1 : +rgb[4] }
    const hex = /#([0-9a-f]{3,8})/i.exec(img)
    if (hex) {
      const h =
        hex[1].length === 3
          ? hex[1]
              .split('')
              .map((c) => c + c)
              .join('')
          : hex[1]
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
      base = {
        r: l.r * l.a + base.r * (1 - l.a),
        g: l.g * l.a + base.g * (1 - l.a),
        b: l.b * l.a + base.b * (1 - l.a),
        a: 1,
      }
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
    const eff =
      fg.a < 1
        ? { r: fg.r * fg.a + bg.r * (1 - fg.a), g: fg.g * fg.a + bg.g * (1 - fg.a), b: fg.b * fg.a + bg.b * (1 - fg.a) }
        : fg
    const size = parseFloat(cs.fontSize)
    const bold = Number(cs.fontWeight) >= 700
    const need = size >= 24 || (size >= 18.66 && bold) ? 3 : 4.5
    const got = ratio(eff, bg)
    if (got < need)
      lowContrast.push(
        `ratio=${got}<${need} "${(el.textContent ?? '').trim().slice(0, 16)}" ${cs.color} on rgb(${Math.round(bg.r)},${Math.round(bg.g)},${Math.round(bg.b)}) ${size}px/${cs.fontWeight} | ${pathOf(el)}`,
      )
  }

  /* 页头动作区单独再量一遍：**可点的**按钮必须过 4.5:1，禁用态按全站口径豁免。
     旧版这里走的是反豁免（`.page-head__actions` 里连禁用钮都要过），理由是
     "antd 的禁用样式是浅底 + 25% 黑字，搬进藏青横幅就成了暗底暗字——不是弱化，是读不到"。
     横幅退役成亮面之后那个前提没了，反豁免照理该一起退役；**但这条判据不删**：
     动作区是这一页的主入口，它比正文更容易被"顺手改小改淡"，所以留着正向量 AA + 一个防空跑的计数。 */
  const heroLow: string[] = []
  let heroBtnCount = 0
  /* 沉浸式编辑器的动作区是 56px 头部右列（.ed-head__right），普通页仍是 .page-head__actions。 */
  const HERO = '.page-head__actions, .ed-head__right'
  for (const el of leaves) {
    if (!(el as HTMLElement).closest(HERO)) continue
    const btn = (el as HTMLElement).closest('button')
    heroBtnCount += 1
    if (btn?.disabled) continue
    const cs = getComputedStyle(el)
    const fg = parse(cs.color)
    if (!fg) continue
    const bg = bgOf(el)
    const eff =
      fg.a < 1
        ? { r: fg.r * fg.a + bg.r * (1 - fg.a), g: fg.g * fg.a + bg.g * (1 - fg.a), b: fg.b * fg.a + bg.b * (1 - fg.a) }
        : fg
    const got = ratio(eff, bg)
    if (got < 4.5)
      heroLow.push(
        `head ratio=${got}<4.5 "${(el.textContent ?? '').trim().slice(0, 16)}" ${cs.color} on rgb(${Math.round(bg.r)},${Math.round(bg.g)},${Math.round(bg.b)})`,
      )
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

  /* 页头动作区是这一页的主入口。antd 的 small 只有 24px 高，在值班台上偏小；
     判据取 ≥34px，钉住"别再退回 small"——这条与底色无关，横幅退役后照样成立。 */
  const heroShortBtns: string[] = []
  for (const btn of [...document.querySelectorAll(`${HERO} button`)]) {
    const r = (btn as HTMLElement).getBoundingClientRect()
    if (r.height > 0 && r.height < 34)
      heroShortBtns.push(`"${(btn.textContent ?? '').trim().slice(0, 10)}" 高=${Math.round(r.height)}px`)
  }

  /* 中文值班台上不该出现 antd 默认的英文空态（"No data" 这类）。
     这一整类缺陷此前是靠人一张张截图发现的（链路表、预警表、智能体表各修过一次），
     现在钉成判据：任何一档状态下，空态文案里没有纯英文句子就算中。 */
  const englishEmpty = [
    ...document.querySelectorAll('.ant-empty-description, .ant-table-placeholder, .ant-empty-normal'),
  ]
    .map((el) => (el.textContent ?? '').trim())
    .filter((t) => t.length > 0 && !/[\u4e00-\u9fa5]/.test(t))

  /* 侧栏宽度读回：收并后顶栏不再有"绝对居中的步进器"，中线判据随结构退役；
     等价的不变量是"侧栏在场上且不侵正文"（上面 navIntrude 已钉），这里读回宽度并**连收起态一起判**：
     外壳在窄档是默认收起的（`aegis.sidebar` 持久化），64px 是设计值而不是"侧栏没了"。 */
  const side = document.querySelector('.sidebar')?.getBoundingClientRect()
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
    sideWidth: side ? Number(side.width.toFixed(2)) : null,
    sideCollapsed: !!document.querySelector('.sidebar.is-collapsed'),
  }
}

for (const width of WIDTHS) {
  test.describe(`外壳与正文不变量 @${width}`, () => {
    test.beforeEach(async ({ page }) => {
      await page.setViewportSize({ width, height: 900 })
    })
    for (const [name, path] of PAGES) {
      test(`页面 ${name}：无重叠、不出屏、无裁字、侧栏不静默裁切、对比度达标`, async ({ page }) => {
        await page.goto(path)
        await page.waitForSelector(READY, { timeout: 45_000 })
        /* 先等网络静 + 再"量到内容够了才继续"：整批跑（71 项）时前面的地图项要吃十几秒 GPU，
           监测页的台账表会晚于固定 sleep 才落地，leafCount 下限就被误触发
           （隔离跑全绿、整批偶发红，量的都是同一个页面）。这不是页面缺陷，是探针的等待方式。
           重试到 12 秒还起不来，那就当真有问题处理。 */
        await settle(page)
        await page.waitForTimeout(name === 'map' ? 12_000 : 2_500)
        /* 沉浸式页整块没有应用外壳，文本叶子本来就少（56px 头部 + 工具条 + 空态 ≈ 17），
           地板按实量定 12；仍高于"选择器全错就 0"的空跑线。 */
        const floor = immersivePath(path) ? 12 : 30
        let r = await page.evaluate(inspect, { contrastAllow: CONTRAST_ALLOW })
        for (let attempt = 0; attempt < 6 && r.leafCount <= floor; attempt++) {
          console.log(`RETRY ${name}@${width} leafCount=${r.leafCount} 还没落地，等 2s 再量`)
          await page.waitForTimeout(2_000)
          r = await page.evaluate(inspect, { contrastAllow: CONTRAST_ALLOW })
        }
        console.log(
          `UI ${name}@${width} leaves=${r.leafCount} overlap=${r.overlapCount} beyond=${r.beyondCount} clipped=${r.clippedCount} folded=${r.foldedCount} navClip=${r.navClip.length} navIntrude=${r.navIntrude.length} navTrunc=${r.navTruncated.length} lowContrast=${r.lowContrastCount} heroBtn=${r.heroBtnCount} heroLow=${r.heroLowCount} heroShort=${r.heroShortBtns.length} sideWidth=${r.sideWidth}`,
        )
        expect(r.leafCount, `${name}@${width} 页面一个文本节点都没看到——判据在空跑`).toBeGreaterThan(floor)
        expect(r.overlapCount, `${name}@${width} 重叠明细：\n${r.overlap.join('\n')}`).toBe(0)
        expect(r.beyondCount, `${name}@${width} 有内容出屏：\n${r.beyond.join('\n')}`).toBe(0)
        expect(r.clippedCount, `${name}@${width} 有文字被裁：\n${r.clipped.join('\n')}`).toBe(0)
        expect(r.foldedCount, `${name}@${width} 有按钮文字折行（"打/开"竖排）：\n${r.folded.join('\n')}`).toBe(0)
        expect(r.navClip, `${name}@${width} 外壳把内容裁掉了（列宽不够）：\n${r.navClip.join('\n')}`).toHaveLength(0)
        expect(
          r.navTruncated,
          `${name}@${width} 顶栏状态文字被省略号截断：
${r.navTruncated.join('\n')}`,
        ).toHaveLength(0)
        expect(r.navIntrude, `${name}@${width} 外壳互相侵入：\n${r.navIntrude.join('\n')}`).toHaveLength(0)
        expect(r.lowContrastCount, `${name}@${width} 有字对比度不足：\n${r.lowContrast.join('\n')}`).toBe(0)
        expect(r.heroLowCount, `${name}@${width} 页头动作区的按钮读不清：\n${r.heroLow.join('\n')}`).toBe(0)
        expect(
          r.heroShortBtns,
          `${name}@${width} 页头主入口按钮太矮（<34px）：\n${r.heroShortBtns.join('\n')}`,
        ).toHaveLength(0)
        expect(r.englishEmptyCount, `${name}@${width} 出现纯英文空态文案：\n${r.englishEmpty.join('\n')}`).toBe(0)
        if (immersivePath(path)) {
          /* 沉浸式页没有应用侧栏：等价的不变量是"编辑器占满头部以下"，而且外壳真的没渲染。
             这条同时给上面那批外壳判据兜底——它们对空集合永远为 0，不额外钉一条就等于空跑。 */
          const frame = await page.evaluate(() => {
            const head = document.querySelector('[data-testid="editor-header"]')?.getBoundingClientRect()
            const canvas = document.querySelector('[data-testid="workflow-canvas"]')?.getBoundingClientRect()
            return {
              heads: document.querySelectorAll('[data-testid="editor-header"]').length,
              sidebars: document.querySelectorAll('[data-testid="sidebar"]').length,
              topbars: document.querySelectorAll('.topbar').length,
              footers: document.querySelectorAll(
                '.footer, [data-testid="assistant-entry"], [data-testid="service-token-bar"]',
              ).length,
              canvas: canvas ? { top: canvas.top, bottom: canvas.bottom, width: canvas.width } : null,
              headH: head ? head.height : 0,
              ih: innerHeight,
            }
          })
          console.log(`IMMERSIVE ${name}@${width} ${JSON.stringify(frame)}`)
          expect(frame.heads, '沉浸式页的编辑器头部不在——判据在空跑').toBe(1)
          expect(frame.sidebars, '沉浸式页还渲染着应用侧栏').toBe(0)
          expect(frame.topbars, '沉浸式页还渲染着应用顶栏').toBe(0)
          expect(frame.footers, '沉浸式页还挂着页脚/令牌条/助手浮标').toBe(0)
          expect(frame.headH, '编辑器头部高度不是 56px（Dify top-14 那档）').toBeCloseTo(56, 0)
          expect(frame.canvas, '编辑器画布没渲染').not.toBeNull()
          if (frame.canvas) {
            expect(frame.canvas.top, '画布没从头部下沿开始').toBeCloseTo(frame.headH, 0)
            expect(frame.canvas.bottom, '画布没铺到视口底').toBeCloseTo(frame.ih, 0)
            expect(frame.canvas.width, `画布宽度 ${frame.canvas.width}，没占满整宽`).toBeCloseTo(width, 0)
          }
          expect(r.sideWidth, '沉浸式页不该再有应用侧栏宽度').toBeNull()
        } else {
          /* 侧栏必须在场上，而且宽度只能是令牌给的那两档：展开 232 / 收起 64（窄档默认收起是设计）。
             上一版写的是 ">180"，@900 的收起态 64px 必红——量的其实是"我没把收起态算进来"；
             而只放宽成 ">60" 又会把"宽度被谁裁了一半"放过去，所以这里改成**按状态判到具体档**。 */
          const sw = r.sideWidth
          const collapsed = r.sideCollapsed
          expect(sw, `${name}@${width} 侧栏不在场上——判据在空跑`).not.toBeNull()
          expect(sw, `${name}@${width} 侧栏宽 ${sw}，收起态该是 64、展开态该是 232`).toBeCloseTo(
            collapsed ? 64 : 232,
            0,
          )
        }
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
  {
    name: 'monitor-report',
    path: '/monitor',
    trigger: '[data-testid="open-report"]',
    scope: '.ant-modal-content',
    minLeaves: 8,
  },
  {
    name: 'assistant-report',
    path: '/assistant',
    trigger: '[data-testid="open-report"]',
    scope: '.ant-modal-content',
    minLeaves: 8,
  },
  {
    name: 'warnings-detail',
    path: '/warnings',
    trigger: 'role=button[name="详情"]',
    scope: '.ant-drawer-content',
    minLeaves: 40,
  },
]

for (const width of [1440, 1280, 1100, 1000]) {
  test.describe(`弹层内不变量 @${width}`, () => {
    test.beforeEach(async ({ page }) => {
      await page.setViewportSize({ width, height: 900 })
    })
    for (const c of OVERLAYS) {
      test(`${c.name}：弹层内无重叠、不出屏、无裁字、按钮不折行、对比度达标`, async ({ page }) => {
        await page.goto(c.path)
        await page.waitForSelector(READY, { timeout: 45_000 })
        /* 无超时的 networkidle 在开着 SSE 的页面上永远等不到（事件流常驻连接），
           2026-10-23 实测整条弹层判据一枚枚 180s 超时。用带兜底的 settle。 */
        await settle(page)
        await page.waitForTimeout(2_500)
        await page.locator(c.trigger).first().click()
        await page.waitForSelector(c.scope, { timeout: 15_000 })
        await page.waitForTimeout(1_200)
        const r = await page.evaluate(inspect, { contrastAllow: CONTRAST_ALLOW, scope: c.scope, inOverlay: true })
        console.log(
          `OVERLAY ${c.name}@${width} leaves=${r.leafCount} overlap=${r.overlapCount} beyond=${r.beyondCount} clipped=${r.clippedCount} folded=${r.foldedCount} lowContrast=${r.lowContrastCount}`,
        )
        await page.screenshot({ path: `test-results/ui-audit/overlay-${c.name}-${width}.png` })
        expect(
          r.leafCount,
          `${c.name}@${width} 弹层里只看到 ${r.leafCount} 个文本节点（应 ≥${c.minLeaves}）——判据在空跑`,
        ).toBeGreaterThanOrEqual(c.minLeaves)
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
      await page.waitForSelector(READY, { timeout: 45_000 })
      await settle(page)
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
      const stillOpen = await page
        .locator(c.scope)
        .first()
        .isVisible()
        .catch(() => false)
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
 * （降级页本就少几块内容，但仍必须有顶栏 + 页头 + 那句"为什么是空的"）。
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
        await page.waitForSelector(READY, { timeout: 45_000 })
        /* 先等顶栏真翻成**最长那句**故障文案再量：故障态的胶囊比在线态长
           （"后端不可达" + "事件流已中断"），只等 health 翻脸是不够的——SSE 那条先是
           "事件流重连中"（短 65px），此时右列已经长出轨道、但还没长到压住步进器，
           量到的就是"假绿"。变异实测：只等 /不可达/ 时这条判据抓不到右列侵入 78px。 */
        if (!immersivePath(path)) {
          await page
            .waitForFunction(
              () =>
                /不可达/.test(document.querySelector('.nav-status')?.textContent ?? '') &&
                /已中断/.test(document.querySelector('.nav-status')?.textContent ?? ''),
              undefined,
              { timeout: 25_000 },
            )
            .catch(() => {})
        }
        await page.waitForTimeout(1_000)
        const r = await page.evaluate(inspect, { contrastAllow: CONTRAST_ALLOW })
        console.log(
          `FAULT ${name}@${width} leaves=${r.leafCount} overlap=${r.overlapCount} beyond=${r.beyondCount} clipped=${r.clippedCount} folded=${r.foldedCount} navClip=${r.navClip.length} navIntrude=${r.navIntrude.length} navTrunc=${r.navTruncated.length} lowContrast=${r.lowContrastCount} heroLow=${r.heroLowCount}`,
        )
        await page.screenshot({ path: `test-results/ui-audit/fault-${name}-${width}.png` })
        expect(
          r.leafCount,
          `${name}@${width} 故障态只看到 ${r.leafCount} 个文本节点——判据在空跑`,
        ).toBeGreaterThanOrEqual(12)
        expect(r.overlapCount, `${name}@${width} 故障态重叠：\n${r.overlap.join('\n')}`).toBe(0)
        expect(r.beyondCount, `${name}@${width} 故障态出屏：\n${r.beyond.join('\n')}`).toBe(0)
        expect(r.clippedCount, `${name}@${width} 故障态文字被裁：\n${r.clipped.join('\n')}`).toBe(0)
        expect(r.foldedCount, `${name}@${width} 故障态按钮折行：\n${r.folded.join('\n')}`).toBe(0)
        expect(r.lowContrastCount, `${name}@${width} 故障态对比度不足：\n${r.lowContrast.join('\n')}`).toBe(0)
        expect(r.heroLowCount, `${name}@${width} 故障态页头按钮读不清：\n${r.heroLow.join('\n')}`).toBe(0)
        expect(r.navClip, `${name}@${width} 故障态顶栏静默裁字：\n${r.navClip.join('\n')}`).toHaveLength(0)
        expect(
          r.navTruncated,
          `${name}@${width} 故障态顶栏状态文字被截断：
${r.navTruncated.join('\n')}`,
        ).toHaveLength(0)
        expect(r.navIntrude, `${name}@${width} 故障态顶栏三列互相侵入：\n${r.navIntrude.join('\n')}`).toHaveLength(0)
        expect(
          r.heroShortBtns,
          `${name}@${width} 故障态页头主入口按钮太矮：\n${r.heroShortBtns.join('\n')}`,
        ).toHaveLength(0)
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
      await page.waitForSelector(READY, { timeout: 45_000 })
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
        return { heroes, bare, custom, inCard: document.querySelectorAll('.ant-card .ant-empty').length }
      })
      console.log(
        `EMPTY ${name} leaves=${r.leafCount} overlap=${r.overlapCount} clipped=${r.clippedCount} folded=${r.foldedCount} lowContrast=${r.lowContrastCount} heroes=${empty.heroes.length} withAction=${empty.heroes.filter((h) => h.action).length} bare=${empty.bare} custom=${empty.custom} inCard=${empty.inCard}`,
      )
      await page.screenshot({ path: `test-results/ui-audit/empty-${name}.png` })
      expect(
        empty.heroes.length + empty.bare + empty.custom,
        `${name} 一个空态都没渲染出来——这项目判据就没了对象`,
      ).toBeGreaterThan(0)
      expect(empty.bare, `${name} 还有 ${empty.bare} 处 antd 默认"暂无数据"（不说为什么没有、也不说下一步去哪）`).toBe(
        0,
      )
      if (empty.heroes.length > 0) {
        expect(
          empty.heroes.some((h) => h.action),
          `${name} 的空态没有"下一步去哪"那个出口`,
        ).toBe(true)
        /* 同一屏不许并排两种空态习语：有芯片块的那一页，卡片正文里就不该再出现
           antd 那种居中大插图（它没有出口，读起来像"这块坏了"而不是"这块还没数据"）。
           地图页不受这条约束：它那一处是"未选中要素"的**选择提示**，heroes=0 时不触发。 */
        expect(empty.inCard, `${name} 同一屏并排着两种空态习语（芯片块 + ${empty.inCard} 处 antd 居中空态）`).toBe(0)
      }
      if (name === 'dashboard') {
        /* 这两块是第三十批从 a-empty 换过来的：换法要能被机器钉住，否则下次有人图省事又换回去。 */
        for (const id of ['region-empty', 'events-empty']) {
          const hero = page.locator(`[data-testid="${id}"]`)
          await expect(hero, `态势页缺了 ${id} 那块带出口的空态`).toHaveCount(1)
          await expect(hero.locator('.empty-hero__action'), `${id} 没有"下一步去哪"那个出口`).toHaveCount(1)
        }
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
/**
 * 门禁自带的大定义（2026-10-06 加）。
 *
 * 撞过的坑：那条"低 zoom 描边还剩多少设备像素"的判据要一份 >30 节点的定义，
 * 原先**依赖环境里恰好留着一份**——后端换过一次实例（store 是内存态）之后列表里最大只剩 11 节点，
 * 判据按设计落红"在空跑"。门禁不许靠 leftovers，要用的数据自己种；名字固定、幂等。
 *
 * 形状是**九段 × 每段五路并行**而不是 45 颗串成一条：一条长链的画布是 9280×120 的细条，
 * 按 `minZoom=0.2` 夹住后 fit 只能收到 0.2、**40 颗里 26 颗在视野外**（真机量到，2s/5s/10s 三次一样），
 * 那是判据在考"画布能不能看全"时被自己的夹具带偏。真实预案那种有并行分支的图才是这里要的形状。
 */
const GATE_BIG_DEF = 'GATE-UI大定义'
const GATE_STAGES = 9
const GATE_STAGE_WIDTH = 5
const GATE_BIG_DEF_NODES = GATE_STAGES * GATE_STAGE_WIDTH

async function ensureBigDefinition(request: APIRequestContext): Promise<void> {
  const base = process.env.AEGIS_API_TARGET ?? 'http://127.0.0.1:8000'
  const list = await request.get(`${base}/api/v1/workflow/definitions`)
  expect(list.status(), `取定义列表就是 ${list.status()}——AEGIS_API_TARGET 指的后端在位吗？`).toBe(200)
  const body = (await list.json()) as {
    items?: Array<{ workflow_id: string; name: string; node_count: number }>
  }
  const items = body.items ?? []
  /* 形状换过时把旧那份归档掉：同名两份会让"打开最大的那份"这一句含糊。 */
  for (const stale of items.filter((d) => d.name === GATE_BIG_DEF && d.node_count !== GATE_BIG_DEF_NODES)) {
    await request.post(`${base}/api/v1/workflow/definitions/${encodeURIComponent(stale.workflow_id)}/archive`)
  }
  if (items.some((d) => d.name === GATE_BIG_DEF && d.node_count === GATE_BIG_DEF_NODES)) return
  const idOf = (stage: number, lane: number): string => `gate_s${stage}_l${lane}`
  const nodes = Array.from({ length: GATE_STAGES * GATE_STAGE_WIDTH }, (_, i) => ({
    node_id: idOf(Math.floor(i / GATE_STAGE_WIDTH), i % GATE_STAGE_WIDTH),
    type: 'notify',
    name: `大定义 S${Math.floor(i / GATE_STAGE_WIDTH)}·L${i % GATE_STAGE_WIDTH}`,
    config: { text: `s${Math.floor(i / GATE_STAGE_WIDTH)}l${i % GATE_STAGE_WIDTH}` },
    sla_ms: 8000,
    timeout_ms: 5000,
    on_failure: 'abort',
    retry: { max_attempts: 1, backoff_ms: 0 },
  }))
  const edges: Array<{ source: string; target: string }> = []
  for (let stage = 0; stage + 1 < GATE_STAGES; stage += 1) {
    for (let lane = 0; lane < GATE_STAGE_WIDTH; lane += 1) {
      edges.push({ source: idOf(stage, lane), target: idOf(stage + 1, lane) })
    }
    /* 每段再搭一条跨道连线：让布局真出现交叉，而不是五条互不相干的平行线。 */
    edges.push({ source: idOf(stage, 0), target: idOf(stage + 1, 1) })
  }
  const created = await request.post(`${base}/api/v1/workflow/definitions`, {
    data: {
      name: GATE_BIG_DEF,
      description: `UI 门禁自带的大定义（${GATE_STAGES} 段 × ${GATE_STAGE_WIDTH} 路，低 zoom 描边与"看全图"判据用）`,
      nodes,
      edges,
    },
  })
  expect(created.status(), `种一份 ${GATE_BIG_DEF_NODES} 节点的大定义失败（${created.status()}）`).toBe(201)
}

/**
 * 门禁的两条**数据前置**：预警列表与实例队列各至少要有一行。
 *
 * 之前它们靠"这台机器的后端恰好有数据"：内存 store 一重启就空，
 * `role=button[name="详情"]` 与 `.wf-run__pick` 各自等到 180 秒超时——
 * 2026-10-07 那一趟 100 项里红了 6 项，红的不是排版而是**没有行可点**，
 * 而画面判据本来要看的正是"点开之后那一屏"。这种红既吓人又费人，必须自己种。
 * 走真接口（`/drill/run`、`/workflow/instances`），不在组件里塞假数据；
 * 需要空态的那些项早就是用 `page.route` 自己灌空列表的，加真数据不会串台。
 */
async function ensureGateRows(request: APIRequestContext): Promise<void> {
  const base = process.env.AEGIS_API_TARGET ?? 'http://127.0.0.1:8000'
  const countOf = async (path: string): Promise<number> => {
    const res = await request.get(`${base}${path}`)
    expect(res.status(), `${path} 返回 ${res.status()}——AEGIS_API_TARGET 指的后端在位吗？`).toBe(200)
    return ((await res.json()) as { count?: number }).count ?? 0
  }

  for (let round = 0; round < 3 && (await countOf('/api/v1/warnings?limit=1')) === 0; round += 1) {
    const drill = await request.post(`${base}/api/v1/drill/run`, { data: { scenario: 'surge' } })
    expect(drill.status(), `演练注入返回 ${drill.status()}——预警抽屉那条判据没有行可点`).toBe(200)
  }
  expect(
    await countOf('/api/v1/warnings?limit=1'),
    '三轮 surge 之后预警仍是 0 条（模拟器没启用，或触发规则一条没命中）',
  ).toBeGreaterThan(0)

  if ((await countOf('/api/v1/workflow/instances?limit=1')) === 0) {
    await ensureBigDefinition(request)
    const started = await request.post(`${base}/api/v1/workflow/instances`, { data: { workflow_name: GATE_BIG_DEF } })
    expect(started.status(), `用 ${GATE_BIG_DEF} 起一条实例失败（${started.status()}）`).toBeLessThan(400)
  }
  expect(await countOf('/api/v1/workflow/instances?limit=1'), '实例队列是空的——时间线那条判据在空跑').toBeGreaterThan(0)

  /* 记下其中一条真实例的身份：两条判据要拿它当"真存在的工作流"的锚点——
     `运行时间线` 灌的那份详情得能被 `getVersionDocument` 接住（编一个 workflow_id 会被编解码层拒掉），
     `减动效` 那一页要用 `?instance=` 深链把编辑器开成"正在轮询"的状态。 */
  const list = await request.get(`${base}/api/v1/workflow/instances?limit=1`)
  expect(list.status(), '读实例列表返回的不是 200').toBe(200)
  const first = ((await list.json()) as { items?: Array<{ instance_id: string; workflow_id: string }> }).items?.[0]
  if (first === undefined) throw new Error('实例列表 count>0 却读不到第一行')
  gateInstance = { instanceId: first.instance_id, workflowId: first.workflow_id }
}

/** 门禁自己种/读到的那一条真实例（`ensureGateRows` 里落账）。 */
let gateInstance: { instanceId: string; workflowId: string } | null = null

/** 桩要按调用方**真正要的那一条**回答，不能把实例号写死——写死了就等于换了个数据源在测。 */
function instanceIdOf(url: string): string {
  return new URL(url).pathname.split('/').pop() ?? ''
}

/** 灌一份"还有一步没走"的实例详情：dashed 档与轮询脉冲都要靠它确定性出现。 */
function probeInstanceNode(id: string, state: string, extra: Record<string, unknown> = {}): Record<string, unknown> {
  return {
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
  }
}

function probeInstanceBody(instanceId: string, workflowId: string, nodes: Array<Record<string, unknown>>): string {
  return JSON.stringify({
    instance_id: instanceId,
    workflow_id: workflowId,
    workflow_version: 1,
    trace_id: 'trc_probe',
    status: 'running',
    error: null,
    payload: {},
    nodes,
  })
}

test.beforeAll(async ({ request }) => {
  await ensureGateRows(request)
})

/** 从定义行上取"哪一行最大、它有几个节点"：只读那一格自己的文字，
 *  读整行会把名字/版本与数字粘成 "v140 节点"（真机就这么误判过一次）。 */
function pickBiggestDefinition(): { best: number; bestCount: number } {
  const rows = [...document.querySelectorAll('.wf-def__list-item')]
  let best = 0
  let bestCount = -1
  rows.forEach((row, i) => {
    const meta = row.querySelector('.wf-def__meta') ?? row
    const m = /^(\d+)\s*节点/.exec((meta.textContent ?? '').trim())
    const n = m ? Number(m[1]) : 0
    if (n > bestCount) {
      bestCount = n
      best = i
    }
  })
  return { best, bestCount }
}

test.describe('画布节点不变量 @1440', () => {
  test.beforeAll(async ({ request }) => {
    await ensureBigDefinition(request)
  })

  test('打开有节点的定义：节点卡不互相压叠、标题不出框', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.goto('/workflow')
    await page.waitForSelector(READY, { timeout: 45_000 })
    await settle(page)
    await page.waitForTimeout(2_000)
    /* 刚进页面时是"新建画布"那份空定义：① 画布上没有节点，小地图里什么都没有，不许把它画出来
       （但它得**挂在 DOM 里**——落点让位要量它的真实尺寸当禁落区，见下面那条拖拽用例）；
       ② 页头里"保存定义"此时是禁用态。全局对比度判据对 `.ant-btn[disabled]` 照旧豁免
       （浅底上那是"点不动"的提示，不算读不到），但这里**钉住"真的有禁用按钮"、
       且动作区里量得到按钮文本**——不然这条判据会静默空跑。 */
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
      disabled: document.querySelectorAll('.ed-head__right button:disabled').length,
    }))
    const heroC = await page.evaluate(inspect, { contrastAllow: CONTRAST_ALLOW })
    console.log(
      `CANVAS[empty] nodes=${empty.nodes} minimap=${empty.minimap} mounted=${empty.minimapMounted} heroBtn=${heroC.heroBtnCount} heroDisabled=${empty.disabled} heroLow=${heroC.heroLowCount}`,
    )
    expect(empty.nodes, '这条要量的是"空画布"，但画布上已经有节点了').toBe(0)
    expect(empty.minimap, '空画布上还画着一个小地图（里面什么都没有，只是块白框）').toBe(false)
    expect(empty.minimapMounted, '空画布上小地图整个不在 DOM 里——落点让位就拿不到它的尺寸，第一颗节点会被它盖住').toBe(
      true,
    )
    expect(empty.disabled, '页头里没有禁用态按钮——这条判据在空跑（"保存定义"此时该是禁用的）').toBeGreaterThan(0)
    expect(heroC.heroBtnCount, '页头动作区一个按钮文本都没量到——判据在空跑').toBeGreaterThanOrEqual(2)
    expect(heroC.heroLowCount, `页头动作区的按钮读不清：\n${heroC.heroLow.join('\n')}`).toBe(0)

    /* 右栏从 2026-10-23 起是覆盖式面板（默认关闭）：点头部「定义」打开，再切到定义档。 */
    await page.locator('[data-testid="header-def"]').click()
    await page.waitForSelector('[data-testid="editor-panel"] .wf-def', { timeout: 15_000 })
    await page.waitForTimeout(800)
    /* 挑"节点最多的那份定义"来量：两份只有 2 个节点的压不出布局问题。
       计数只读那一格自己的文字（读整行会把名字/版本与数字粘起来，"v1"+"40 节点"→"140"）。 */
    const idx = await page.evaluate(pickBiggestDefinition)
    expect(idx.bestCount, '服务端定义列表里没有带节点的定义——判据在空跑').toBeGreaterThan(1)
    await page.locator('[data-testid^="open-"]').nth(idx.best).click()
    const confirm = page.locator('.ant-modal-confirm .ant-btn-primary')
    if (await confirm.isVisible().catch(() => false)) await confirm.click()
    await page.waitForSelector('.wf-node', { timeout: 20_000 })
    /* 面板是覆盖式的：开着它会压住画布右缘近 400px，量"整图收拢"要关掉它。 */
    await page.locator('[data-testid="panel-close"]').click()
    await page.waitForTimeout(2_500)
    const measure = () =>
      page.evaluate(() => {
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
              overlap.push(
                `${(nodes[i].querySelector('.wf-node__title')?.textContent ?? '').trim()} × ${(nodes[j].querySelector('.wf-node__title')?.textContent ?? '').trim()} 压叠 ${Math.round(ox)}×${Math.round(oy)}`,
              )
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
            spill.push(
              `"${(title.textContent ?? '').trim().slice(0, 18)}" 画到框外（右缘差=${Math.round(tr.right - nr.right)} 左缘差=${Math.round(tr.left - nr.left)}）`,
            )
          }
        }
        const zoom = Number(
          document
            .querySelector('.vue-flow__viewport')
            ?.getAttribute('style')
            ?.match(/zoom\(([\d.]+)\)/)?.[1] ?? 1,
        )
        /* 画布浮层（小地图 / 缩放控件）此前是判据死角：@vue-flow 默认只给 minimap 一个
         `background-color:#fff`，落进我们的圆角玻璃画布就是一块生硬白方块（真机截图里那个
         "空白矩形"）。判据：要么不出现，要么就得是卡片族的样子（圆角 + 描边或投影）且不出画布。 */
        const canvasRect = document.querySelector('[data-testid="workflow-canvas"]')?.getBoundingClientRect()
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
      console.log(
        `CANVAS[${phase}] nodes=${r.count} overlap=${r.overlap.length} spill=${r.spill.length} minimap=${JSON.stringify(r.minimap)} controls=${JSON.stringify(r.controls)}`,
      )
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
    /* 「自动布局」在头部“更多”菜单里，画布工具条上有一枚常驻的同一个动作 */
    await page.locator('[data-testid="toolbar-layout"]').click()
    await page.waitForTimeout(1_800)
    await check('auto-layout')
  })

  /**
   * 连续添加三颗节点：落点让位（压叠 / 出画布 / 被浮层盖住）与 zoom 不跳档。
   *
   * 交互从 2026-10-23 起改为 Dify 式"工具条 → 块选择器 → 点选"（旧的下拉面板拖拽已退役），
   * 落点仍走同一支 `freeDropPosition`；此时右栏是**覆盖式面板**（400px），
   * 正是让位要躲的不透明浮层之一。
   * 历次真机量到的缺陷（保留作判据来源与变异依据）：
   * - 压叠：第二张压住第一张 **389×234px**（落点原样取光标位置，而节点卡有 240px 宽）。
   * - 出画布：只躲压叠不躲可视区时落到 y=704..957（画布下沿 880）、x 到 1128（画布右 1068）。
   * - 被浮层盖住：收进框内后又压在小地图那块不透明卡片下（实测 171×102px）。
   * - **空画布上的第一颗**：那一刻小地图还没挂载 ⇒ 禁落区为空，节点落好后立刻被刚挂载的
   *   小地图盖住（实测 171×114px）——所以小地图改成"空画布不画但仍在 DOM 里"。
   * 根因还有 init-fit：空画布时它不跑，第一颗节点落下才 fit 且不带参数，zoom 被顶到 maxZoom=2，
   * 可视区只剩 414×339 flow 单位（不到两个节点宽）——所以先钉 zoom，再钉症状。
   */
  test('连续添加三颗节点：不压叠、不出画布、不被浮层盖住、画布不被 fit 放大', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.goto('/workflow')
    await page.waitForSelector(READY, { timeout: 45_000 })
    await settle(page)
    await page.waitForTimeout(3_000)
    /* 每次取"第一个仍可选的块"：第一颗之后 Start 会因"已有开始节点"禁用，
       于是三次点选自然落到三个不同类型上（搜索/禁用逻辑顺带被真机走一遍）。 */
    const addBlock = async (): Promise<void> => {
      await page.locator('[data-testid="toolbar-add"]').click()
      await page.waitForSelector('[data-testid="block-selector"]', { timeout: 10_000 })
      const rows = page.locator('.block-selector__row:not(.is-disabled)')
      expect(await rows.count(), '块选择器里一个可选项都没有——判据在空跑').toBeGreaterThan(2)
      await rows.first().click()
      await page.waitForTimeout(700)
    }
    await addBlock()
    await addBlock()
    await addBlock()
    await page.waitForTimeout(900)
    const m = await page.evaluate(() => {
      const nodes = [...document.querySelectorAll('.wf-node')]
      const rect = (el: Element) => el.getBoundingClientRect()
      const frame = document.querySelector('[data-testid="workflow-canvas"]')?.getBoundingClientRect()
      const overlap: string[] = []
      for (let i = 0; i < nodes.length; i += 1) {
        for (let j = i + 1; j < nodes.length; j += 1) {
          const a = rect(nodes[i])
          const b = rect(nodes[j])
          const ox = Math.min(a.right, b.right) - Math.max(a.left, b.left)
          const oy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top)
          if (ox > 4 && oy > 4) {
            overlap.push(
              `${(nodes[i].querySelector('.wf-node__title')?.textContent ?? '').trim()} × ${(nodes[j].querySelector('.wf-node__title')?.textContent ?? '').trim()} 压 ${Math.round(ox)}×${Math.round(oy)}px`,
            )
          }
        }
      }
      const outside = nodes
        .map((n, i) => {
          const r = rect(n)
          if (!frame) return ''
          const bad =
            r.left < frame.left - 1 || r.top < frame.top - 1 || r.right > frame.right + 1 || r.bottom > frame.bottom + 1
          return bad
            ? `第 ${i + 1} 张出画布（${Math.round(r.left)},${Math.round(r.top)},${Math.round(r.right)},${Math.round(r.bottom)} vs 画布 ${Math.round(frame.left)},${Math.round(frame.top)},${Math.round(frame.right)},${Math.round(frame.bottom)}）`
            : ''
        })
        .filter((s) => s !== '')
      const titles = nodes.map((n) => (n.querySelector('.wf-node__title')?.textContent ?? '').trim())
      /* 小地图/缩放控件/工具条/右栏都是画布上的不透明浮层：节点被盖住＝看不见，和出画布同一类缺陷。 */
      const overlays = [
        ...document.querySelectorAll('.vue-flow__minimap, .vue-flow__controls, .ed-toolbar, .ed-panel'),
      ].map(rect)
      const covered: string[] = []
      for (const n of nodes) {
        const r = rect(n)
        for (const o of overlays) {
          const ox = Math.min(r.right, o.right) - Math.max(r.left, o.left)
          const oy = Math.min(r.bottom, o.bottom) - Math.max(r.top, o.top)
          if (ox > 4 && oy > 4) {
            covered.push(
              `${(n.querySelector('.wf-node__title')?.textContent ?? '').trim()} 被浮层盖住 ${Math.round(ox)}×${Math.round(oy)}px`,
            )
          }
        }
      }
      const pane = document.querySelector('.vue-flow__transformationpane')
      const t = pane ? getComputedStyle(pane).transform : ''
      const zoom = t.startsWith('matrix') ? Number(t.slice(t.indexOf('(') + 1).split(',')[0]) : Number.NaN
      return { count: nodes.length, overlap, outside, covered, titles, zoom }
    })
    console.log(
      `DROPCOLLIDE nodes=${m.count} titles=${JSON.stringify(m.titles)} overlap=${m.overlap.length} outside=${m.outside.length} covered=${JSON.stringify(m.covered)} zoom=${m.zoom}`,
    )
    await page.screenshot({ path: 'test-results/ui-audit/canvas-drop.png' })
    expect(m.count, '三次点选后画布上应当有三张节点卡（添加没生效＝判据在空跑）').toBe(3)
    /* 先钉 zoom：init-fit 一旦被顶到 maxZoom，可视区就只有两个节点宽，
       后面"出画布/被盖住"都是它的下游症状——报根因比报症状有用。 */
    expect(m.zoom, `加完三张之后画布 zoom=${m.zoom}，fit 把画布放大了`).toBeCloseTo(0.9, 1)
    expect(m.overlap, `落点压叠：\n${m.overlap.join('\n')}`).toHaveLength(0)
    expect(m.outside, `让位把节点推出了画布：\n${m.outside.join('\n')}`).toHaveLength(0)
    expect(m.covered, `落点被画布浮层盖住：\n${m.covered.join('\n')}`).toHaveLength(0)
  })

  /**
   * 低 zoom 下描边还剩几个设备像素。
   *
   * 画布的 zoom 作用在整个变换层上，1px 的连线与节点描边会跟着缩。真机打开大定义（原先是环境里那份
   * 111 节点的，fit 落在 zoom=0.318；现在换成门禁自种的 `GATE-UI大定义`，见 `ensureBigDefinition`），
   * 修之前连线（库里默认还是浅灰 #b1b1b7）只剩 **0.32 设备像素**，
   * 基本看不见；而"看全局"恰恰就是要缩到那一档。修完按 `1/zoom`（封顶 3）补偿。
   */
  test('打开最大的那份定义：连线与节点描边仍有可辨粗细（不是 0.3 设备像素）', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.goto('/workflow')
    await page.waitForSelector(READY, { timeout: 45_000 })
    await settle(page)
    await page.waitForTimeout(3_000)
    await page.locator('[data-testid="header-def"]').click()
    await page.waitForSelector('[data-testid="editor-panel"] .wf-def', { timeout: 15_000 })
    await page.waitForTimeout(1_500)
    const pick = await page.evaluate(pickBiggestDefinition)
    expect(pick.bestCount, '定义列表里没有大定义——这条判据在空跑').toBeGreaterThan(30)
    await page.locator('[data-testid^="open-"]').nth(pick.best).click()
    const confirm = page.locator('.ant-modal-confirm .ant-btn-primary')
    if (await confirm.isVisible().catch(() => false)) await confirm.click()
    /* 等的是"条数"而不是"某条可见"：直上直下的连线是一条水平线，SVG path 的包围盒高度为 0，
       Playwright 的可见性判定会把它当成不可见而干等到超时（45 节点那份九段×五路里 45 条连线全是水平线）。
       这条判据要量的是描边粗细，元素在不在 DOM 里才是前提。 */
    await page.waitForFunction(() => document.querySelectorAll('.vue-flow__edge-path').length > 3, null, {
      timeout: 30_000,
    })
    /* fit 是按整块画布算的；开着 400px 覆盖面板量"视野外"会把被面板遮住也算进去，先关。 */
    await page.locator('[data-testid="panel-close"]').click()
    await page.waitForTimeout(2_500)
    const m = await page.evaluate(() => {
      const pane = document.querySelector('.vue-flow__transformationpane')
      const t = pane ? getComputedStyle(pane).transform : ''
      const zoom = t.startsWith('matrix') ? Number(t.slice(t.indexOf('(') + 1).split(',')[0]) : Number.NaN
      const edge = document.querySelector('.vue-flow__edge-path') as SVGElement | null
      const node = document.querySelector('.wf-node') as HTMLElement | null
      const es = edge ? getComputedStyle(edge) : null
      const ns = node ? getComputedStyle(node) : null
      return {
        zoom,
        edges: document.querySelectorAll('.vue-flow__edge-path').length,
        strokePx: Number.parseFloat(es?.strokeWidth ?? '0') * zoom,
        stroke: es?.stroke ?? '',
        borderPx: Number.parseFloat(ns?.borderTopWidth ?? '0') * zoom,
        barPx: Number.parseFloat(ns?.borderLeftWidth ?? '0') * zoom,
        /* "收拢"这一句到底收没收到：节点卡有几张落在画布可视区之外。
           长链那种极端纵横比的图会撞上 `minZoom=0.2` 的夹持（40 颗里 26 颗在视野外），
           判据要的是真实形状的定义下 fit 能把全图收进眼里。 */
        nodeCount: document.querySelectorAll('.vue-flow__node').length,
        offView: (() => {
          const frame = document.querySelector('[data-testid="workflow-canvas"]')?.getBoundingClientRect()
          if (!frame) return -1
          return [...document.querySelectorAll('.vue-flow__node')].filter((n) => {
            const r = n.getBoundingClientRect()
            return (
              r.left < frame.left - 1 ||
              r.top < frame.top - 1 ||
              r.right > frame.right + 1 ||
              r.bottom > frame.bottom + 1
            )
          }).length
        })(),
      }
    })
    console.log(
      `STROKE zoom=${m.zoom} edges=${m.edges} 线=${m.strokePx.toFixed(2)}设备px/${m.stroke} 描边=${m.borderPx.toFixed(2)} 类别条=${m.barPx.toFixed(2)} 节点=${m.nodeCount} 视野外=${m.offView}`,
    )
    expect(m.edges, '画布上一条连线都没有——判据在空跑').toBeGreaterThan(3)
    expect(m.zoom, `这份定义的 fit 落在 ${m.zoom}，不在"看全局"那一档，前提变了`).toBeLessThan(0.6)
    expect(
      m.strokePx,
      `连线只剩 ${m.strokePx.toFixed(2)} 设备像素，缩到看全局那档就看不见链路了`,
    ).toBeGreaterThanOrEqual(1.2)
    expect(m.borderPx, `节点描边只剩 ${m.borderPx.toFixed(2)} 设备像素`).toBeGreaterThanOrEqual(0.8)
    expect(m.barPx, `节点类别条只剩 ${m.barPx.toFixed(2)} 设备像素`).toBeGreaterThanOrEqual(2)
    expect(m.offView, `fit 之后仍有 ${m.offView}/${m.nodeCount} 张节点卡在画布视野外（收拢没收到全图）`).toBe(0)
    /* 颜色也要脱离库里那支浅灰：#94a3b8 = rgb(148,163,184) */
    expect(m.stroke, `连线还是库里默认的 ${m.stroke}`).not.toBe('rgb(177, 177, 183)')
    await page.screenshot({ path: 'test-results/ui-audit/canvas-lowzoom-stroke.png' })
  })

  /**
   * 深色下的**画布**——"暗色不许白底浅字"这条口径第一次有机器证据。
   *
   * 常驻判据把 `.vue-flow` 整块跳过了（里面有 canvas 与大量绝对定位），所以节点卡本身从没被量过，
   * 而编辑器恰恰是这一批改的主体。这里不套 inspect，按卡片自己量三件事：底色必须是暗面、
   * 标题压在底色上要过 AA、连线压在画布底上要有可辨的图形对比（≥3:1）。
   */
  test('深色画布：节点卡不是白底浅字，连线不是库里那支浅灰 @1440', async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem('aegis.theme', 'dark'))
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.goto('/workflow')
    await page.waitForSelector(READY, { timeout: 45_000 })
    await settle(page)
    await page.waitForTimeout(2_500)
    await page.locator('[data-testid="header-def"]').click()
    await page.waitForSelector('[data-testid="editor-panel"] .wf-def', { timeout: 15_000 })
    await page.waitForTimeout(1_500)
    const pick = await page.evaluate(pickBiggestDefinition)
    expect(pick.bestCount, '定义列表里没有大定义——这条判据在空跑').toBeGreaterThan(30)
    await page.locator('[data-testid^="open-"]').nth(pick.best).click()
    const confirm = page.locator('.ant-modal-confirm .ant-btn-primary')
    if (await confirm.isVisible().catch(() => false)) await confirm.click()
    await page.waitForSelector('.wf-node', { timeout: 20_000 })
    await page.locator('[data-testid="panel-close"]').click()
    await page.waitForTimeout(1_500)
    const m = await page.evaluate(() => {
      type RGB = { r: number; g: number; b: number }
      const parse = (c: string): RGB | null => {
        const hit = /rgba?\(([^)]+)\)/.exec(c)
        if (!hit) return null
        const p = hit[1]
          .split(/[,\s/]+/)
          .filter(Boolean)
          .map(Number)
        return { r: p[0] as number, g: p[1] as number, b: p[2] as number }
      }
      const lum = (x: RGB): number => {
        /* WCAG 的线性化是按 0–1 的 sRGB 算的：先除以 255 再比 0.03928。
           第一版直接拿 0–255 去比，45 张卡全被判成"亮面底"——红的是这条判据自己的算术。 */
        const f = (v: number): number => {
          const s = v / 255
          return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4)
        }
        return 0.2126 * f(x.r) + 0.7152 * f(x.g) + 0.0722 * f(x.b)
      }
      const ratio = (a: RGB, b: RGB): number => {
        const hi = Math.max(lum(a), lum(b))
        const lo = Math.min(lum(a), lum(b))
        return (hi + 0.05) / (lo + 0.05)
      }
      const problems: string[] = []
      const canvas = document.querySelector('.ed-canvas')
      const canvasBg = canvas ? parse(getComputedStyle(canvas).backgroundColor) : null
      const cards = [...document.querySelectorAll('.wf-node')]
      for (const card of cards) {
        const bg = parse(getComputedStyle(card).backgroundColor)
        const title = card.querySelector('.wf-node__title')
        const fg = title ? parse(getComputedStyle(title).color) : null
        if (!bg || !fg) {
          problems.push('节点卡的底色或标题色读不出来')
          break
        }
        if (lum(bg) > 0.2) problems.push(`节点卡是亮面底 rgb(${bg.r},${bg.g},${bg.b})——深主题下画布发白`)
        const got = ratio(fg, bg)
        if (got < 4.5) problems.push(`标题 ${got.toFixed(2)}:1 压在 rgb(${bg.r},${bg.g},${bg.b}) 上不足 AA`)
      }
      const edge = document.querySelector('.vue-flow__edge-path')
      const edgeStroke = edge ? parse(getComputedStyle(edge).stroke) : null
      if (!edgeStroke || !canvasBg) problems.push('连线颜色或画布底色读不出来')
      else {
        const got = ratio(edgeStroke, canvasBg)
        if (got < 3) problems.push(`连线 ${got.toFixed(2)}:1 压在画布底上看不见（图形对比要 ≥3）`)
      }
      return {
        cards: cards.length,
        problems,
        edgeStroke: edgeStroke ? `rgb(${edgeStroke.r},${edgeStroke.g},${edgeStroke.b})` : null,
        cardBg: cards[0] ? getComputedStyle(cards[0]).backgroundColor : null,
      }
    })
    console.log(`DARK-CANVAS cards=${m.cards} cardBg=${m.cardBg} stroke=${m.edgeStroke} problems=${m.problems.length}`)
    expect(m.cards, '深色画布上一张节点卡都没有——判据在空跑').toBeGreaterThan(10)
    expect(m.problems, `深色画布读不清：\n${m.problems.slice(0, 6).join('\n')}`).toHaveLength(0)
    await page.screenshot({ path: 'test-results/ui-audit/canvas-dark.png' })
  })

  /**
   * 画布快捷键：值班员一天里要反复"看全局 → 看这一片 → 回到默认"，
   * 这些动作原先只能靠右下角那几个 22px 的控件钮，鼠标得从画布中央跑过去。
   * 另一件必须钉住的是**不抢键**：右栏"中止理由"输入框里打 0 是打字符，不是缩放画布。
   */
  test('画布快捷键 1 / 0 / + / - 生效，且在输入框里不抢键', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.goto('/workflow')
    await page.waitForSelector(READY, { timeout: 45_000 })
    await settle(page)
    await page.waitForTimeout(3_000)
    const canvas = page.locator('[data-testid="workflow-canvas"]')
    /* 画布上得有一颗节点，'1'（收拢）才有可 fit 的对象。 */
    await page.locator('[data-testid="toolbar-add"]').click()
    await page.waitForSelector('[data-testid="block-selector"]', { timeout: 10_000 })
    await page.locator('.block-selector__row:not(.is-disabled)').first().click()
    await page.waitForTimeout(800)
    const zoom = (): Promise<number> =>
      page.evaluate(() => {
        const pane = document.querySelector('.vue-flow__transformationpane')
        const t = pane ? getComputedStyle(pane).transform : ''
        return t.startsWith('matrix') ? Number(t.slice(t.indexOf('(') + 1).split(',')[0]) : Number.NaN
      })
    /* (40,40) 会点进左上角工具条；挑一块空画布落焦点。 */
    await canvas.click({ position: { x: 400, y: 520 } })
    const start = await zoom()
    /* 先按 - 把缩放推离默认档，再按 0 才看得出"回到默认"真的发生了。 */
    await page.keyboard.press('-')
    await page.waitForTimeout(500)
    const afterMinus = await zoom()
    await page.keyboard.press('0')
    await page.waitForTimeout(500)
    const afterZero = await zoom()
    await page.keyboard.press('+')
    await page.waitForTimeout(500)
    const afterPlus = await zoom()
    /* 「适配画布」是 Mod+1（Shift+1 是 100%）；按 1 什么都不会发生，判据会在那里空跑。 */
    await page.keyboard.press('Control+1')
    await page.waitForTimeout(1_200)
    const afterFit = await zoom()
    console.log(`SHORTCUT start=${start} minus=${afterMinus} zero=${afterZero} plus=${afterPlus} fit=${afterFit}`)
    expect(start, `起始 zoom=${start} 不是默认那档——夹具站不住`).toBeCloseTo(0.9, 2)
    expect(afterMinus, `按 - 之后 zoom=${afterMinus}，没缩小`).toBeLessThan(start)
    expect(afterZero, `按 0 之后 zoom=${afterZero}，没回到默认 0.9`).toBeCloseTo(0.9, 2)
    expect(afterPlus, `按 + 之后 zoom=${afterPlus}，没放大`).toBeGreaterThan(afterZero)
    expect(afterFit, `按 1 收拢之后 zoom=${afterFit}，fit 把画布放大了`).toBeLessThanOrEqual(1.001)
    /* 输入框里打字不许动画布：运行态是覆盖面板，先切到那一档 */
    await page.locator('[data-testid="header-run"]').click()
    await page.waitForSelector('[data-testid="editor-panel"] .wf-run', { timeout: 15_000 })
    const reason = page.getByPlaceholder('中止理由（可选）')
    await expect(reason, '右栏没有"中止理由"输入框——这条判据在空跑').toHaveCount(1)
    await reason.click()
    await page.keyboard.press('0')
    await page.waitForTimeout(500)
    const typed = await reason.inputValue()
    const zoomAfterTyping = await zoom()
    console.log(`SHORTCUT typing value=${JSON.stringify(typed)} zoom=${zoomAfterTyping}`)
    expect(typed, '按下去的 0 没进输入框——按键被画布抢走了').toBe('0')
    expect(zoomAfterTyping, `在输入框里按 0 之后 zoom=${zoomAfterTyping}，画布被缩放了`).toBeCloseTo(afterFit, 3)
    await page.screenshot({ path: 'test-results/ui-audit/canvas-shortcuts.png' })
  })
})

/**
 * 沉浸式外壳与 Dify 式编辑交互（2026-10-23 深度改造后）。
 *
 * 应用侧栏/顶栏/页脚在这一路由上**整块不渲染**（`App.vue` 的 `v-if="!immersive"`），
 * 画布从 56px 头部下沿铺到视口底；右栏与块选择器都是覆盖式浮层，打开不许挤压画布宽度。
 * 这几条都是"改回去也照样能跑"的布局事实，必须由机器钉住。
 */
test.describe('沉浸式编辑器外壳 @1440', () => {
  test('无侧栏顶栏、56px 头部、画布满宽、400px 覆盖面板不挤压画布', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.goto('/workflow')
    await page.waitForSelector(READY, { timeout: 45_000 })
    await settle(page)
    await page.waitForTimeout(2_500)
    const shell = await page.evaluate(() => {
      const rect = (sel: string) => document.querySelector(sel)?.getBoundingClientRect() ?? null
      const head = rect('[data-testid="editor-header"]')
      const canvas = rect('[data-testid="workflow-canvas"]')
      const pattern = document.querySelector('.vue-flow__background pattern')
      const pane = document.querySelector('.vue-flow__transformationpane')
      const t = pane ? getComputedStyle(pane).transform : ''
      return {
        sidebar: document.querySelectorAll('[data-testid="sidebar"]').length,
        topbar: document.querySelectorAll('.topbar').length,
        footer: document.querySelectorAll('.footer, [data-testid="assistant-entry"], [data-testid="service-token-bar"]')
          .length,
        headH: head?.height ?? 0,
        canvas: canvas ? { top: canvas.top, bottom: canvas.bottom, width: canvas.width } : null,
        /* 点阵的"14px"是**一个 flow 单位**上的间距：库把 pattern 绑在变换层上（跟着画布一起缩），
           默认档 zoom=0.9 时屏量 12.6px 是对的。所以这里按 zoom 还原回 flow 单位再判。 */
        grid: pattern
          ? {
              w:
                Number(pattern.getAttribute('width')) /
                (t.startsWith('matrix') ? Number(t.slice(t.indexOf('(') + 1).split(',')[0]) : 1),
              h:
                Number(pattern.getAttribute('height')) /
                (t.startsWith('matrix') ? Number(t.slice(t.indexOf('(') + 1).split(',')[0]) : 1),
            }
          : null,
        toolbar: document.querySelectorAll('.ed-toolbar__btn').length,
      }
    })
    console.log(`IMMERSIVE-SHELL ${JSON.stringify(shell)}`)
    expect(shell.sidebar, '沉浸式页还渲染着应用侧栏').toBe(0)
    expect(shell.topbar, '沉浸式页还渲染着应用顶栏').toBe(0)
    expect(shell.footer, '沉浸式页还挂着页脚/令牌条/助手浮标').toBe(0)
    expect(shell.headH, '编辑器头部不是 56px（Dify top-14）').toBeCloseTo(56, 0)
    expect(shell.canvas, '画布没渲染').not.toBeNull()
    if (shell.canvas) {
      expect(shell.canvas.top).toBeCloseTo(shell.headH, 0)
      expect(shell.canvas.bottom).toBeCloseTo(900, 0)
      expect(shell.canvas.width).toBeCloseTo(1440, 0)
    }
    expect(shell.grid, '画布上没有点阵 pattern——判据在空跑').not.toBeNull()
    if (shell.grid) {
      expect(shell.grid.w, `点阵横向间距按 zoom 还原是 ${shell.grid.w}，不是设计钉的 14`).toBeCloseTo(14, 1)
      expect(shell.grid.h, `点阵纵向间距按 zoom 还原是 ${shell.grid.h}，不是设计钉的 14`).toBeCloseTo(14, 1)
    }
    expect(shell.toolbar, '画布工具条五枚按钮没齐（添加/备注/自动布局/适配/指针切换）').toBe(5)

    /* 块选择器：400px 宽、打开时搜索框常驻焦点、Esc 关闭并把焦点还给打开它的按钮。 */
    await page.locator('[data-testid="toolbar-add"]').click()
    await page.waitForSelector('[data-testid="block-selector"]', { timeout: 10_000 })
    const selector = await page.evaluate(() => {
      const el = document.querySelector('[data-testid="block-selector"]') as HTMLElement | null
      const input = document.querySelector('.block-selector__input') as HTMLInputElement | null
      if (!el) return null
      const r = el.getBoundingClientRect()
      return {
        w: Math.round(r.width),
        top: Math.round(r.top),
        bottom: Math.round(r.bottom),
        focused: document.activeElement === input,
        hint: input?.placeholder ?? '',
      }
    })
    expect(selector, '块选择器没打开').not.toBeNull()
    if (selector) {
      expect(selector.w, '块选择器不是 400px（Dify block-selector）').toBe(400)
      expect(selector.top, '块选择器压到编辑器头部上了').toBeGreaterThanOrEqual(56)
      expect(selector.bottom).toBeLessThanOrEqual(900)
      expect(selector.focused, '块选择器打开后搜索框没拿到焦点').toBe(true)
      expect(selector.hint, '搜索框提示词丢了').toBe('搜索节点')
    }
    await page.keyboard.press('Escape')
    await page.waitForTimeout(300)
    await expect(page.locator('[data-testid="block-selector"]'), 'Esc 关不掉块选择器').toHaveCount(0)
    expect(
      await page.evaluate(() => document.activeElement?.getAttribute('data-testid') ?? ''),
      '关闭块选择器后焦点没还给工具条按钮',
    ).toBe('toolbar-add')

    /* 面板是覆盖式：打开后画布宽度不许变，面板贴右缘、从头部下沿起。 */
    await page.locator('[data-testid="toolbar-add"]').click()
    await page.waitForSelector('[data-testid="block-selector"]', { timeout: 10_000 })
    await page.locator('.block-selector__row:not(.is-disabled)').first().click()
    await page.waitForSelector('.wf-node', { timeout: 20_000 })
    await page.waitForTimeout(600)
    const panel = await page.evaluate(() => {
      const p = document.querySelector('[data-testid="editor-panel"]')?.getBoundingClientRect()
      const c = document.querySelector('[data-testid="workflow-canvas"]')?.getBoundingClientRect()
      const node = document.querySelector('.wf-node')?.getBoundingClientRect()
      const pane = document.querySelector('.vue-flow__transformationpane')
      const t = pane ? getComputedStyle(pane).transform : ''
      const zoom = t.startsWith('matrix') ? Number(t.slice(t.indexOf('(') + 1).split(',')[0]) : 1
      return {
        panel: p ? { left: p.left, top: p.top, w: p.width } : null,
        canvasWidth: c?.width ?? 0,
        nodeFlowWidth: node ? node.width / zoom : 0,
      }
    })
    console.log(`IMMERSIVE-PANEL ${JSON.stringify(panel)}`)
    expect(panel.panel, '节点面板没打开').not.toBeNull()
    if (panel.panel) {
      expect(panel.panel.w, '面板不是 400px').toBeCloseTo(400, 0)
      expect(panel.panel.left, '面板没贴右缘').toBeCloseTo(1040, 0)
      expect(panel.panel.top, '面板没从头部下沿开始').toBeCloseTo(56, 0)
    }
    expect(panel.canvasWidth, `面板把画布挤窄了（${panel.canvasWidth}px）`).toBeCloseTo(1440, 0)
    expect(panel.nodeFlowWidth, '节点卡不是 240 flow 像素宽').toBeCloseTo(240, 0)
    await page.locator('[data-testid="panel-close"]').click()
    await page.waitForTimeout(300)
    await expect(page.locator('[data-testid="editor-panel"]')).toHaveCount(0)
    expect(await page.evaluate(() => window.innerWidth), '关面板后视口宽度变了').toBe(1440)
  })
})

/**
 * 真机验收：搜索添加 → 拖线连接 → 保存 → F5。
 *
 * 这条走的是完整用户动作，不灌任何 fixture：块选择器里搜"事件通报"、从开始节点拖到它、
 * 把第二颗拖到一个非默认位置、点保存，然后 F5 读回**同一组 flow 坐标**。
 * 光断言"页面上还有两个 `.wf-node`"不够——F5 之后坐标若被自动布局重算，用户摆的版面就丢了。
 */
test.describe('编排主流程 @1440', () => {
  test('搜索添加 → 连线 → 保存 → F5：节点、连线与坐标都还在', async ({ page }) => {
    test.setTimeout(150_000)
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.goto('/workflow')
    await page.waitForSelector(READY, { timeout: 45_000 })
    await settle(page)
    await page.waitForTimeout(2_500)

    const addBlock = async (query: string): Promise<void> => {
      await page.locator('[data-testid="toolbar-add"]').click()
      await page.waitForSelector('[data-testid="block-selector"]', { timeout: 10_000 })
      await page.getByPlaceholder('搜索节点').fill(query)
      await page.waitForTimeout(250)
      const rows = page.locator('.block-selector__row')
      await expect(rows, `搜 "${query}" 一个可选块都没有——判据在空跑`).not.toHaveCount(0)
      await rows.first().click()
      await page.waitForTimeout(700)
    }
    await addBlock('开始')
    await addBlock('事件通报')
    await expect(page.locator('.wf-node'), '两次添加后画布上不是两颗节点').toHaveCount(2)
    await page.locator('[data-testid="panel-close"]').click()
    await page.waitForTimeout(500)

    const startNode = page.locator('.wf-node', { hasText: '开始' }).first()
    const notifyNode = page.locator('.wf-node', { hasText: '事件通报' }).first()
    const source = await startNode.locator('.vue-flow__handle.source').boundingBox()
    const target = await notifyNode.locator('.vue-flow__handle.target').boundingBox()
    expect(source, '开始节点没有出线圆点').not.toBeNull()
    expect(target, '事件通报节点没有入线圆点').not.toBeNull()
    if (source && target) {
      await page.mouse.move(source.x + source.width / 2, source.y + source.height / 2)
      await page.mouse.down()
      await page.mouse.move(target.x + target.width / 2, target.y + target.height / 2, { steps: 10 })
      await page.mouse.up()
      await page.waitForTimeout(800)
    }
    await expect(page.locator('.vue-flow__edge-path'), '拖线后没有生成连线').toHaveCount(1)

    /* 把第二颗挪到一个非默认位置：F5 之后要能原样读回这一组坐标。
       抓手用的是卡片抬头那条（top+14）——正中间会落在参数芯片上，芯片自己吞掉指针事件，
       真机量到按中心拖会把这次拖动变成"点开这颗节点"。 */
    const box = await notifyNode.boundingBox()
    expect(box, '事件通报那颗量不到盒子').not.toBeNull()
    if (box) {
      await page.mouse.move(box.x + box.width / 2, box.y + 14)
      await page.mouse.down()
      await page.mouse.move(box.x + box.width / 2 + 140, box.y + 14 + 40, { steps: 12 })
      await page.mouse.up()
      await page.waitForTimeout(900)
    }
    const readPositions = (): Promise<Array<{ id: string; x: number; y: number }>> =>
      page.evaluate(() =>
        [...document.querySelectorAll('.vue-flow__node')]
          .map((node) => {
            const match = /translate\(([-\d.]+)px,\s*([-\d.]+)px\)/.exec(node.getAttribute('style') ?? '')
            return {
              id: node.getAttribute('data-id') ?? '',
              x: match ? Number(match[1]) : Number.NaN,
              y: match ? Number(match[2]) : Number.NaN,
            }
          })
          .filter((row) => row.id !== ''),
      )
    const before = await readPositions()
    expect(before, '画布上读不到两颗节点的坐标').toHaveLength(2)
    expect(new Set(before.map((row) => row.id)).size).toBe(2)

    await page
      .locator('.ed-head__right button')
      .filter({ hasText: /^保存$/ })
      .click()
    await expect(page.locator('.ed-head__save'), '保存后头部没有回到"已保存"').toContainText('已保存', {
      timeout: 20_000,
    })
    await expect.poll(() => new URL(page.url()).searchParams.get('lineage') ?? '', { timeout: 15_000 }).not.toBe('')

    await page.reload()
    await page.waitForSelector(READY, { timeout: 45_000 })
    await page.waitForFunction(() => document.querySelectorAll('.wf-node').length === 2, null, { timeout: 30_000 })
    await page.waitForTimeout(2_000)
    const after = await readPositions()
    console.log(`FLOW-ROUNDTRIP before=${JSON.stringify(before)} after=${JSON.stringify(after)} url=${page.url()}`)
    expect(after, 'F5 之后节点数变了').toHaveLength(2)
    expect(new Set(after.map((row) => row.id))).toEqual(new Set(before.map((row) => row.id)))
    for (const row of before) {
      const restored = after.find((item) => item.id === row.id)
      expect(restored, `F5 之后节点 ${row.id} 不见了`).toBeDefined()
      expect(restored?.x, `节点 ${row.id} 的 x 坐标在 F5 后变了（${row.x} → ${restored?.x}）`).toBeCloseTo(row.x, 0)
      expect(restored?.y, `节点 ${row.id} 的 y 坐标在 F5 后变了（${row.y} → ${restored?.y}）`).toBeCloseTo(row.y, 0)
    }
    await expect(page.locator('.vue-flow__edge-path'), 'F5 之后连线丢了').toHaveCount(1)
    await page.screenshot({ path: 'test-results/ui-audit/canvas-roundtrip.png' })
  })
})

/**
 * 边中点 “+” 插入（批次 B 交付的第三条动线）。
 *
 * 这条动线在 Dify 里是改链路最常用的动作之一：链路已经连好了，才发现中间少一步。
 * 插入的语义有两处容易做错，都得在真机上钉住：
 * ① **条件留在进新节点的那一段**（分支语义属于原上游），新节点出边是无条件的；
 * ② 边被拆成两段之后，画布与定义面板里看到的必须是同一条链路（不是"线断了但图没变"）。
 */
test.describe('边中点插入 @1440', () => {
  test('连线中点的 “+” 把块插进这一段，条件留在进来那条上', async ({ page }) => {
    test.setTimeout(150_000)
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.goto('/workflow')
    await page.waitForSelector(READY, { timeout: 45_000 })
    await settle(page)
    await page.waitForTimeout(2_000)

    const addBlock = async (query: string): Promise<void> => {
      await page.locator('[data-testid="toolbar-add"]').click()
      await page.waitForSelector('[data-testid="block-selector"]', { timeout: 10_000 })
      await page.getByPlaceholder('搜索节点').fill(query)
      await page.waitForTimeout(250)
      const rows = page.locator('.block-selector__row')
      await expect(rows, `搜 "${query}" 一个可选块都没有——判据在空跑`).not.toHaveCount(0)
      await rows.first().click()
      await page.waitForTimeout(700)
      if (await page.locator('[data-testid="panel-close"]').count()) {
        await page.locator('[data-testid="panel-close"]').click()
        await page.waitForTimeout(300)
      }
    }
    await addBlock('开始')
    await addBlock('事件通报')
    await expect(page.locator('.wf-node'), '两颗节点没加上').toHaveCount(2)

    const handles = await page.evaluate(() =>
      [...document.querySelectorAll('.vue-flow__node')].flatMap((n) =>
        [...n.querySelectorAll('.vue-flow__handle')].map((h) => {
          const r = h.getBoundingClientRect()
          return {
            id: n.getAttribute('data-id'),
            source: h.className.includes('source'),
            x: r.left + r.width / 2,
            y: r.top + r.height / 2,
          }
        }),
      ),
    )
    const from = handles.find((h) => h.source && h.id === 'start_1')
    const to = handles.find((h) => !h.source && h.id === 'notify_1')
    expect(from, '开始节点没有出线圆点').toBeDefined()
    expect(to, '事件通报节点没有入线圆点').toBeDefined()
    if (from && to) {
      await page.mouse.move(from.x, from.y)
      await page.mouse.down()
      await page.mouse.move((from.x + to.x) / 2, (from.y + to.y) / 2, { steps: 6 })
      await page.mouse.move(to.x, to.y, { steps: 6 })
      await page.mouse.up()
      await page.waitForTimeout(900)
    }
    await expect(page.locator('.vue-flow__edge-path'), '拖线后没有生成连线').toHaveCount(1)

    /* 先给这条边写一个分支名：条件是中点插入最容易弄丢的语义。 */
    await page.locator('[data-testid="header-def"]').click()
    await page.waitForSelector('[data-testid="editor-panel"] .wf-def', { timeout: 15_000 })
    const condition = page.getByPlaceholder('边条件（分支名）')
    await expect(condition, '定义面板里没有边条件输入框——这条判据在空跑').toHaveCount(1)
    await condition.fill('high')
    await condition.dispatchEvent('change')
    await page.waitForTimeout(600)
    await page.locator('[data-testid="panel-close"]').click()
    await page.waitForTimeout(600)

    /* 中点 “+” → 块选择器 → 选一块事件通报。
       先钉"点得到"：库里 `.vue-flow__edge-labels` 是 `z-index: auto`，而每棵节点带内联 z-index
       （被选中/拖动那棵是 1000）——真机量到这颗 20px 的钮落在上一张卡"超时 5000 ms"那行字下面，
       hit-test 命中的是节点子树，"在中间插一步"在默认布局下根本按不到。 */
    const insert = page.locator('[data-testid^="edge-insert-"]')
    await expect(insert, '连线中点没有插入按钮——B 批交付的这条动线不在位').toHaveCount(1)
    const clickable = await page.evaluate(() => {
      const btn = document.querySelector('.wf-edge__insert')
      if (!btn) return { found: false, hit: '没有按钮' }
      const r = btn.getBoundingClientRect()
      const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)
      return {
        found: true,
        hit:
          top === btn
            ? 'button'
            : `${top?.tagName.toLowerCase() ?? 'null'}.${String(top?.className ?? '').split(/\s+/)[0]}`,
      }
    })
    expect(clickable.found, '读不到中点按钮').toBe(true)
    expect(clickable.hit, `中点 “+” 被盖住了，hit-test 命中的是 ${clickable.hit}`).toBe('button')
    await insert.click()
    await page.waitForSelector('[data-testid="block-selector"]', { timeout: 10_000 })
    await page.getByPlaceholder('搜索节点').fill('事件通报')
    await page.waitForTimeout(250)
    await page.locator('.block-selector__row').first().click()
    await page.waitForTimeout(900)

    const shape = await page.evaluate(() => ({
      nodes: [...document.querySelectorAll('.wf-node .wf-node__title')].map((el) => (el.textContent ?? '').trim()),
      edges: document.querySelectorAll('.vue-flow__edge-path').length,
      /* 只有非空条件会渲染成标签：一条 'high' 说明条件留在了进来那一段，出去那段是无条件的。 */
      conditions: [...document.querySelectorAll('.wf-edge__condition')].map((el) => (el.textContent ?? '').trim()),
    }))
    console.log(`MIDPOINT ${JSON.stringify(shape)}`)
    expect(shape.nodes, `插入后画布上不是三颗节点：${JSON.stringify(shape.nodes)}`).toHaveLength(3)
    expect(shape.edges, `插入后连线不是两段：${shape.edges}`).toBe(2)
    expect(shape.conditions, `条件没留在进新节点那一段：${JSON.stringify(shape.conditions)}`).toEqual(['high'])

    /* 定义面板里那条链路必须是 start_1 → 新节点 → notify_1，不是"看起来断了"。 */
    await page.locator('[data-testid="header-def"]').click()
    await page.waitForSelector('[data-testid="editor-panel"] .wf-def', { timeout: 15_000 })
    const rows = await page.evaluate(() =>
      [...document.querySelectorAll('.wf-def__edge')].map((row) =>
        [...row.querySelectorAll('.wf-def__edge-endpoints span')]
          .map((s) => (s.textContent ?? '').trim())
          .filter((t) => t !== '→'),
      ),
    )
    console.log(`MIDPOINT-EDGES ${JSON.stringify(rows)}`)
    expect(rows, `定义面板里的链路不是拆开的两段：${JSON.stringify(rows)}`).toEqual([
      ['start_1', 'notify_2'],
      ['notify_2', 'notify_1'],
    ])
    await page.screenshot({ path: 'test-results/ui-audit/canvas-midpoint.png' })
  })
})

/**
 * 提示条（antd message）落点：库里默认 top:8px，而顶栏定高 68px——报错提示正好糊在顶栏上。
 * 真机截图实测过一条 500 提示把步进器第 7 档和右侧状态胶囊整个盖住，而那两处是
 * "我在哪一步 / 后端通不通"的唯一入口。重叠判据看不见它（`.ant-message` 在 skip 名单里，
 * 弹层本来就该浮在正文上），所以单独钉一条"不许盖住顶栏"。
 */
test.describe('提示条落点 @1440', () => {
  test('报错提示落在外壳下方，不吃掉编辑器头部', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.route('**/api/**', (route) =>
      route.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"probe boom"}' }),
    )
    await page.goto('/workflow')
    await page.waitForSelector(READY, { timeout: 45_000 })
    await page.waitForTimeout(1_500)
    await page.getByRole('button', { name: '刷新实例' }).click()
    await page.waitForSelector('.ant-message-notice', { timeout: 15_000 })
    const m = await page.evaluate(() => {
      const box = document.querySelector('.ant-message')?.getBoundingClientRect()
      /* 沉浸式页没有应用顶栏，等价的外壳上沿是 56px 编辑器头部。 */
      const nav = document.querySelector('.topbar, [data-testid="editor-header"]')?.getBoundingClientRect()
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
      expect(
        m.noticeTop,
        `提示条压在顶栏上（提示顶边=${m.noticeTop}，顶栏底边=${m.navBottom}）`,
      ).toBeGreaterThanOrEqual(m.navBottom)
    }
  })
})

/**
 * 页头状态位（PageHero 的 `status`）：那句"现在怎么样了"是页头最贵的一行字，
 * 圆点必须真的按 tone 变色（不是所有档都画同一个灰点），文字必须读得清。
 * 目前只有编排页与预警页给了 status——断言按页来，别拿"每页都得有"当判据。
 */
test.describe('页头状态位 @1440', () => {
  test('warnings 的状态语有彩色圆点且字读得清', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.goto('/warnings')
    await page.waitForSelector(READY, { timeout: 45_000 })
    await page.waitForTimeout(3_000)
    const m = await page.evaluate(() => {
      const el = document.querySelector('.page-head__status')
      const dot = el?.querySelector('.page-head__status-dot') as HTMLElement | null
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
    console.log(`HEROSTATUS warnings cls=${m.cls} dot=${m.dotBg} ${m.dotW}x${m.dotH} text="${m.text}"`)
    expect(m.text, 'warnings：页头没有状态语——这条判据在空跑').not.toBe('')
    expect(m.cls, 'warnings：状态语没带 tone 类（is-ok/is-warn/is-bad/is-idle）').not.toBe('')
    expect(m.dotW, 'warnings：圆点宽 0').toBeGreaterThanOrEqual(6)
    expect(m.dotH, 'warnings：圆点高 0').toBeGreaterThanOrEqual(6)
    /* tone 类要真的落到颜色上：除 idle 外，圆点不许还是那个 50% 白的默认底 */
    if (!m.cls.includes('is-idle')) {
      expect(m.dotBg, `warnings：${m.cls} 的圆点还是默认灰（tone 类没起作用）`).not.toBe('rgba(255, 255, 255, 0.5)')
      expect(m.dotBg).not.toBe('rgb(255, 255, 255)')
    }
    expect(m.fontSize, 'warnings：状态语字号').toBeGreaterThanOrEqual(12)
  })

  /* 沉浸式编辑器把"现在怎么样了"收进头部保存态：同一条语义要换到这里继续钉住。 */
  test('workflow 的保存态有彩色圆点且字读得清', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.goto('/workflow')
    await page.waitForSelector(READY, { timeout: 45_000 })
    await page.waitForTimeout(3_000)
    const m = await page.evaluate(() => {
      const el = document.querySelector('.ed-head__save')
      const dot = el?.querySelector('.ed-head__save-dot') as HTMLElement | null
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
    console.log(`HEROSTATUS workflow cls=${m.cls} dot=${m.dotBg} ${m.dotW}x${m.dotH} text="${m.text}"`)
    expect(m.text, 'workflow：头部没有保存态——这条判据在空跑').not.toBe('')
    expect(m.cls, 'workflow：保存态没带 tone 类（is-ok/is-warn/is-bad/is-idle）').not.toBe('')
    expect(m.dotW, 'workflow：圆点宽 0').toBeGreaterThanOrEqual(6)
    expect(m.dotH, 'workflow：圆点高 0').toBeGreaterThanOrEqual(6)
    expect(m.dotBg, 'workflow：保存态的圆点还是默认灰（tone 类没起作用）').not.toBe('rgb(98, 107, 125)')
    expect(m.fontSize, 'workflow：保存态字号').toBeGreaterThanOrEqual(11)
  })
})

/**
 * 新鲜度条（FreshnessBar）：左端口径、右端"取数 HH:MM:SS ｜ N 秒前"，中间一道线。
 * 判据盯三件：两端都得有字（少一端就是这条判据在空跑）、两端不许压在一起、
 * 右端必须真的报出"多久之前"——只写绝对时间戳的话，页面挂两小时也看不出来。
 */
test.describe('新鲜度条 @1440', () => {
  /* workflow 不再在页面上挂新鲜度条：它随「定义 / 运行态」并进了 400px 覆盖面板，
     由下面的「定义列表自查」与 RuntimePanel 组件用例覆盖（那里量的是同一根条）。 */
  for (const [name, path] of [
    ['warnings', '/warnings'],
    ['monitor', '/monitor'],
    ['metrics', '/metrics'],
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
            return el && el.scrollWidth > el.clientWidth + 1
              ? `${sel} 需要 ${el.scrollWidth}px 只有 ${el.clientWidth}px`
              : ''
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
      console.log(
        `FRESH ${name} left="${m.left}" right="${m.right}" gap=${m.gap} ruleH=${m.ruleH} stacked=${m.stacked} cut=${JSON.stringify(m.cut)}`,
      )
      await page.screenshot({
        path: `test-results/ui-audit/freshness-${name}.png`,
        clip: { x: 0, y: 190, width: 1440, height: 140 },
      })
      expect(m.left, `${name}：左端口径是空的`).not.toBe('')
      expect(m.right, `${name}：右端没报新鲜度——判据在空跑`).not.toBe('')
      expect(m.right, `${name}：右端要同时报"取数时刻"和"多久之前"，实测：${m.right}`).toMatch(
        /取数 .*｜ .*(秒前|分前|比预期周期慢)|尚未取到数据/,
      )
      expect(
        m.cut,
        `${name}：有一端被省略号截掉了（这条说的就是"数据多新"，截掉就等于没说）：\n${m.cut.join('\n')}`,
      ).toHaveLength(0)
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
    await page.waitForSelector(READY, { timeout: 45_000 })
    await settle(page)
    await page.waitForTimeout(2_500)
    /* 运行态从 2026-10-23 起是覆盖面板，默认关闭（默认页签存在 localStorage）；先打开它。 */
    await page.locator('[data-testid="header-run"]').click()
    await page.waitForSelector('[data-testid="editor-panel"] .wf-run', { timeout: 15_000 })
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
        axisW: rows.map((r) =>
          Math.round((r.querySelector('.wf-tl__axis') as HTMLElement).getBoundingClientRect().width),
        ),
        lines: document.querySelectorAll('.wf-tl__line').length,
        named: [...document.querySelectorAll('.wf-tl__name')].map((el) => ({
          name: (el.textContent ?? '').trim(),
          id: el.getAttribute('title') ?? '',
        })),
      }
    })
    console.log(
      `TIMELINE-real rows=${real.rows} axis=${JSON.stringify(real.axisW)} lines=${real.lines} named=${JSON.stringify(real.named.slice(0, 2))}`,
    )
    expect(real.rows, '时间线一行都没有').toBeGreaterThan(0)
    expect(new Set(real.axisW).size, `轴宽不统一：${JSON.stringify(real.axisW)}`).toBe(1)
    expect(real.axisW[0], '轴列宽必须是 24px（连接线要靠它定位）').toBe(24)
    expect(real.lines, '最后一行不该再画一截悬空的轴').toBe(real.rows - 1)
    expect(
      real.named.some((row) => row.name !== '' && row.name !== row.id),
      '节点名一律退回 ID——没回定义里取名字',
    ).toBe(true)
    /* 第二段灌一份实例详情：本机种子数据里的实例都跑完了（真机试过，前六条一条"未开始"的行都没有），
       那样 dashed 那一档根本没被验到。五种状态各一行，四档底色与错误行都能确定性地量。
       走 route 而不是真启动一条实例——启动会往台账里落真数据（探针消耗现场这条坑记在台账里）。
       两件事必须对着真后端：`workflow_id` 要过编解码层的长度下限，而且是**真存在的那一版**——
       实例详情落地后 store 还会取那一版文档铺画布，编一个 id 会让整次 focus 失败、时间线停在旧数据上
       （2026-10-23 就是这么红的：报文里写了 `wf_probe`，看着像判据在空跑，其实是被解码层拒了）。 */
    if (gateInstance === null) throw new Error('ensureGateRows 没记下门禁实例的身份——后面的桩没法造')
    const { workflowId } = gateInstance
    await page.route(/\/api\/v1\/workflow\/instances\/[^/?]+$/, (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: probeInstanceBody(instanceIdOf(route.request().url()), workflowId, [
          probeInstanceNode('gate', 'succeeded', { duration_ms: 1_250 }),
          probeInstanceNode('notify_ok', 'failed', { error: '上游 502：网关无响应', attempts: 3 }),
          probeInstanceNode('review', 'awaiting_human'),
          probeInstanceNode('archive', 'pending'),
          probeInstanceNode('report', 'ready'),
        ]),
      }),
    )
    await picks.first().click()
    /* 这里的等待必须等**桩里独有的那一行**（archive），不是"时间线里有行"：
       上一版等的是 `.wf-tl__step` 存在——第一段真数据留下的 5 行本来就在那儿，于是
       立刻放行、todo 计数读到的是旧的五行，红的其实是这条判据自己的等待方式（12.9s 就"超时"在 0 上）。 */
    await page.waitForFunction(
      () => [...document.querySelectorAll('.wf-tl__name')].some((el) => el.getAttribute('title') === 'archive'),
      null,
      { timeout: 20_000 },
    )
    const sawTodo = await page.locator('.wf-tl__step.is-todo').count()
    await page.waitForTimeout(900)
    const m = await page.evaluate(() => {
      const rows = [...document.querySelectorAll('.wf-tl__step')]
      const axisW = rows.map((r) =>
        Math.round((r.querySelector('.wf-tl__axis') as HTMLElement).getBoundingClientRect().width),
      )
      const offAxis: string[] = []
      for (const r of rows) {
        const dot = r.querySelector('.wf-tl__dot')?.getBoundingClientRect()
        const line = r.querySelector('.wf-tl__line')?.getBoundingClientRect()
        if (!dot || !line) continue
        const dotCx = dot.left + dot.width / 2
        const lineCx = line.left + line.width / 2
        if (Math.abs(dotCx - lineCx) > 1)
          offAxis.push(`第 ${rows.indexOf(r) + 1} 行点与线偏心 ${Math.round(dotCx - lineCx)}px`)
        if (dot.width < 8) offAxis.push(`第 ${rows.indexOf(r) + 1} 行圆点只有 ${Math.round(dot.width)}px`)
      }
      const dashed: string[] = []
      for (const r of rows) {
        const cs = getComputedStyle(r)
        const todo = r.classList.contains('is-todo')
        if (todo && cs.borderTopStyle !== 'dashed')
          dashed.push(`第 ${rows.indexOf(r) + 1} 行是待办档却画成 ${cs.borderTopStyle}`)
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
    console.log(
      `TIMELINE rows=${m.rows} axis=${JSON.stringify(m.axisW)} lines=${m.lines} named=${JSON.stringify(m.named.slice(0, 2))} offAxis=${m.offAxis.length} dashed=${m.dashed.length} todoRows=${sawTodo}`,
    )
    await page.screenshot({
      path: 'test-results/ui-audit/run-timeline.png',
      clip: { x: 1040, y: 190, width: 400, height: 700 },
    })
    expect(m.rows, '时间线一行都没有').toBeGreaterThan(0)
    expect(new Set(m.axisW).size, `轴宽不统一：${JSON.stringify(m.axisW)}`).toBe(1)
    expect(m.axisW[0], '轴列宽必须是 24px（连接线要靠它定位）').toBe(24)
    expect(m.offAxis, m.offAxis.join('\n')).toHaveLength(0)
    expect(m.dashed, m.dashed.join('\n')).toHaveLength(0)
    expect(m.lines, '最后一行不该再画一截悬空的轴').toBe(m.rows - 1)
    /* 空判防护：这一段灌的是"还有一步没走"的详情，dashed 那一档必须真被验到 */
    expect(sawTodo, '前六条实例都没有"未开始"的一行——dashed 那档没被验到').toBeGreaterThan(0)
    /* 灌进去的五种状态要各归各的档：succeeded→done、failed→bad、awaiting_human→active、pending/ready→todo */
    expect(m.tones, `五行的底色档不对：${JSON.stringify(m.tones)}`).toEqual([
      'is-done',
      'is-bad',
      'is-active',
      'is-todo',
      'is-todo',
    ])
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
    await page.waitForSelector(READY, { timeout: 45_000 })
    await settle(page)
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
        hitsPager:
          !!b &&
          !!pag &&
          Math.min(b.right, pag.right) - Math.max(b.left, pag.left) > 4 &&
          Math.min(b.bottom, pag.bottom) - Math.max(b.top, pag.top) > 4,
        rows,
        empty,
      }
    })
    const r = await page.evaluate(inspect, { contrastAllow: CONTRAST_ALLOW, scope: '.sys-term' })
    console.log(
      `SYSTERM-open h=${geo.h} top=${geo.top} bottom=${geo.bottom} ih=${geo.ih} right=${geo.right} iw=${geo.iw} scrollY=${geo.scrollY} bodyH=${geo.bodyH} inView=${geo.inView} hitsPager=${geo.hitsPager} rows=${geo.rows} empty=${geo.empty} leaves=${r.leafCount} overlap=${r.overlapCount} low=${r.lowContrastCount}`,
    )
    await page.screenshot({
      path: 'test-results/ui-audit/sys-term-open.png',
      clip: { x: 640, y: 500, width: 800, height: 400 },
    })
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
  test('Tab 走全部页面：每个焦点都有可见指示且不被裁到看不见', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    const problems: string[] = []
    let brandRings = 0
    for (const [, path] of PAGES) {
      await page.goto(path)
      await page.waitForSelector(READY, { timeout: 45_000 })
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
        if (!info.ring)
          problems.push(`${path} 第${i + 1}次 Tab：<${info.tag}.${info.cls}> "${info.text}" 没有可见焦点指示`)
        if (!info.inView)
          problems.push(`${path} 第${i + 1}次 Tab：<${info.tag}.${info.cls}> "${info.text}" 焦点落在视口外`)
      }
    }
    console.log(`FOCUS problems=${problems.length} brandRings=${brandRings}`)
    expect(problems, `键盘焦点态问题：\n${problems.slice(0, 12).join('\n')}`).toHaveLength(0)
    expect(brandRings, '70 次 Tab 里一处主色焦点环都没有——`:focus-visible` 那条规则可能整块失效了').toBeGreaterThan(0)
  })
})

/**
 * 「服务端已存定义」那一列的行 + 全站入场动效 @1440。
 *
 * 定义行原先是 `flex-wrap: wrap`：322px 的右栏装不下"名字 + 计数 + 状态 + 两颗按钮"，
 * 折到哪一行没人管——真机量到两颗按钮落在**不同行的不同位置**
 * （"打开"右缘 1384、"归档"右缘 1150）。改两行网格之后行高统一、按钮右缘对齐成一列。
 * 动效那条钉的是移植 NexusMind 的 `fadeIn .2s ease-out + translateY(5px)`，
 * 以及它全站都没有、我们自己补的 `prefers-reduced-motion` 兜底。
 */
test.describe('定义行网格与入场动效 @1440', () => {
  test('每一行两颗按钮右缘对齐、行高统一，不靠意外折行', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.goto('/workflow')
    await page.waitForSelector(READY, { timeout: 45_000 })
    await settle(page)
    await page.waitForTimeout(3_000)
    await page.locator('[data-testid="header-def"]').click()
    await page.waitForSelector('[data-testid="editor-panel"] .wf-def', { timeout: 15_000 })
    await page.waitForTimeout(2_000)
    const m = await page.evaluate(() => {
      const rows = [...document.querySelectorAll('.wf-def__list-item')]
      const rail = document.querySelector('.wf-def')?.getBoundingClientRect()
      const detail = rows.map((row) => {
        const r = row.getBoundingClientRect()
        const btn = (sel: string) => row.querySelector(sel)?.getBoundingClientRect() ?? null
        const open = btn('.wf-def__button--open')
        const archive = btn('.wf-def__button--archive')
        /* 这一列要点了"定义"档才渲染，页面级那 42 项看不到它 ⇒ 行内子元素自己两两求交 */
        const kids = [...row.children].map((k) => k.getBoundingClientRect()).filter((k) => k.width > 1 && k.height > 1)
        let inner = 0
        for (let i = 0; i < kids.length; i += 1) {
          for (let j = i + 1; j < kids.length; j += 1) {
            const ox = Math.min(kids[i].right, kids[j].right) - Math.max(kids[i].left, kids[j].left)
            const oy = Math.min(kids[i].bottom, kids[j].bottom) - Math.max(kids[i].top, kids[j].top)
            if (ox > 2 && oy > 2) inner += 1
          }
        }
        return {
          h: Math.round(r.height),
          inside: !!rail && r.left >= rail.left - 1 && r.right <= rail.right + 1,
          openRight: open ? Math.round(open.right) : -1,
          archiveRight: archive ? Math.round(archive.right) : -1,
          openTop: open ? Math.round(open.top) : -1,
          archiveTop: archive ? Math.round(archive.top) : -1,
          ver: (row.querySelector('.wf-def__ver')?.textContent ?? '').trim(),
          inner,
        }
      })
      return { count: rows.length, detail }
    })
    expect(m.count, '定义列表不足 3 行——判据在空跑').toBeGreaterThanOrEqual(3)
    for (const [index, row] of m.detail.entries()) {
      const at = `第 ${index + 1} 行`
      expect(row.inside, `${at} 撑出右栏之外`).toBe(true)
      expect(row.h, `${at} 行高 ${row.h}px，比"两行网格"该有的还高（说明还在乱折）`).toBeLessThanOrEqual(60)
      expect(row.openRight, `${at}「打开」右缘`).toBe(row.archiveRight)
      expect(row.archiveTop, `${at}「归档」应与「打开」分行（第一行放主操作）`).toBeGreaterThan(row.openTop)
      expect(row.inner, `${at} 行内子元素压叠 ${row.inner} 处`).toBe(0)
      /* 版本号单独一格：名字再长也只截名字，"v几"必须看得见（截了就没人知道自己开的是哪版） */
      expect(row.ver, `${at} 版本号不见了`).toMatch(/^v\d+$/)
    }
    await page.screenshot({ path: 'test-results/ui-audit/def-rows-grid.png' })
  })

  test('卡片有入场淡入上浮；开了"减少动效"就不该动', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.goto('/dashboard')
    await page.waitForSelector(READY, { timeout: 45_000 })
    const animated = await page.evaluate(() => {
      const card = document.querySelector('.ant-card')
      const cs = card ? getComputedStyle(card) : null
      return { name: cs?.animationName ?? '', duration: cs?.animationDuration ?? '' }
    })
    console.log(`MOTION normal animation=${JSON.stringify(animated)}`)
    expect(animated.name, '卡片没有入场动效——NexusMind 那条 fadeIn 没接上').toContain('aegis-rise-in')
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await page.waitForTimeout(200)
    const calm = await page.evaluate(() => {
      const card = document.querySelector('.ant-card')
      const cs = card ? getComputedStyle(card) : null
      return { name: cs?.animationName ?? '', fill: cs?.animationFillMode ?? '', transform: cs?.transform ?? '' }
    })
    console.log(`MOTION reduced=${JSON.stringify(calm)}`)
    expect(calm.name, '开了 prefers-reduced-motion 还在放动画').toBe('none')
    /* `backwards` 而不是 `both`：both 会把最后一帧的 transform 永久留下，
       那会让卡片一直充当 fixed 后代的包含块。 */
    expect(calm.fill, 'fill-mode 不该是 both（会在元素上永久留下 transform）').not.toBe('both')
  })
})

/**
 * 风险等级芯片（兼图例）@1440。
 * 一排按钮同时干两件事：不点也是"每级什么色"的图例，点下去是"只看这一级"的筛选。
 * 最贵的两种退化是**图例说谎**（芯片色与图上色不是同一份）与**点了没反应**，两样都在这条里量。
 */
test.describe('风险芯片兼图例 @1440', () => {
  test('五枚芯片取自 RISK_COLORS，点下去按下态与"筛后 / 全量"都跟着变', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.goto('/map')
    await page.waitForSelector(READY, { timeout: 45_000 })
    await settle(page)
    await page.waitForTimeout(12_000)
    const chips = page.locator('.risk-chip')
    expect(await chips.count(), '一张图上一个风险芯片都没有——判据在空跑').toBe(5)
    const seen = await page.evaluate(() =>
      [...document.querySelectorAll('.risk-chip')].map((el) => ({
        chip: (el.getAttribute('style') ?? '').match(/--chip:\s*([^;]+)/)?.[1]?.trim() ?? '',
        line: (el.getAttribute('style') ?? '').match(/--chip-line:\s*([^;]+)/)?.[1]?.trim() ?? '',
        label: el.querySelector('.risk-chip__label')?.textContent?.trim() ?? '',
        pressed: el.getAttribute('aria-pressed'),
      })),
    )
    /** 与 `src/api/types.ts:33-39` 的 `RISK_COLORS` 对齐——图上画的就是这一份。 */
    const LEGEND = ['#cf1322', '#fa8c16', '#fadb14', '#1677ff', '#8c8c8c']
    expect(
      seen.map((entry) => entry.chip),
      '芯片颜色与 RISK_COLORS 不一致（图例说谎）',
    ).toEqual(LEGEND)
    for (const entry of seen) {
      expect(entry.line, `芯片"${entry.label}"未选中时没有该级描边——不点就当不了图例`).toMatch(/^rgba\(/)
      expect(entry.pressed, '未筛选时不该有芯片是按下态').toBe('false')
    }
    await chips.first().click()
    await page.waitForTimeout(700)
    const after = await page.evaluate(() => ({
      pressed: [...document.querySelectorAll('.risk-chip')].map((el) => el.getAttribute('aria-pressed')),
      counts: [...document.querySelectorAll('.row .count')].map((el) => (el.textContent ?? '').trim()),
      note: document.body.innerText.includes('已按风险等级筛选'),
    }))
    console.log(
      `RISKCHIP after-click pressed=${JSON.stringify(after.pressed)} counts=${JSON.stringify(after.counts)} note=${after.note}`,
    )
    expect(after.pressed[0], '点第一枚没变成按下态——筛选没接上').toBe('true')
    expect(after.note, '筛选生效却没有"数字为什么变小"的说明').toBe(true)
    expect(after.counts.filter((text) => text.includes('/')).length, '图层计数没写成"筛后 / 全量"').toBeGreaterThan(0)
    await page.screenshot({ path: 'test-results/ui-audit/map-risk-chips.png' })
    await chips.first().click()
    await page.waitForTimeout(700)
    const back = await page.evaluate(() => ({
      pressed: [...document.querySelectorAll('.risk-chip')].map((el) => el.getAttribute('aria-pressed')),
      slash: [...document.querySelectorAll('.row .count')].some((el) => (el.textContent ?? '').includes('/')),
    }))
    expect(
      back.pressed.every((value) => value === 'false'),
      '再点一次没退回未筛选',
    ).toBe(true)
    expect(back.slash, '退回未筛选之后计数还带着斜杠').toBe(false)
  })
})

/**
 * 悬停态体检 @1440。
 *
 * 此前 89 项判据全程不悬停，而我们移植的 NexusMind 习语有一半是 hover 习语：
 * 行悬停上主色雾、pill `translateY(-1px)` 微升、卡片抬投影、芯片与按钮描边变色。
 * **改底色就可能改对比度，改位移就可能压到邻居**——这两类缺陷一直没被看过。
 * 基线（不悬停）那四项在常驻判据里已经是 0，所以这里任何命中都只能是悬停造成的。
 */
const HOVER_TARGETS = [
  '.side-item',
  '.theme-switch__btn',
  '.page-head button',
  '.ant-list-item',
  '.wf-def__list-item',
  '.wf-run__pick',
  '.ed-head__btn',
  '.ed-toolbar__btn',
  '.risk-chip',
  '.sys-term__chip',
  '.ant-card-hoverable',
  '.side-assistant',
  '.side-collapse',
]

/**
 * 悬停扫描：逐页把可悬停元素逐个 hover，每次悬停后跑同一套判据。
 *
 * `perPage` 是预算：一次全页体检要 1~3 秒，七页 ×N 直接决定这条判据跑多久
 * （门禁是给人反复跑的，慢到没人愿意跑就等于没有）。
 */
async function hoverSweep(page: Page, opts: { perPage: number }): Promise<{ hovered: number; problems: string[] }> {
  const problems: string[] = []
  let hovered = 0
  for (const [name, path] of PAGES) {
    await page.goto(path)
    await page.waitForSelector(READY, { timeout: 45_000 })
    await settle(page)
    await page.waitForTimeout(2_500)
    let budget = opts.perPage
    for (const selector of HOVER_TARGETS) {
      if (budget <= 0) break
      const locator = page.locator(selector)
      const found = Math.min(await locator.count(), 2)
      for (let index = 0; index < found && budget > 0; index += 1) {
        const target = locator.nth(index)
        if (!(await target.isVisible().catch(() => false))) continue
        budget -= 1
        await target.hover({ timeout: 3_000 }).catch(() => {})
        await page.waitForTimeout(150)
        const r = await page.evaluate(inspect, { contrastAllow: CONTRAST_ALLOW })
        hovered += 1
        const groups: Array<[string, string[]]> = [
          ['对比度', r.lowContrast],
          ['压叠', r.overlap],
          ['裁切', r.clipped],
          ['折行', r.folded],
        ]
        for (const [label, lines] of groups) {
          for (const line of lines) problems.push(`${name} 悬停 ${selector} 第${index + 1}个 → ${label}：${line}`)
        }
        await page.mouse.move(0, 0)
        await page.waitForTimeout(60)
      }
    }
  }
  return { hovered, problems }
}

test.describe('悬停态体检 @1440', () => {
  test('逐页悬停顶栏步骤/页头按钮/行与芯片：对比度、压叠、裁切、折行都不许变坏', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    const { hovered, problems } = await hoverSweep(page, { perPage: 6 })
    console.log(`HOVER targets=${hovered} problems=${problems.length}`)
    expect(hovered, '一个可悬停元素都没找到——判据在空跑').toBeGreaterThanOrEqual(20)
    expect(problems, `悬停态问题：\n${problems.slice(0, 14).join('\n')}`).toHaveLength(0)
    /* 留一张悬停态的图：判据说"读得清"，图要能自己看出来——主按钮悬停是变深+抬起，
       不是变浅（变浅正是这条判据第一次跑就抓到的缺陷）。 */
    await page.goto('/monitor')
    await page.waitForSelector(READY, { timeout: 45_000 })
    await page.waitForTimeout(2_500)
    await page.locator('.page-head .ant-btn-primary').last().hover()
    await page.waitForTimeout(250)
    await page.screenshot({ path: 'test-results/ui-audit/hover-primary.png' })
  })

  /**
   * 窄档（1000 / 900）的悬停态。
   *
   * `.wf-rail__pill:hover` 与主按钮 hover 都带 `translateY(-1px)`，而窄档轨道更挤——
   * "抬起 + 加宽底色"会不会压到邻居，@1440 那一档看不出来。
   * 这里只判压叠/出容器/裁切/折行/对比度，**不判"文字被省略号截断"**：
   * 窄档收字是有意的设计（第十八批定的口径），一律判就成了逼产品改口径。
   */
  test('窄档 1000 / 900 悬停：抬起与加宽底色不许压到邻居', async ({ page }) => {
    /* 预算按实测给：两档 × 八页，每页都要 goto + 网络静 + 三枚悬停后各跑一次全页几何/对比度体检，
       一趟就是 3 分钟出头（2026-10-23 量到 180s 默认档直接超时——红的是预算，不是页面）。 */
    test.setTimeout(420_000)
    for (const width of [1000, 900]) {
      await page.setViewportSize({ width, height: 900 })
      const { hovered, problems } = await hoverSweep(page, { perPage: 3 })
      console.log(`HOVER-NARROW ${width} targets=${hovered} problems=${problems.length}`)
      expect(hovered, `${width} 档一个可悬停元素都没找到——判据在空跑`).toBeGreaterThanOrEqual(10)
      expect(problems, `${width} 档悬停态问题：\n${problems.slice(0, 12).join('\n')}`).toHaveLength(0)
    }
    await page.screenshot({ path: 'test-results/ui-audit/hover-narrow.png' })
  })
})

/**
 * 减动效与动画中的可读性 @1440。
 *
 * 立这两条的实测起因（2026-10-06，构建产物 + 4175 预览，七页全过）：
 * ① 旧横幅徽标挂的是 `page-hero-pulse 2s infinite`（这支脉冲已随旧视觉退役；这里留的是当时漏兜底的记录），而 `@media (prefers-reduced-motion: reduce)`
 *    那一块里**没有它**——开"减少动效"后 7/7 页都还在无限循环（`document.getAnimations()` 量到 `iter=Infinity`）；
 * ② 同一支动画淡的是**文字的不透明度**（0% → 100%、50% → 0.6）：10px/700 的「实时」对比度
 *    从 5.76:1 掉到 **3.08:1**，每两秒有将近半秒低于 AA 的 4.5:1。
 * 对标 NexusMind：它动环的那一支 `pulse-dot`（`Step4Report.vue:2961-2969`）改的是 box-shadow 的环，
 * `step-pulse`（`IncidentWorkspaceView.vue:1936-1947`）、`pulse-border-blue`（`Home.vue:2020`）同理；
 * 但它的 `.gsc-badge`（`IncidentWorkspaceView.vue:1450-1463`）淡的正是 opacity 1 → 0.6（9px 的字），
 * 那一处我们不照抄——照抄来的正是这个 3.08:1。
 */
const scanMotion = (): { infinite: string[]; running: number } => {
  const pathOf = (el: Element): string => {
    const parts: string[] = []
    let cur: Element | null = el
    while (cur && parts.length < 3) {
      parts.unshift(cur.tagName.toLowerCase() + (cur.classList.length ? '.' + [...cur.classList].join('.') : ''))
      cur = cur.parentElement
    }
    return parts.join(' > ')
  }
  const infinite: string[] = []
  const seen = new Set<string>()
  const consider = (el: Element, pseudo: string, cs: CSSStyleDeclaration) => {
    if (cs.animationName === 'none') return
    if (!cs.animationIterationCount.split(', ').includes('infinite')) return
    for (const name of cs.animationName.split(', ')) {
      const key = `${name}|${pathOf(el)}${pseudo}`
      if (seen.has(key)) continue
      seen.add(key)
      infinite.push(`${name} 还在动（${pathOf(el)}${pseudo}）`)
    }
  }
  /* 伪元素也要扫：`getAnimations()` 会把 `::before/::after` 上的动画算进来，
     只量元素就会出"样式层 0 条、动画层 12 条"这种对不上的账（antd 卡片加载的微光就挂在伪元素上）。 */
  for (const el of [...document.querySelectorAll('body *')]) {
    consider(el, '', getComputedStyle(el))
    consider(el, '::before', getComputedStyle(el, '::before'))
    consider(el, '::after', getComputedStyle(el, '::after'))
  }
  /* 只数"无限循环"的那几条：有限动画（antd 提示条退出 0.3s、卡片入场 0.2s）在减动效口径下是允许的，
     把它们算进来会让"恰好有一条提示正在收"变成误红——门禁不能自己带竞态。 */
  const running = document
    .getAnimations()
    .filter((a) => a.playState === 'running' && a.effect?.getTiming?.().iterations === Infinity).length
  return { infinite, running }
}

test.describe('减动效与动画中的可读性 @1440', () => {
  test('全部页面开"减少动效"后一个无限动画都不许留（正常态确有动效，不是在空跑）', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    const offenders: string[] = []
    let animated = 0
    for (const [name, path] of PAGES) {
      /* 编辑器那一页的空画布是**静态工作面**——没有该被减掉的动效，判据在它上面会空跑。
         它自己那条"还在轮询"的脉冲（头部那颗点）才是真的循环动画，所以这一档把编辑器开到
         正在跑的实例上：状态用桩固定成 running + 一个未开始的节点，实例号与版本仍取真后端那一条。 */
      if (name === 'workflow') {
        if (gateInstance === null) throw new Error('ensureGateRows 没记下门禁实例的身份——轮询档没法造')
        const { instanceId, workflowId } = gateInstance
        await page.route(/\/api\/v1\/workflow\/instances\/[^/?]+$/, (route) =>
          route.fulfill({
            status: 200,
            contentType: 'application/json',
            body: probeInstanceBody(instanceIdOf(route.request().url()), workflowId, [
              probeInstanceNode('gate', 'succeeded', { duration_ms: 900 }),
              probeInstanceNode('notify_ok', 'pending'),
            ]),
          }),
        )
        await page.goto(`/workflow?instance=${instanceId}`)
      } else {
        await page.goto(path)
      }
      await page.waitForSelector(READY, { timeout: 45_000 })
      await page.waitForTimeout(600)
      /* emulateMedia 是跟着 browser context 走的，一次设定后面每一页都还生效：
         不显式退回 no-preference，第二页起量到的"正常态"其实是减动效态（第一版就是这么误红过一次——
         正常态只剩 1 条无限动画，因为其余六页仍在 reduce 下）。 */
      await page.emulateMedia({ reducedMotion: 'no-preference' })
      await page.waitForTimeout(250)
      animated += (await page.evaluate(scanMotion)).infinite.length
      await page.emulateMedia({ reducedMotion: 'reduce' })
      await page.waitForTimeout(400)
      const reduced = await page.evaluate(scanMotion)
      for (const row of reduced.infinite) offenders.push(`${name}：${row}`)
      /* 真跑着的动画也要一起看：CSS 声明停了但 WAAPI 还在推进的（JS 驱动的动画）同样算没做到。 */
      if (reduced.running > 0) offenders.push(`${name}：getAnimations 里还有 ${reduced.running} 条 running`)
      if (name === 'workflow') await page.unroute(/\/api\/v1\/workflow\/instances\//)
    }
    console.log(`REDUCED-MOTION normal-infinite=${animated} offenders=${offenders.length}`)
    expect(animated, '每一页一个循环动画都没有——这条判据在空跑').toBeGreaterThanOrEqual(PAGES.length)
    expect(offenders, `开了减少动效还在动：\n${offenders.slice(0, 10).join('\n')}`).toHaveLength(0)
    await page.screenshot({ path: 'test-results/ui-audit/reduced-motion.png' })
  })

  /**
   * 加载态也要过这一关（第二十九批补的）。
   *
   * 上一条跑了两轮都绿，第三轮全量跑才红：那一刻 dashboard 恰好有按钮停在 loading，
   * 量到 `loadingCircle 还在动（…ant-btn-loading-icon > span.anticon-loading.anticon-spin）`
   * 与 `getAnimations 里还有 9 条 running`——**reduce 名单原先只列了我们自己写的三条，antd 那几支没进去**。
   * 状态类缺陷要显式制造（延迟接口让加载态常驻），不然这条判据只在碰巧有请求在飞时才有效。
   */
  test('antd 的加载态（按钮转圈 / Spin 点）在减动效下也不许转', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.emulateMedia({ reducedMotion: 'no-preference' })
    await page.route('**/api/**', async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 6_000))
      await route.continue()
    })
    await page.goto('/dashboard')
    await page.waitForSelector(READY, { timeout: 45_000 })
    await page.waitForTimeout(1_500)
    const normal = await page.evaluate(scanMotion)
    const spinNames = normal.infinite.filter((line) => /loading|spin|ant/i.test(line))
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await page.waitForTimeout(600)
    const reduced = await page.evaluate(scanMotion)
    console.log(
      `REDUCED-LOADING normal-infinite=${normal.infinite.length} spin=${spinNames.length} running=${normal.running} reduced=${reduced.infinite.length} reducedRunning=${reduced.running}`,
    )
    expect(spinNames.length, '没造出加载态动画——route 延迟没生效，判据在空跑').toBeGreaterThanOrEqual(1)
    expect(reduced.infinite, `减动效下还在转：\n${reduced.infinite.join('\n')}`).toHaveLength(0)
    expect(reduced.running, `减动效下 getAnimations 仍有 ${reduced.running} 条无限动画在跑`).toBe(0)
    await page.screenshot({ path: 'test-results/ui-audit/reduced-motion-loading.png' })
  })

  const motionContrast = (): Array<{ where: string; need: number; worst: number; samples: string }> => {
    type RGBA = { r: number; g: number; b: number; a: number }
    const parse = (c: string): RGBA | null => {
      const hex = /^#([0-9a-f]{6})$/i.exec(c)
      if (hex) {
        const n = parseInt(hex[1], 16)
        return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255, a: 1 }
      }
      const m = /rgba?\(([^)]+)\)/.exec(c)
      if (!m) return null
      const p = m[1].split(',').map((s) => parseFloat(s))
      return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 }
    }
    const gradFirst = (img: string): RGBA | null => {
      const m = /(?:^|[, (])\s*(#[0-9a-fA-F]{6}|rgba?\([^)]*\))/.exec(
        img.replace(/^(linear|radial|conic)-gradient\(/, ''),
      )
      return m ? parse(m[1]) : null
    }
    const over = (dst: RGBA, src: RGBA): RGBA => {
      const a = src.a + dst.a * (1 - src.a)
      if (a === 0) return dst
      return {
        r: (src.r * src.a + dst.r * dst.a * (1 - src.a)) / a,
        g: (src.g * src.a + dst.g * dst.a * (1 - src.a)) / a,
        b: (src.b * src.a + dst.b * dst.a * (1 - src.a)) / a,
        a,
      }
    }
    const lum = (c: { r: number; g: number; b: number }) => {
      const f = (v: number) => (v / 255 <= 0.03928 ? v / 255 / 12.92 : ((v / 255 + 0.055) / 1.055) ** 2.4)
      return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b)
    }
    const ratio = (x: { r: number; g: number; b: number }, y: { r: number; g: number; b: number }) => {
      const l1 = lum(x)
      const l2 = lum(y)
      return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05)
    }
    const pathOf = (el: Element): string => {
      const parts: string[] = []
      let cur: Element | null = el
      while (cur && parts.length < 2) {
        parts.unshift(cur.tagName.toLowerCase() + (cur.classList.length ? '.' + [...cur.classList].join('.') : ''))
        cur = cur.parentElement
      }
      return parts.join(' > ')
    }
    const out: Array<{ where: string; need: number; worst: number; samples: string }> = []
    for (const el of [...document.querySelectorAll('body *')]) {
      const cs = getComputedStyle(el)
      if (cs.animationName === 'none' || !cs.animationIterationCount.includes('infinite')) continue
      /* 只管"这个元素自己带文字"的：装饰件（伪元素、纯圆点）淡一下不违背可读性。 */
      const own = [...el.childNodes].some((n) => n.nodeType === 3 && (n.textContent ?? '').trim() !== '')
      if (!own) continue
      const chain: Element[] = []
      let cur: Element | null = el
      while (cur && chain.length < 8) {
        chain.unshift(cur)
        cur = cur.parentElement
      }
      let bg: RGBA = { r: 255, g: 255, b: 255, a: 1 }
      for (const c of chain) {
        const s = getComputedStyle(c)
        const image = s.backgroundImage
        if (image && image !== 'none') {
          const g = gradFirst(image)
          if (g) bg = over(bg, g)
        }
        const bc = parse(s.backgroundColor)
        if (bc && bc.a > 0) bg = over(bg, bc)
      }
      const base = parse(cs.color)
      if (!base) continue
      const fs = parseFloat(cs.fontSize)
      const bold = parseInt(cs.fontWeight, 10) >= 700
      const need = fs >= 24 || (fs >= 18.66 && bold) ? 3 : 4.5
      const anims = el.getAnimations()
      const samples: string[] = []
      let worst = 99
      for (const frac of [0, 0.25, 0.5, 0.75]) {
        for (const a of anims) {
          a.pause()
          const d = a.effect?.getTiming().duration
          a.currentTime = (typeof d === 'number' ? d : 2000) * frac
        }
        const r = ratio(over(bg, { ...base, a: base.a * parseFloat(getComputedStyle(el).opacity) }), bg)
        worst = Math.min(worst, r)
        samples.push(`t=${frac} op=${getComputedStyle(el).opacity} ${r.toFixed(2)}`)
      }
      for (const a of anims) a.play()
      out.push({ where: pathOf(el), need, worst: Number(worst.toFixed(2)), samples: samples.join(' | ') })
    }
    return out
  }

  test('循环动画不许把带字的标签淡到 AA 以下 @1440', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    const low: string[] = []
    let read = 0
    for (const [name, path] of PAGES) {
      await page.goto(path)
      await page.waitForSelector(READY, { timeout: 45_000 })
      await page.waitForTimeout(600)
      for (const row of await page.evaluate(motionContrast)) {
        read += 1
        if (row.worst < row.need) {
          low.push(`${name} ${row.where} 最低 ${row.worst}:1（要 ${row.need}:1）｜${row.samples}`)
        }
      }
    }
    /* 防空跑的地板换成**自检**：旧口径是"至少扫到一个"，那靠的是横幅徽标那支 `pulse` 无限脉冲；
       旧视觉停用之后全站没有"带字的 infinite 动画"了——这正是我们要的状态，但不能让判据因此静默空转。
       所以现场种一个把字淡到 0.25 的标签，扫不到就是扫描器坏了（数学/选择器/时序任一环节退化都会红）。 */
    await page.goto('/dashboard')
    await page.waitForSelector(READY, { timeout: 45_000 })
    await page.evaluate(() => {
      const style = document.createElement('style')
      style.id = 'probe-fade-css'
      style.textContent = '@keyframes probe-fade{0%,100%{opacity:1}50%{opacity:.25}}'
      const el = document.createElement('span')
      el.id = 'probe-fade-el'
      /* pathOf 只拼 tagName + class（不带 id），所以要给一个专属 class 才认得出来 */
      el.className = 'probe-fade-el'
      el.textContent = '实时'
      el.style.cssText =
        'position:fixed;top:140px;left:140px;z-index:9999;padding:4px;background:#FFFFFF;color:#0A0A0C;font-size:12px;animation:probe-fade 2s infinite'
      document.body.append(style, el)
    })
    await page.waitForTimeout(300)
    const planted = (await page.evaluate(motionContrast)).filter((row) => row.where.includes('probe-fade-el'))
    await page.evaluate(() => {
      document.getElementById('probe-fade-el')?.remove()
      document.getElementById('probe-fade-css')?.remove()
    })
    console.log(`MOTION-CONTRAST sampled=${read} low=${low.length} planted=${JSON.stringify(planted)}`)
    expect(planted.length, '现场种的"淡到 0.25 的带字无限动画"没被扫到——扫描器坏了，这条判据在空转').toBe(1)
    expect(planted[0].worst, `自检那格的最低对比=${planted[0]?.worst}，没低于门槛说明数学退化`).toBeLessThan(
      planted[0].need,
    )
    expect(low, `动画把字淡到读不清：\n${low.slice(0, 8).join('\n')}`).toHaveLength(0)
  })
})

/**
 * 微动效基准 @1440（第二十七批）。
 *
 * 摸底量到改之前**同屏并存六档**过渡时长：0.15（我们写的）/ 0.16（地图芯片）/ 0.18（轨道 pill）/
 * 0.2（antd 默认与卡片抬起）/ 0.24（顶栏步进器）/ 0.3（antd spin）。
 * 一档一档看过去像五个系统在各自动。NexusMind 全站是一个 `transition: all .15s`（284 处），
 * 这里把"悬停/选中这类状态反馈"钉在同一档，只留三档有理由的例外：
 * `0.1s`＝antd 内部的即时反馈（`motionDurationFast`）、`0.2s`＝antd 的复杂过渡（Slow，如 spin 淡入）、
 * `0.6s`＝时间线那一笔**一次性描线**（不是状态反馈，慢才读得出"在长"）。
 * 判据读的是计算样式，所以"库内部的默认时长没被 token 压住"这种漂移也抓得到。
 */
const MOTION_ALLOW = ['0.1s', '0.15s', '0.2s', '0.6s']

const scanDurations = (): { strays: string[]; tally: Record<string, number> } => {
  const pathOf = (el: Element): string => {
    const cls = el.classList
    return el.tagName.toLowerCase() + (cls.length ? '.' + [...cls].slice(0, 2).join('.') : '')
  }
  const strays: string[] = []
  const tally: Record<string, number> = {}
  for (const el of [...document.querySelectorAll('body *')]) {
    const cs = getComputedStyle(el)
    if (cs.transitionProperty === 'none') continue
    const durations = cs.transitionDuration.split(',').map((s) => s.trim())
    for (const d of durations) {
      if (d === '0s') continue
      tally[d] = (tally[d] ?? 0) + 1
    }
    /* 这份名单与上面的 `MOTION_ALLOW` 是**故意重复**的：`page.evaluate` 只序列化函数本身，
       引用不到模块作用域的常量（引用了就是运行时 undefined）。改一处要改两处。 */
    const bad = durations.filter((d) => d !== '0s' && !['0.1s', '0.15s', '0.2s', '0.6s'].includes(d))
    if (bad.length > 0) strays.push(`${pathOf(el)} → ${bad.join('/')}`)
  }
  return { strays, tally }
}

test.describe('微动效基准 @1440', () => {
  test('全部页面同屏只许出现四档时长，且基准那一档占多数', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    const strays: string[] = []
    const all: Record<string, number> = {}
    for (const [name, path] of PAGES) {
      await page.goto(path)
      await page.waitForSelector(READY, { timeout: 45_000 })
      await page.waitForTimeout(1_800)
      const r = await page.evaluate(scanDurations)
      for (const line of r.strays) strays.push(`${name} ${line}`)
      for (const [d, n] of Object.entries(r.tally)) all[d] = (all[d] ?? 0) + n
    }
    const base = all['0.15s'] ?? 0
    const total = Object.values(all).reduce((a, b) => a + b, 0)
    console.log(`MOTION-BASE tally=${JSON.stringify(all)} base=${base}/${total} strays=${strays.length}`)
    expect(total, '一个带过渡的元素都没扫到——判据在空跑').toBeGreaterThan(20)
    expect(strays, `出现了基准之外的时长档：\n${[...new Set(strays)].slice(0, 10).join('\n')}`).toHaveLength(0)
    expect(
      Object.keys(all).length,
      `时长还有 ${Object.keys(all).length} 档（${Object.keys(all).join(' ')}），没归一到四档以内`,
    ).toBeLessThanOrEqual(4)
    /* "归一"不是把每一档都留着：基准那一档得是**多数**，否则只是换了六个新值。 */
    expect(base / total, `0.15s 只占 ${((base / total) * 100).toFixed(0)}%，没真的归一`).toBeGreaterThanOrEqual(0.5)
  })
})

/**
 * 页头统计托盘的对齐 @1440（第二十九批）。
 *
 * 目视复核七页整页截图时抓到的：托盘是 `display:flex; align-items:center`，
 * 而带环形 KPI 的那一格比左右两格高出一截（实测 94px vs 44px），
 * 于是**三格的 24px 大数字不在同一条线上**——中间那颗比两边高 **25px**（cell top 184 vs 209）。
 * 数字托盘读起来 ragged，而且越加装饰件越歪：这类"格子里内容高度不等 + 垂直居中"的重叠类盲区，
 * 压叠/裁切/对比度三条判据都看不见。
 */
const trayAlignment = (): { cells: number[]; spread: number } => {
  const tray = document.querySelector('.page-head__stats')
  if (!tray) return { cells: [], spread: 0 }
  const tops: number[] = []
  for (const cell of [...tray.children]) {
    /* 量的是"那一排大数字"本身（antd Statistic 的 content 行），不是格子里字号最大的任意元素：
       带后缀标签（"达标"）的那一格会挑到别的节点，差出 3px 是探针的误差不是页面的缺陷。 */
    const number = cell.querySelector('.ant-statistic-content')
    if (number) {
      tops.push(Math.round(number.getBoundingClientRect().top))
      continue
    }
    let big: HTMLElement | null = null
    let bigSize = 0
    for (const el of [...cell.querySelectorAll('*')]) {
      const hasDigit = [...el.childNodes].some((n) => n.nodeType === 3 && /\d/.test(n.textContent ?? ''))
      if (!hasDigit) continue
      const px = parseFloat(getComputedStyle(el).fontSize)
      if (px > bigSize) {
        bigSize = px
        big = el as HTMLElement
      }
    }
    if (big) tops.push(Math.round(big.getBoundingClientRect().top))
  }
  return { cells: tops, spread: tops.length > 1 ? Math.max(...tops) - Math.min(...tops) : 0 }
}

test.describe('页头统计托盘对齐 @1440', () => {
  test('每一格的大数字顶边在同一条线上（±1px），带环的那格不许把数字抬走', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    const problems: string[] = []
    let trays = 0
    for (const [name, path] of PAGES) {
      await page.goto(path)
      await page.waitForSelector(READY, { timeout: 45_000 })
      await page.waitForTimeout(1_500)
      const r = await page.evaluate(trayAlignment)
      if (r.cells.length === 0) continue
      trays += 1
      console.log(`TRAY ${name} tops=${JSON.stringify(r.cells)} spread=${r.spread}`)
      if (r.cells.length > 1 && r.spread > 1)
        problems.push(`${name} 大数字顶边差 ${r.spread}px（${r.cells.join(' / ')}）`)
    }
    /* 地板 2→1（2026-10-17 dashboard 标杆页 spike）：态势页的四个 KPI 从页头托盘搬进了独立读数带
       （见 `components/dashboard/KpiStrip.vue`），`.page-head__stats` 只剩指标页一处——
       而这条判据的原始事故（带环那格把数字抬走 25px）恰恰就在指标页，主体没丢。
       七页里第二处托盘在推演沙盒页，但它不在 PAGES 名单内，所以地板按实量下调而不是放虚。 */
    expect(trays, '一个统计托盘都没找到——判据在空跑').toBeGreaterThanOrEqual(1)
    expect(problems, `托盘里的数字不在一条线上：\n${problems.join('\n')}`).toHaveLength(0)
  })
})

/**
 * 「服务端已存定义」自己长出来 @1440（第二十八批）。
 *
 * 这一列此前只在进页/保存/归档时对账：助手或同事刚存的那一版，值班员要**刷新整页**才看得见，
 * 而画布上有未保存改动时刷新还会触发"丢弃吗"的确认——等于没有这条路。
 * 现在每 15 秒自查一次，并把取数时刻与"多旧"写在同一栏。
 * 这条判据量的正是"不 reload、不点任何按钮"这一件事，所以中间不许有任何用户动作。
 */
test.describe('定义列表自查 @1440', () => {
  test('别人刚存的那一版会自己出现在列表里，且新鲜度条说得出什么时候取的数', async ({ page, request }) => {
    test.setTimeout(90_000)
    const base = process.env.AEGIS_API_TARGET ?? 'http://127.0.0.1:8000'
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.goto('/workflow')
    await page.waitForSelector(READY, { timeout: 45_000 })
    await settle(page)
    await page.locator('[data-testid="header-def"]').click()
    await page.waitForSelector('[data-testid="editor-panel"] .wf-def', { timeout: 15_000 })
    await page.waitForTimeout(2_000)

    const before = await page.locator('.wf-def__list-item').count()
    expect(before, '定义列表一行都没有——判据在空跑').toBeGreaterThanOrEqual(3)

    const name = `GATE-自刷-${Date.now().toString().slice(-7)}`
    const nodes = [0, 1].map((i) => ({
      node_id: `gate_poll_${i}`,
      type: 'notify',
      name: `自刷节点 ${i}`,
      config: { text: `p${i}` },
      sla_ms: 8000,
      timeout_ms: 5000,
      on_failure: 'abort',
      retry: { max_attempts: 1, backoff_ms: 0 },
    }))
    const created = await request.post(`${base}/api/v1/workflow/definitions`, {
      data: {
        name,
        description: '门禁用来验"列表会不会自己长出来"的定义',
        nodes,
        edges: [{ source: 'gate_poll_0', target: 'gate_poll_1' }],
      },
    })
    expect(created.status(), `门禁自己存一份定义都失败（${created.status()}）`).toBe(201)
    const createdId = String((await created.json()).workflow_id ?? '')

    try {
      /* 这里不做任何用户动作：不 reload、不点刷新，只等定时器自己跑一轮（15 秒一档，给到 30 秒）。 */
      await expect
        .poll(() => page.locator('.wf-def__list-item').count(), { timeout: 30_000, intervals: [1_000] })
        .toBeGreaterThan(before)
      await expect(page.locator('.wf-def__list-item', { hasText: name })).toHaveCount(1)

      const fresh = page.locator('.wf-def [data-testid="freshness"]')
      await expect(fresh, '定义那一列没挂新鲜度条——自己刷了却不说几点取的数').toHaveCount(1)
      const text = (await fresh.textContent()) ?? ''
      console.log(
        `DEF-POLL rows ${before} → ${await page.locator('.wf-def__list-item').count()} fresh="${text.replace(/\s+/g, ' ')}"`,
      )
      expect(text, '新鲜度条没报窗口口径').toContain('每 15 秒自己对一遍')
      expect(text, '新鲜度条没说取数时刻').toMatch(/取数 \d{1,2}:\d{2}/)
      expect(text, '新鲜度条没说出这份数多旧').toMatch(/\d+ 秒前|\d+ 分前/)
      await page.screenshot({ path: 'test-results/ui-audit/def-self-refresh.png' })
    } finally {
      await request.post(`${base}/api/v1/workflow/definitions/${encodeURIComponent(createdId)}/archive`)
    }
  })
})

/**
 * 深色主题（大屏演示模式）——阶段 1 立起来的换肤判据。
 *
 * 为什么单独钉一条而不是把上面 WIDTHS 那几档复制一遍：这条盯的不是几何而是**表面归属**。
 * 深色下还剩一块亮底，就是规则里点名的"白底补丁"——它只会在整页截图里刺眼，
 * 重叠/出屏/裁字那四类几何判据一条都不会红（顶栏此前正是这样：antd 组件变了深蓝黑，外壳仍是 rgb(242,244,248)）。
 *
 * 三条判据：
 * 1) 结构性表面（body/外壳/顶栏/内容区/卡片/卡头/表头/输入框/标签）在深色下必须**是暗的**（0–255 相对亮度 ≤128）；
 *    同一套读数在浅色下必须**是亮的**——两个方向都验，这样判据本身反了或写坏了会当场露出来（不是在空跑）；
 * 2) 七页在深色下过同一套常驻几何与 AA 对比度判据（复用上面那个 inspect，不自建一套宽松版）；
 * 3) 顶栏切换器真的在换肤、真的存得住：点"深"→ `data-theme=dark` → reload 之后还是深色。
 *
 * 页签导航那一组单独钉一条：页名全档铺开（1100–1920 四档连跑），任何一档把页名挤回编号点、
 * 或让右列压叠/裁字，都会当场红——顶栏是七页共用的唯一导航，它的破绽在哪一页都一样刺眼。
 * ECharts / Cesium / Vue Flow 的**画面内部**不在这里（那是阶段 2 的活，现在钉上就是一条永远红的判据），
 * 用的是上面同一份 skipSel：画布与地图容器整体跳过。
 */
const shellLum = (opts: { limit: number; wantBright: boolean }) => {
  const { limit, wantBright } = opts
  const parse = (value: string): number[] | null => {
    const m = /rgba?\(([^)]+)\)/.exec(value)
    if (!m) return null
    const parts = m[1]
      .split(/[,\s/]+/)
      .filter(Boolean)
      .map(Number)
    if (parts.length >= 4 && parts[3] < 0.9) return null
    return parts.slice(0, 3)
  }
  const lum = (c: number[]) => 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
  const SELS = [
    'body',
    '.app-shell',
    '.sidebar',
    '.topbar',
    '.main-content',
    '.ant-card',
    '.ant-card-head',
    '.ant-table-thead > tr > th',
    '.ant-input',
    '.ant-select-selector',
  ]
  const wrong: string[] = []
  let seen = 0
  for (const sel of SELS) {
    for (const el of Array.from(document.querySelectorAll(sel)).slice(0, 4)) {
      const rgb = parse(getComputedStyle(el).backgroundColor)
      if (!rgb) continue
      seen += 1
      const bright = lum(rgb) > limit
      if (wantBright ? !bright : bright)
        wrong.push(`${sel}=${getComputedStyle(el).backgroundColor} 亮度=${Math.round(lum(rgb))}`)
    }
  }
  return { wrong, seen }
}

test.describe('深色主题换肤', () => {
  test('结构性表面两档都归位：深色全是暗面、浅色全是亮面 @1440', async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem('aegis.theme', 'dark'))
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.goto('/dashboard')
    await page.waitForSelector('.ant-card', { timeout: 45_000 })
    await settle(page)
    await page.waitForTimeout(2_000)
    const dark = await page.evaluate(shellLum, { limit: 128, wantBright: false })
    console.log(`SHELL-dark seen=${dark.seen} wrong=${dark.wrong.join(' | ')}`)
    /* 地板 8→7（2026-10-17 dashboard 标杆页 spike）：这张页原先的两张 antd 表换成了自绘行，
       SELS 里 `.ant-table-thead > tr > th` 这一路在 /dashboard 归零，实量剩
       body + .app-shell + .navbar + 4×.ant-card = 7。地板的职责是"别空跑"，不是"必须 8"，
       所以随实量下调、判据与两向对照都保留。
       （2026-10-17 晚信息架构收并：`.navbar` 换成 `.sidebar` + `.topbar` 两族表面，
       读数只增不减；地板不动。） */
    expect(dark.seen, '一块表面都没读到——判据在空跑').toBeGreaterThanOrEqual(7)
    expect(dark.wrong, `深色下还有亮底（白底补丁）：\n${dark.wrong.join('\n')}`).toHaveLength(0)

    /* 反向对照：同一份读数在浅色下必须全是亮面。只验深色一边的话，判据写反了（或恒真）也是绿的。 */
    await page.click('[data-testid="theme-light"]')
    await page.waitForTimeout(700)
    const light = await page.evaluate(shellLum, { limit: 128, wantBright: true })
    console.log(`SHELL-light seen=${light.seen} wrong=${light.wrong.join(' | ')}`)
    expect(light.seen, '浅色这一档没读到表面——两向对照失效').toBeGreaterThanOrEqual(7)
    expect(
      light.wrong,
      `浅色主题里出现了暗面（说明令牌被写死、没跟着主题翻）：\n${light.wrong.join('\n')}`,
    ).toHaveLength(0)
    expect(await page.evaluate(() => document.documentElement.dataset.theme), '浅色档没落到 DOM 上').toBe('light')
  })

  test('顶栏切换器：点"深"就换肤，reload 之后还记得 @1440', async ({ page }) => {
    /* 要带条件：addInitScript 每次导航都跑（reload 也算），无条件写会把刚存下的 dark 又盖回 light，
       于是"持久化"这条永远验不到真东西——探针里同一个坑栽过一次。 */
    await page.addInitScript(() => {
      if (!localStorage.getItem('aegis.theme')) localStorage.setItem('aegis.theme', 'light')
    })
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.goto('/dashboard')
    await page.waitForSelector('.theme-switch', { timeout: 45_000 })
    await page.waitForTimeout(1_200)
    expect(await page.evaluate(() => document.documentElement.dataset.theme), '初始该是浅色').toBe('light')
    await page.click('[data-testid="theme-dark"]')
    await page.waitForTimeout(700)
    const after = await page.evaluate(() => ({
      theme: document.documentElement.dataset.theme,
      stored: localStorage.getItem('aegis.theme'),
      scheme: document.documentElement.style.colorScheme,
      pressed: document.querySelector('.theme-switch__btn.is-on')?.getAttribute('aria-pressed'),
      body: getComputedStyle(document.body).backgroundColor,
    }))
    console.log(`SWITCH click -> ${JSON.stringify(after)}`)
    expect(after.theme, '点了"深"但 data-theme 没翻').toBe('dark')
    expect(after.stored, '选择没存进 localStorage').toBe('dark')
    expect(after.scheme, 'color-scheme 没跟着翻（原生控件与滚动条会留浅色）').toBe('dark')
    expect(after.pressed, '选中那格的 aria-pressed 没翻（读屏听不出当前是哪档）').toBe('true')
    /* 不能用 waitUntil:'networkidle'：SSE 是常驻连接，reload 会一直等到 180s 测试超时
       （2026-10-17 实测，与上面 settle() 同一根因）。等 DOM 就绪即可，主题落没落由后面的断言判。 */
    await page.reload({ waitUntil: 'domcontentloaded' }).catch(() => {})
    await page.waitForSelector('.topbar', { timeout: 45_000 })
    expect(await page.evaluate(() => document.documentElement.dataset.theme), 'reload 之后忘了用户选过深色').toBe(
      'dark',
    )
    /* 侧栏导航全档铺页名（2026-10-17 收并后）：title/aria-label 仍要带着页名（读屏与 hover 都靠它），
       可见文字本身也得是真页名（展开态全名、收起态短名，两个都不是占位符）。 */
    const navText = await page.evaluate(() => ({
      titled: [...document.querySelectorAll('.side-item')].every((el) => (el.getAttribute('title') ?? '').length > 1),
      labelled: [...document.querySelectorAll('.side-item__label')].every(
        (el) => (el.textContent ?? '').trim().length > 1,
      ),
    }))
    expect(navText.titled, '侧栏入口的 title 里没了页名——读屏只剩下一格空壳').toBe(true)
    expect(navText.labelled, '侧栏铺出来的不是页名（空文字或单字符）').toBe(true)
  })

  for (const [name, path] of PAGES) {
    test(`${name} 深色：无重叠、不出屏、无裁字、不折行、对比度达标 @1440`, async ({ page }) => {
      await page.addInitScript(() => localStorage.setItem('aegis.theme', 'dark'))
      await page.setViewportSize({ width: 1440, height: 900 })
      await page.goto(path)
      await page.waitForSelector(READY, { timeout: 45_000 })
      await settle(page)
      await page.waitForTimeout(2_500)
      const r = await page.evaluate(inspect, { contrastAllow: CONTRAST_ALLOW })
      console.log(
        `DARK ${name} leaves=${r.leafCount} overlap=${r.overlapCount} beyond=${r.beyondCount} clipped=${r.clippedCount} folded=${r.foldedCount} lowContrast=${r.lowContrastCount}`,
      )
      expect(r.leafCount, `${name} 深色下只看到 ${r.leafCount} 个文本节点——判据在空跑`).toBeGreaterThan(0)
      expect(r.overlapCount, `${name} 深色重叠：\n${r.overlap.join('\n')}`).toBe(0)
      expect(r.beyondCount, `${name} 深色出屏：\n${r.beyond.join('\n')}`).toBe(0)
      expect(r.clippedCount, `${name} 深色文字被裁：\n${r.clipped.join('\n')}`).toBe(0)
      expect(r.foldedCount, `${name} 深色按钮折行：\n${r.folded.join('\n')}`).toBe(0)
      expect(r.lowContrastCount, `${name} 深色对比度不足：\n${r.lowContrast.join('\n')}`).toBe(0)
      await page.screenshot({ path: `test-results/ui-audit/dark-${name}.png` })
    })
  }

  /* 侧栏导航（2026-10-17 信息架构收并：值班主线 4 + 系统 1，旧入口退场）。
     "铺没铺"从顶栏页签改成侧栏入口：四档连跑（1100–1920），每一档都要求页名在场、
     右列不压叠不裁字、侧栏自身不裁——任何一档空间算错都会当场红。
     侧栏是定宽令牌（232px），不随视口伸缩，所以真正会随宽度出问题的是顶栏右列。 */
  for (const width of [1100, 1280, 1440, 1920]) {
    test(`侧栏导航 @${width}：页名铺开，右列不压叠、不裁字`, async ({ page }) => {
      await page.addInitScript(() => localStorage.setItem('aegis.theme', 'dark'))
      await page.setViewportSize({ width, height: 1080 })
      await page.goto('/dashboard')
      await page.waitForSelector('.sidebar', { timeout: 45_000 })
      await settle(page)
      await page.waitForTimeout(2_000)
      const nav = await page.evaluate(() => {
        const box = (sel: string) => {
          const el = document.querySelector(sel)
          if (!el) return null
          const r = el.getBoundingClientRect()
          return {
            left: Math.round(r.left),
            right: Math.round(r.right),
            w: Math.round(r.width),
            scroll: el.scrollWidth,
            client: el.clientWidth,
          }
        }
        return {
          side: box('.sidebar'),
          right: box('.topbar-right'),
          status: box('.nav-status'),
          sw: box('.theme-switch'),
          labels: document.querySelectorAll('.side-item__label').length,
          labelShown: [...document.querySelectorAll('.side-item__label')].every(
            (el) => getComputedStyle(el).display !== 'none',
          ),
        }
      })
      console.log(`NAV@${width} ${JSON.stringify(nav)}`)
      expect(nav.labelShown, `@${width} 侧栏页名没铺开——这一档不许把入口收成图标`).toBe(true)
      expect(nav.labels, '侧栏入口数不对（值班主线 4 + 系统 1）').toBe(5)
      expect(
        nav.status!.right,
        `状态胶囊压进主题切换器（胶囊右缘=${nav.status!.right}，切换器左缘=${nav.sw!.left}）`,
      ).toBeLessThanOrEqual(nav.sw!.left + 1)
      expect(
        nav.right!.scroll - nav.right!.client,
        `右列被裁：scroll=${nav.right!.scroll} client=${nav.right!.client}`,
      ).toBeLessThanOrEqual(1)
      expect(
        nav.status!.scroll - nav.status!.client,
        `状态胶囊静默裁字：scroll=${nav.status!.scroll} client=${nav.status!.client}`,
      ).toBeLessThanOrEqual(1)
      expect(
        nav.side!.scroll - nav.side!.client,
        `侧栏静默裁字：scroll=${nav.side!.scroll} client=${nav.side!.client}`,
      ).toBeLessThanOrEqual(1)
      if (width === 1920) await page.screenshot({ path: 'test-results/ui-audit/dark-nav-1920.png' })
    })
  }
})

/**
 * 画布像素级换肤判据（ECharts 那一面）。
 *
 * 为什么 DOM 那套不够：ECharts 画在 canvas 里，常驻的 `inspect()` 又把 canvas 整体 skip，
 * 所以"主题注册到底生效没有"在 DOM 上量不出来——改造前深色下量到 91.2% 的成笔像素
 * 对比只有 3:1–4.5:1（轴标还是内置的 #666），而页面 DOM 一切正常。
 * 口径：只统计 alpha≥240 的成笔像素（抗锯齿边缘本就落在两支令牌之间，不算违规），
 * 每一颗都必须在**当前主题**的 `--aeg-*` 里找得到逐通道差 ≤2 的一支。
 * 阈值 1.5% 由实测定：浅/深两档未命中占比最大 0.80%（`E:\tmp\chart-gate-numbers.mjs`）。
 */
const canvasInk = (opts: { tol: number }) => {
  const { tol } = opts
  const parse = (value: string): number[] | null => {
    const m = /rgba?\(([^)]+)\)/.exec(value)
    if (!m) return null
    const p = m[1]
      .split(/[,\s/]+/)
      .filter(Boolean)
      .map(Number)
    return [p[0], p[1], p[2]]
  }
  const root = document.documentElement
  const scratch = document.createElement('div')
  document.body.appendChild(scratch)
  const palette: number[][] = []
  for (const decl of (root.getAttribute('style') ?? '').split(';')) {
    const key = (decl.split(':')[0] ?? '').trim()
    if (!key.startsWith('--aeg-')) continue
    scratch.style.color = `var(${key})`
    const rgb = parse(getComputedStyle(scratch).color)
    if (rgb) palette.push(rgb)
  }
  scratch.remove()
  const near = (p: number[]) =>
    palette.some((q) => Math.abs(p[0] - q[0]) <= tol && Math.abs(p[1] - q[1]) <= tol && Math.abs(p[2] - q[2]) <= tol)

  let opaque = 0
  let unmatched = 0
  const freq = new Map<string, number>()
  for (const canvas of [...document.querySelectorAll('canvas')]) {
    const ctx = (canvas as HTMLCanvasElement).getContext('2d')
    if (!ctx || canvas.width < 8) continue
    const d = ctx.getImageData(0, 0, canvas.width, canvas.height).data
    for (let i = 0; i < d.length; i += 4) {
      if (d[i + 3] < 240) continue
      opaque++
      if (near([d[i], d[i + 1], d[i + 2]])) continue
      unmatched++
      const key = `${d[i]},${d[i + 1]},${d[i + 2]}`
      freq.set(key, (freq.get(key) ?? 0) + 1)
    }
  }
  const worst = [...freq.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, 4)
    .map(([c, n]) => `${c}×${n}`)
  return {
    theme: root.dataset.theme ?? '',
    slots: palette.length,
    opaque,
    unmatched,
    pct: opaque ? Number(((unmatched / opaque) * 100).toFixed(2)) : 0,
    worst,
  }
}

test.describe('ECharts 画布换肤', () => {
  const CHART_PAGES: Array<[string, string]> = [
    ['monitor', '/monitor'],
    ['metrics', '/metrics'],
  ]
  for (const [name, path] of CHART_PAGES) {
    test(`${name} 画布只用当前主题的令牌色，翻主题立刻重画 @1440`, async ({ page }) => {
      await page.addInitScript(() => localStorage.setItem('aegis.theme', 'dark'))
      await page.setViewportSize({ width: 1440, height: 900 })
      await page.goto(path)
      await page.waitForSelector('.ant-card canvas', { timeout: 45_000 })
      await settle(page)
      await page.waitForTimeout(2_200)
      const dark = await page.evaluate(canvasInk, { tol: 2 })
      console.log(`CANVAS-${name}-dark ${JSON.stringify(dark)}`)
      expect(dark.slots, '一个 --aeg-* 颜色变量都没读到——判据在空跑').toBeGreaterThanOrEqual(15)
      expect(dark.opaque, '画布上没有成笔像素——判据在空跑').toBeGreaterThanOrEqual(15_000)
      expect(
        dark.pct,
        `深色画布上有令牌外的色（${dark.unmatched} 颗，最重的几支 ${dark.worst.join(' / ')}）`,
      ).toBeLessThanOrEqual(1.5)

      /* 点切换器翻到浅色再量一遍：如果实例没随主题重建，这一趟读到的仍是深色的墨，
         未命中占比会直接冲到两位数——这一条同时是"翻转立刻重画"的证据。 */
      await page.click('[data-testid="theme-light"]')
      await page.waitForTimeout(1_500)
      const light = await page.evaluate(canvasInk, { tol: 2 })
      console.log(`CANVAS-${name}-light ${JSON.stringify(light)}`)
      expect(light.theme, '点了"浅"但主题没翻').toBe('light')
      expect(
        light.pct,
        `浅色画布上有令牌外的色（${light.unmatched} 颗，最重的几支 ${light.worst.join(' / ')}）`,
      ).toBeLessThanOrEqual(1.5)
      await page.screenshot({ path: `test-results/ui-audit/canvas-${name}-light.png` })
    })
  }
})
