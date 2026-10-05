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
    lowContrast: lowContrast.slice(0, 10),
    lowContrastCount: lowContrast.length,
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
        console.log(`UI ${name}@${width} leaves=${r.leafCount} overlap=${r.overlapCount} beyond=${r.beyondCount} clipped=${r.clippedCount} folded=${r.foldedCount} navClip=${r.navClip.length} lowContrast=${r.lowContrastCount} centerOffset=${r.centerOffset}`)
        expect(r.leafCount, `${name}@${width} 页面一个文本节点都没看到——判据在空跑`).toBeGreaterThan(30)
        expect(r.overlapCount, `${name}@${width} 重叠明细：\n${r.overlap.join('\n')}`).toBe(0)
        expect(r.beyondCount, `${name}@${width} 有内容出屏：\n${r.beyond.join('\n')}`).toBe(0)
        expect(r.clippedCount, `${name}@${width} 有文字被裁：\n${r.clipped.join('\n')}`).toBe(0)
        expect(r.foldedCount, `${name}@${width} 有按钮文字折行（"打/开"竖排）：\n${r.folded.join('\n')}`).toBe(0)
        expect(r.navClip, `${name}@${width} 顶栏把内容裁掉了（列宽不够）：\n${r.navClip.join('\n')}`).toHaveLength(0)
        expect(r.lowContrastCount, `${name}@${width} 有字对比度不足：\n${r.lowContrast.join('\n')}`).toBe(0)
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
        await page.waitForTimeout(3_000)
        const r = await page.evaluate(inspect, { contrastAllow: CONTRAST_ALLOW })
        console.log(`FAULT ${name}@${width} leaves=${r.leafCount} overlap=${r.overlapCount} beyond=${r.beyondCount} clipped=${r.clippedCount} folded=${r.foldedCount} lowContrast=${r.lowContrastCount}`)
        await page.screenshot({ path: `test-results/ui-audit/fault-${name}-${width}.png` })
        expect(r.leafCount, `${name}@${width} 故障态只看到 ${r.leafCount} 个文本节点——判据在空跑`).toBeGreaterThanOrEqual(12)
        expect(r.overlapCount, `${name}@${width} 故障态重叠：\n${r.overlap.join('\n')}`).toBe(0)
        expect(r.beyondCount, `${name}@${width} 故障态出屏：\n${r.beyond.join('\n')}`).toBe(0)
        expect(r.clippedCount, `${name}@${width} 故障态文字被裁：\n${r.clipped.join('\n')}`).toBe(0)
        expect(r.foldedCount, `${name}@${width} 故障态按钮折行：\n${r.folded.join('\n')}`).toBe(0)
        expect(r.lowContrastCount, `${name}@${width} 故障态对比度不足：\n${r.lowContrast.join('\n')}`).toBe(0)
      })
    }
  })
}

test.describe('空数据态不变量 @1440', () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.route('**/api/v1/warnings*', (route) => route.fulfill({ json: { items: [], total: 0 } }))
    await page.route('**/api/v1/events*', (route) => route.fulfill({ json: { items: [] } }))
  })
  for (const [name, path] of [['warnings', '/warnings'], ['dashboard', '/dashboard']] as const) {
    test(`没有数据时 ${name} 的空态排版达标`, async ({ page }) => {
      await page.goto(path)
      await page.waitForSelector('.page-hero', { timeout: 45_000 })
      await page.waitForTimeout(2_500)
      const r = await page.evaluate(inspect, { contrastAllow: CONTRAST_ALLOW })
      const emptyText = await page.evaluate(() => {
        /* 只认 .empty-hero：上一版写了 '.empty-hero, .ant-empty'，于是态势页其实是
           匹配到了风险网格那块 a-empty 才过的——空态判据不能认"页面上随便哪块空态" */
        const el = document.querySelector('.empty-hero')
        return el ? (el.textContent ?? '').trim().slice(0, 40) : null
      })
      console.log(`EMPTY ${name} leaves=${r.leafCount} overlap=${r.overlapCount} clipped=${r.clippedCount} folded=${r.foldedCount} lowContrast=${r.lowContrastCount} empty=${JSON.stringify(emptyText)}`)
      await page.screenshot({ path: `test-results/ui-audit/empty-${name}.png` })
      expect(emptyText, `${name} 空态没渲染出来——这项目判据就没了对象`).not.toBeNull()
      expect(r.overlapCount, `${name} 空态重叠：\n${r.overlap.join('\n')}`).toBe(0)
      expect(r.clippedCount, `${name} 空态文字被裁：\n${r.clipped.join('\n')}`).toBe(0)
      expect(r.foldedCount, `${name} 空态按钮折行：\n${r.folded.join('\n')}`).toBe(0)
      expect(r.lowContrastCount, `${name} 空态对比度不足：\n${r.lowContrast.join('\n')}`).toBe(0)
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
      return { count: nodes.length, zoom, overlap, spill }
    })

    const check = async (phase: string) => {
      const r = await measure()
      console.log(`CANVAS[${phase}] nodes=${r.count} overlap=${r.overlap.length} spill=${r.spill.length}`)
      await page.screenshot({ path: `test-results/ui-audit/canvas-${phase}.png` })
      expect(r.count, `${phase}：画布上没有节点——判据在空跑`).toBeGreaterThan(1)
      expect(r.overlap, `${phase} 节点卡互相压叠：\n${r.overlap.join('\n')}`).toHaveLength(0)
      expect(r.spill, `${phase} 节点标题出框：\n${r.spill.join('\n')}`).toHaveLength(0)
    }

    await check('opened')
    await page.getByRole('button', { name: '自动布局' }).click()
    await page.waitForTimeout(1_800)
    await check('auto-layout')
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
