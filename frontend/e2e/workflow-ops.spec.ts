/**
 * 画布操作语法的真机验收（批次 C4）。
 *
 * 这里走的是**真浏览器、真剪贴板、真鼠标**：框选 → 复制 → 粘贴 → 拖动 → 撤销 3 次 → 重做 3 次，
 * 外加两处"不动作"的判据（输入框里按 Delete 不许删节点；剪贴板里是坏 JSON 时只报错、图不许变）。
 * 与 `ui-overlap.spec.ts` 同一条门禁：构建产物 + `vite preview`（4175）；
 * dev server 只用于调试，不能当交付证据。
 *
 * 为什么要真剪贴板：`navigator.clipboard` 在权限/焦点不对时是"读回空串"，
 * 单测里注入适配器永远绿；这里用 `permissions` 授权后走系统剪贴板，
 * 复制完再把文本读回来确认写的是本项目的片段格式。
 */
import { expect, test, type Page } from '@playwright/test'

const READY = '[data-testid="editor-header"]'

const settle = async (page: Page): Promise<void> => {
  await page.waitForLoadState('networkidle', { timeout: 8_000 }).catch(() => {})
}

interface NodePos {
  id: string
  x: number
  y: number
}

const readNodes = (page: Page): Promise<NodePos[]> =>
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

const nodeCount = (page: Page): Promise<number> => page.locator('.wf-node').count()

/**
 * 找一块"能接收框选起手"的空白点：最靠右下（离节点最近）的 pane 点，
 * 保证从它往右下拖出来的矩形能盖住全部节点，又不会先点到工具条/面板/小地图。
 */
const pickMarquee = (page: Page) =>
  page.evaluate(() => {
    const canvas = document.querySelector('[data-testid="workflow-canvas"]')?.getBoundingClientRect()
    if (!canvas) return null
    const cards = [...document.querySelectorAll('.wf-node')].map((node) => node.getBoundingClientRect())
    if (cards.length === 0) return null
    const minX = Math.min(...cards.map((r) => r.left))
    const minY = Math.min(...cards.map((r) => r.top))
    const maxX = Math.max(...cards.map((r) => r.right))
    const maxY = Math.max(...cards.map((r) => r.bottom))

    const blocking = '.vue-flow__node, .ed-toolbar, .ed-panel, .vue-flow__minimap, .vue-flow__controls, .ant-message'
    const blank = (x: number, y: number): boolean => {
      const el = document.elementFromPoint(x, y)
      if (!el || el.closest('.vue-flow__pane') === null) return false
      return el.closest(blocking) === null
    }

    let start: { x: number; y: number } | null = null
    for (let y = canvas.top + 76; y <= minY - 12; y += 20) {
      for (let x = canvas.left + 12; x <= minX - 12; x += 20) {
        if (blank(x, y)) start = { x, y }
      }
    }
    if (start === null) return null
    return {
      start,
      end: {
        x: Math.min(canvas.right - 12, maxX + 40),
        y: Math.min(canvas.bottom - 12, maxY + 40),
      },
    }
  })

/** 离所有节点最远的空白点：粘贴落点与"点空白清选择"共用，避免落点直接压在原节点上。 */
const pickBlank = (page: Page) =>
  page.evaluate(() => {
    const canvas = document.querySelector('[data-testid="workflow-canvas"]')?.getBoundingClientRect()
    if (!canvas) return null
    const cards = [...document.querySelectorAll('.wf-node')].map((node) => node.getBoundingClientRect())
    const blocking = '.vue-flow__node, .ed-toolbar, .ed-panel, .vue-flow__minimap, .vue-flow__controls, .ant-message'
    const blank = (x: number, y: number): boolean => {
      const el = document.elementFromPoint(x, y)
      if (!el || el.closest('.vue-flow__pane') === null) return false
      return el.closest(blocking) === null
    }
    const clearance = (x: number, y: number): number =>
      cards.length === 0
        ? Number.POSITIVE_INFINITY
        : Math.min(
            ...cards.map((r) => {
              const dx = Math.max(r.left - x, 0, x - r.right)
              const dy = Math.max(r.top - y, 0, y - r.bottom)
              return Math.hypot(dx, dy)
            }),
          )

    let best: { x: number; y: number } | null = null
    let bestClearance = -1
    for (let y = canvas.top + 120; y <= canvas.bottom - 40; y += 30) {
      for (let x = canvas.left + 30; x <= canvas.right - 30; x += 30) {
        if (!blank(x, y)) continue
        const clearancePx = clearance(x, y)
        if (clearancePx > bestClearance) {
          bestClearance = clearancePx
          best = { x, y }
        }
      }
    }
    return best === null ? null : { point: best, clearance: Math.round(bestClearance) }
  })

const addBlock = async (page: Page): Promise<void> => {
  await page.locator('[data-testid="toolbar-add"]').click()
  await page.waitForSelector('[data-testid="block-selector"]', { timeout: 10_000 })
  const rows = page.locator('.block-selector__row:not(.is-disabled)')
  expect(await rows.count(), '块选择器里一个可选块都没有——判据在空跑').toBeGreaterThan(0)
  await rows.first().click()
  await page.waitForTimeout(700)
  if ((await page.locator('[data-testid="panel-close"]').count()) > 0) {
    await page.locator('[data-testid="panel-close"]').click()
    await page.waitForTimeout(300)
  }
}

test.use({ permissions: ['clipboard-read', 'clipboard-write'] })

test.describe('画布操作语法 @1440', () => {
  test('框选→复制→粘贴→拖动→撤销 3 次→重做 3 次；输入框 Delete 不删节点；坏剪贴板只报错', async ({
    page,
  }) => {
    test.setTimeout(180_000)
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.goto('/workflow')
    await page.waitForSelector(READY, { timeout: 45_000 })
    await settle(page)
    await page.waitForTimeout(2_500)
    if ((await page.locator('[data-testid="panel-close"]').count()) > 0) {
      await page.locator('[data-testid="panel-close"]').click()
      await page.waitForTimeout(300)
    }

    /* —— 铺三颗节点 —— */
    await addBlock(page)
    await addBlock(page)
    await addBlock(page)
    await expect(page.locator('.wf-node'), '三次添加后画布上不是三颗节点').toHaveCount(3)
    const origin = await readNodes(page)
    expect(new Set(origin.map((row) => row.id)).size, '三颗节点的 id 不唯一').toBe(3)

    /* —— 框选三颗 —— */
    const marquee = await pickMarquee(page)
    expect(marquee, '找不到一块可起手的空白画布——框选判据在空跑').not.toBeNull()
    if (marquee === null) return
    await page.mouse.move(marquee.start.x, marquee.start.y)
    await page.mouse.down()
    await page.mouse.move(
      (marquee.start.x + marquee.end.x) / 2,
      (marquee.start.y + marquee.end.y) / 2,
      { steps: 6 },
    )
    await expect(page.locator('.ed-marquee'), '拖动时没有出现框选矩形').toHaveCount(1)
    await page.mouse.move(marquee.end.x, marquee.end.y, { steps: 6 })
    console.log(
      `MARQUEE start=${JSON.stringify(marquee.start)} end=${JSON.stringify(marquee.end)} liveSelected=${await page.locator('.wf-node.is-selected').count()}`,
    )
    await page.mouse.up()
    await page.waitForTimeout(400)
    /* 松手后浏览器补发的 pane click 不许把框选结果清掉（修前实测 0 颗）。 */
    await expect(page.locator('.wf-node.is-selected'), '框选没把三颗节点都选上').toHaveCount(3)
    await page.screenshot({ path: 'test-results/c4/01-marquee.png' })

    /* —— 复制：真剪贴板里必须是本项目的片段格式 —— */
    await page.keyboard.press('Control+c')
    await page.waitForTimeout(700)
    const copied = await page.evaluate(() => navigator.clipboard.readText())
    expect(copied, '复制后剪贴板里不是 AEGIS 片段').toContain('aegis-workflow-clipboard')
    expect(copied, '复制出来的片段里没带三颗节点').toContain('node_id')

    /* —— 粘贴：落在远离原节点的空白处，再收拢视野把六颗都看全 —— */
    const blank = await pickBlank(page)
    expect(blank, '画布上找不到一块落点空白区').not.toBeNull()
    if (blank === null) return
    await page.mouse.move(blank.point.x, blank.point.y)
    await page.keyboard.press('Control+v')
    await expect(page.locator('.wf-node'), '粘贴后画布上不是六颗节点').toHaveCount(6, { timeout: 15_000 })
    const transformNow = (): Promise<string> =>
      page.evaluate(() => {
        const pane = document.querySelector('.vue-flow__transformationpane')
        return pane ? getComputedStyle(pane).transform : ''
      })
    console.log(`FIT before=${await transformNow()}`)
    /* 「适配画布」是 Mod+1；Shift+1 是 100%（定义表里的两条，别按错）。 */
    await page.keyboard.press('Control+1')
    await page.waitForTimeout(1_800)
    console.log(`FIT after=${await transformNow()}`)
    const offView = await page.evaluate(() => {
      const frame = document.querySelector('[data-testid="workflow-canvas"]')?.getBoundingClientRect()
      if (!frame) return -1
      return [...document.querySelectorAll('.wf-node')].filter((node) => {
        const r = node.getBoundingClientRect()
        return r.left < frame.left - 1 || r.top < frame.top - 1 || r.right > frame.right + 1 || r.bottom > frame.bottom + 1
      }).length
    })
    expect(offView, `适配画布后仍有 ${offView} 颗节点在视野外——拖动判据会落空`).toBe(0)
    await expect(page.locator('.wf-node.is-selected'), '粘贴后选中的不是刚粘出来的三颗').toHaveCount(3)

    const pasted = await readNodes(page)
    const originalIds = new Set(origin.map((row) => row.id))
    const pastedIds = pasted.filter((row) => !originalIds.has(row.id)).map((row) => row.id)
    expect(pastedIds, '粘贴出来的三颗没有独立 id').toHaveLength(3)
    expect(new Set(pasted.map((row) => `${row.x},${row.y}`)).size, '粘贴的落点与原节点重合').toBe(6)
    await page.screenshot({ path: 'test-results/c4/02-pasted.png' })

    /* —— 拖动：多选拖其中一颗，其余两颗跟同一位移 —— */
    const beforeDrag = new Map(pasted.map((row) => [row.id, row]))
    const anchor = page.locator('.wf-node.is-selected').first().locator('.wf-node__head')
    const box = await anchor.boundingBox()
    expect(box, '量不到被拖节点的抬头').not.toBeNull()
    if (box === null) return
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
    await page.mouse.down()
    await page.mouse.move(box.x + box.width / 2 + 140, box.y + box.height / 2 + 80, { steps: 12 })
    await page.mouse.up()
    await page.waitForTimeout(1_000)

    const afterDrag = await readNodes(page)
    const deltas = pastedIds.map((id) => {
      const before = beforeDrag.get(id)
      const after = afterDrag.find((row) => row.id === id)
      expect(before, `${id} 拖动前不在画布上`).toBeDefined()
      expect(after, `${id} 拖动后不在画布上`).toBeDefined()
      return { id, dx: (after?.x ?? Number.NaN) - (before?.x ?? Number.NaN), dy: (after?.y ?? Number.NaN) - (before?.y ?? Number.NaN) }
    })
    console.log(`DRAG deltas=${JSON.stringify(deltas)}`)
    for (const row of deltas) {
      expect(row.dx, `${row.id} 没跟着移动（dx=${row.dx}）`).toBeGreaterThan(10)
      expect(row.dy, `${row.id} 没跟着移动（dy=${row.dy}）`).toBeGreaterThan(10)
    }
    for (const row of deltas.slice(1)) {
      expect(row.dx, `多选拖动位移不一致：${deltas[0].id} dx=${deltas[0].dx} / ${row.id} dx=${row.dx}`).toBeCloseTo(
        deltas[0].dx,
        1,
      )
      expect(row.dy, `多选拖动位移不一致：${deltas[0].id} dy=${deltas[0].dy} / ${row.id} dy=${row.dy}`).toBeCloseTo(
        deltas[0].dy,
        1,
      )
    }
    const untouched = afterDrag.filter((row) => originalIds.has(row.id))
    for (const row of untouched) {
      const before = origin.find((item) => item.id === row.id)
      expect(row.x, `拖动改到了未选中的 ${row.id}`).toBeCloseTo(before?.x ?? Number.NaN, 1)
      expect(row.y, `拖动改到了未选中的 ${row.id}`).toBeCloseTo(before?.y ?? Number.NaN, 1)
    }
    await page.screenshot({ path: 'test-results/c4/03-dragged.png' })

    /* —— 撤销 3 次：拖动 → 粘贴 → 第三颗节点 —— */
    await page.keyboard.press('Control+z')
    await page.waitForTimeout(700)
    let state = await readNodes(page)
    expect(state, '撤销拖动后节点数变了').toHaveLength(6)
    for (const row of pastedIds) {
      const before = beforeDrag.get(row)
      const now = state.find((item) => item.id === row)
      expect(now?.x, `撤销拖动后 ${row} 的 x 没回到原位`).toBeCloseTo(before?.x ?? Number.NaN, 1)
      expect(now?.y, `撤销拖动后 ${row} 的 y 没回到原位`).toBeCloseTo(before?.y ?? Number.NaN, 1)
    }

    await page.keyboard.press('Control+z')
    await page.waitForTimeout(900)
    await expect(page.locator('.wf-node'), '撤销粘贴后画布上不是三颗节点').toHaveCount(3)

    await page.keyboard.press('Control+z')
    await page.waitForTimeout(900)
    await expect(page.locator('.wf-node'), '撤销第三次添加后画布上不是两颗节点').toHaveCount(2)
    await page.screenshot({ path: 'test-results/c4/04-undo3.png' })

    /* —— 重做 3 次：第三颗节点 → 粘贴 → 拖动 —— */
    await page.keyboard.press('Control+y')
    await page.waitForTimeout(900)
    await expect(page.locator('.wf-node'), '重做第三次添加后画布上不是三颗节点').toHaveCount(3)

    await page.keyboard.press('Control+y')
    await page.waitForTimeout(1_000)
    await expect(page.locator('.wf-node'), '重做粘贴后画布上不是六颗节点').toHaveCount(6)

    await page.keyboard.press('Control+y')
    await page.waitForTimeout(1_000)
    const redone = await readNodes(page)
    for (const row of pastedIds) {
      const dragged = afterDrag.find((item) => item.id === row)
      const now = redone.find((item) => item.id === row)
      expect(now?.x, `重做拖动后 ${row} 的 x 没回到拖拉后的位置`).toBeCloseTo(dragged?.x ?? Number.NaN, 1)
      expect(now?.y, `重做拖动后 ${row} 的 y 没回到拖拉后的位置`).toBeCloseTo(dragged?.y ?? Number.NaN, 1)
    }
    await page.screenshot({ path: 'test-results/c4/05-redo3.png' })

    /* —— 输入框里按 Delete：那是删字符，不是删节点 —— */
    const countBeforeInput = await nodeCount(page)
    await page.locator('.wf-node').first().click({ position: { x: 40, y: 14 } })
    const name = page.locator('[data-testid="editor-panel"] .wf-inspector__input').first()
    await expect(name, '选中节点后右栏没有参数输入框——判据在空跑').toBeVisible({ timeout: 10_000 })
    await name.click()
    expect(
      await page.evaluate(() => document.activeElement?.className ?? ''),
      '焦点不在参数输入框上——Delete 判据在空跑',
    ).toContain('wf-inspector__input')
    await page.keyboard.press('Delete')
    await page.waitForTimeout(400)
    expect(await nodeCount(page), '在输入框里按 Delete 把节点删了').toBe(countBeforeInput)
    await expect(page.locator('.wf-node'), '输入框里按 Delete 后节点数变了').toHaveCount(countBeforeInput)
    await page.screenshot({ path: 'test-results/c4/06-delete-in-input.png' })

    /* —— 坏剪贴板：明确报错，图一动不动 —— */
    const idsBeforeJunk = (await readNodes(page)).map((row) => row.id).sort()
    await page.evaluate(() => navigator.clipboard.writeText('这不是 JSON'))
    const clear = await pickBlank(page)
    if (clear !== null) await page.mouse.click(clear.point.x, clear.point.y)
    await page.waitForTimeout(300)
    await page.keyboard.press('Control+v')
    const notice = page.locator('.ant-message-notice').last()
    await expect(notice, '坏剪贴板没弹出错误提示').toContainText('剪贴板内容不是合法 JSON', { timeout: 10_000 })
    /* 提示条在动画入场的头几十毫秒是透明的：只断言"DOM 里有字"会拍到一张看不见提示的截图。 */
    await expect(notice, '错误提示条在 DOM 里但没显示出来').toBeVisible()
    await page.waitForTimeout(300)
    expect(await nodeCount(page), '坏剪贴板粘贴改动了画布').toBe(countBeforeInput)
    expect((await readNodes(page)).map((row) => row.id).sort(), '坏剪贴板粘贴改动了节点集合').toEqual(idsBeforeJunk)
    await page.screenshot({ path: 'test-results/c4/07-invalid-paste.png' })

    /* 正对照：同一条 Delete 在画布上真的会删——证明上面那条是被输入框守卫挡住的。
       先关面板：它盖住画布右侧，节点若在下面会被 Playwright 判成不可点。 */
    if ((await page.locator('[data-testid="panel-close"]').count()) > 0) {
      await page.locator('[data-testid="panel-close"]').click()
      await page.waitForTimeout(300)
    }
    await page.locator('.wf-node').first().click({ position: { x: 40, y: 14 } })
    await page.waitForTimeout(300)
    await page.keyboard.press('Delete')
    await page.waitForTimeout(600)
    expect(await nodeCount(page), '画布上按 Delete 没删掉选中的节点（正对照失效）').toBe(countBeforeInput - 1)
    await page.keyboard.press('Control+z')
    await page.waitForTimeout(800)
    expect(await nodeCount(page), '撤销删除后节点没回来').toBe(countBeforeInput)
  })
})
