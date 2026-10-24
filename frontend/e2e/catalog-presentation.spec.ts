/**
 * 批次 D1 真机验收：节点目录不可达时的降级形态（构建产物 + vite preview）。
 *
 * 预览服务器没有 `/api` 代理，正好就是"现场后端没起来"的那一档：
 * 目录拉不到时，画布必须继续用前端镜像把图渲染出来，横幅给出原因与重试，
 * 而不是整页空白或把节点画成方块。判据里的图标路径数与强调色都从真 DOM 上量。
 */
import { expect, test, type Page } from '@playwright/test'

const READY = '[data-testid="editor-header"]'

/** 主题令牌的六种强调色；getComputedStyle 统一回报 rgb() 写法。 */
const ACCENTS = [
  'rgb(37, 99, 235)',
  'rgb(124, 58, 237)',
  'rgb(15, 118, 110)',
  'rgb(180, 83, 9)',
  'rgb(190, 18, 60)',
  'rgb(71, 85, 105)',
]

const addBlock = async (page: Page): Promise<void> => {
  await page.locator('[data-testid="toolbar-add"]').click()
  await page.waitForSelector('[data-testid="block-selector"]', { timeout: 10_000 })
  await page.locator('.block-selector__row:not(.is-disabled)').first().click()
  await page.waitForTimeout(500)
}

test('目录不可达：本地镜像仍画出节点与图标，横幅可重试，强调色真落到 DOM', async ({ page }) => {
  test.setTimeout(120_000)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.route('**/api/**', (route) => route.abort())
  await page.goto('/workflow')
  await page.waitForSelector(READY, { timeout: 45_000 })

  const banner = page.locator('[data-testid="catalog-retry"]')
  await expect(banner, '目录拉不到时应给出错误横幅').toBeVisible()
  await expect(banner).toContainText('节点目录')
  /* 横幅必须真的在最上层：开页 toast 曾浮在同一位置，把"重试"整块盖住。 */
  const occluded = await banner.evaluate((el) => {
    const rect = el.getBoundingClientRect()
    const top = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)
    return top === null || !el.contains(top)
  })
  expect(occluded, '目录横幅被别的东西盖住了').toBe(false)

  /* 横幅自己的几何：不出屏、不裁字。800px 以下媒体查询会把整叠挪到工具条下方。 */
  const bannerGeometry = await page.evaluate(() =>
    [...document.querySelectorAll('.ed-banner')].map((el) => {
      const rect = el.getBoundingClientRect()
      return {
        text: el.textContent?.trim().slice(0, 30) ?? '',
        left: rect.left,
        right: rect.right,
        clipped: el.scrollWidth > el.clientWidth + 1,
      }
    }),
  )
  for (const row of bannerGeometry) {
    expect(row.left, `${row.text} 溢出左边`).toBeGreaterThanOrEqual(0)
    expect(row.right, `${row.text} 溢出右边`).toBeLessThanOrEqual(1440)
    expect(row.clipped, `${row.text} 的文字被裁`).toBe(false)
  }

  /* 块选择器：18 类可上面板的节点全部在，且每行都有可画的图标路径。 */
  await page.locator('[data-testid="toolbar-add"]').click()
  await page.waitForSelector('[data-testid="block-selector"]', { timeout: 10_000 })
  const rows = page.locator('.block-selector__row')
  await expect(rows, '块选择器应列出 18 类可上面板的节点').toHaveCount(18)
  const iconless = await page.evaluate(
    () =>
      [...document.querySelectorAll('.block-selector__row')].filter(
        (row) => (row.querySelectorAll('svg path').length ?? 0) === 0,
      ).length,
  )
  expect(iconless, '有块选择器条目缺图标').toBe(0)
  await page.screenshot({ path: 'test-results/d1/offline-catalog.png' })
  await page.keyboard.press('Escape')

  /* 画三颗节点：卡片必须画得出来，图标与强调色都来自新令牌。 */
  await addBlock(page)
  await addBlock(page)
  await addBlock(page)
  await expect(page.locator('.wf-node'), '离线时连本地节点都画不出来').toHaveCount(3)

  const cards = await page.evaluate(() =>
    [...document.querySelectorAll('.wf-node')].map((node) => {
      const icon = node.querySelector('.wf-node__icon')
      return {
        type: node.querySelector('.wf-node__type')?.textContent ?? '',
        paths: icon?.querySelectorAll('svg path').length ?? 0,
        color: icon ? getComputedStyle(icon).color : '',
        known: !node.classList.contains('is-unknown'),
      }
    }),
  )
  for (const card of cards) {
    expect(card.known, `${card.type} 用了镜像里没有的类型`).toBe(true)
    expect(card.paths, `${card.type} 的图标没有路径`).toBeGreaterThan(0)
    expect(ACCENTS, `${card.type} 的强调色 ${card.color} 不在令牌表里`).toContain(card.color)
  }

  /* 重试：仍在离线，横幅不许消失，也不许把图弄丢。 */
  const before = await page.locator('.wf-node').count()
  await banner.locator('.ed-banner__retry').click()
  await expect(banner, '离线时重试失败后横幅不该消失').toBeVisible({ timeout: 10_000 })
  await expect(page.locator('.ant-message')).toContainText('节点目录', { timeout: 10_000 })
  await expect(page.locator('.wf-node'), '重试把画布节点弄丢了').toHaveCount(before)
  await page.screenshot({ path: 'test-results/d1/offline-catalog-retry.png' })
})

test('窄屏 900：两幅横幅挪到工具条下方，不出屏、不裁字', async ({ page }) => {
  test.setTimeout(120_000)
  await page.setViewportSize({ width: 900, height: 900 })
  await page.route('**/api/**', (route) => route.abort())
  await page.goto('/workflow')
  await page.waitForSelector(READY, { timeout: 45_000 })
  await expect(page.locator('[data-testid="catalog-retry"]')).toBeVisible()

  const rows = await page.evaluate(() =>
    [...document.querySelectorAll('.ed-banner')].map((el) => {
      const rect = el.getBoundingClientRect()
      return {
        text: el.textContent?.trim().slice(0, 24) ?? '',
        top: rect.top,
        left: rect.left,
        right: rect.right,
        clipped: el.scrollWidth > el.clientWidth + 1,
      }
    }),
  )
  expect(rows.length, '两处开页错误应各有一条横幅').toBeGreaterThanOrEqual(2)
  for (const row of rows) {
    expect(row.top, `${row.text} 顶到页头里`).toBeGreaterThanOrEqual(56)
    expect(row.left, `${row.text} 溢出左边`).toBeGreaterThanOrEqual(0)
    expect(row.right, `${row.text} 溢出右边`).toBeLessThanOrEqual(900)
    expect(row.clipped, `${row.text} 的文字被裁`).toBe(false)
  }
  await page.screenshot({ path: 'test-results/d1/offline-catalog-900.png' })
})
