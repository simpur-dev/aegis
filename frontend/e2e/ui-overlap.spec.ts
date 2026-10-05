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
const WIDTHS = [1440, 1280, 1200, 1100]

/** 对比度豁免：这些位置的字按 antd/交互语义本就该是"非正文"色，不是内容读不到。 */
const CONTRAST_ALLOW = [
  '.ant-btn[disabled]',
  '.ant-btn-loading',
  '.ant-select-selection-placeholder',
  '.ant-table-placeholder',
  '.ant-empty-description',
  '.cesium-widget-credits',
].join(',')

const inspect = (contrastAllow: string) => {
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
  const skip = (el: Element) => !!(el as HTMLElement).closest('.map-frame,.vue-flow,.cesium-viewer,.ant-modal,.ant-drawer,.ant-message,.ant-notification,canvas')
  const visible = (el: Element) => {
    const cs = getComputedStyle(el)
    if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) < 0.05) return false
    const r = el.getBoundingClientRect()
    return r.width > 1 && r.height > 1
  }
  const leaves = [...document.querySelectorAll('body *')].filter(
    (el) => el.children.length === 0 && !skip(el) && visible(el) && (el.textContent ?? '').trim().length > 0,
  )

  const overlap: string[] = []
  const boxes = leaves.map((el) => ({ el, r: el.getBoundingClientRect() }))
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
    const r = el.getBoundingClientRect()
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
    overlap: overlap.slice(0, 8),
    overlapCount: overlap.length,
    beyond: beyond.slice(0, 8),
    beyondCount: beyond.length,
    clipped: clipped.slice(0, 8),
    clippedCount: clipped.length,
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
        await page.waitForTimeout(name === 'map' ? 12_000 : 2_500)
        const r = await page.evaluate(inspect, CONTRAST_ALLOW)
        console.log(`UI ${name}@${width} overlap=${r.overlapCount} beyond=${r.beyondCount} clipped=${r.clippedCount} navClip=${r.navClip.length} lowContrast=${r.lowContrastCount} centerOffset=${r.centerOffset}`)
        expect(r.overlapCount, `${name}@${width} 重叠明细：\n${r.overlap.join('\n')}`).toBe(0)
        expect(r.beyondCount, `${name}@${width} 有内容出屏：\n${r.beyond.join('\n')}`).toBe(0)
        expect(r.clippedCount, `${name}@${width} 有文字被裁：\n${r.clipped.join('\n')}`).toBe(0)
        expect(r.navClip, `${name}@${width} 顶栏把内容裁掉了（列宽不够）：\n${r.navClip.join('\n')}`).toHaveLength(0)
        expect(r.lowContrastCount, `${name}@${width} 有字对比度不足：\n${r.lowContrast.join('\n')}`).toBe(0)
        if (r.centerOffset !== null) expect(r.centerOffset, `${name}@${width} 步进器偏离中线`).toBeLessThanOrEqual(1)
      })
    }
  })
}
