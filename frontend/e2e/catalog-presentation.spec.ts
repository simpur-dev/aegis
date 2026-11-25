/**
 * 批次 D1 真机验收：节点目录不可达时的降级形态（构建产物 + vite preview）。
 *
 * 预览服务器没有 `/api` 代理，正好就是"现场后端没起来"的那一档：
 * 目录拉不到时，画布必须继续用前端镜像把图渲染出来，横幅给出原因与重试，
 * 而不是整页空白或把节点画成方块。判据里的图标路径数与强调色都从真 DOM 上量。
 */
import { expect, test, type Page } from '@playwright/test'
import type { ProcessingDetail, ProcessingStage } from '../src/api/types'

const READY = '[data-testid="editor-header"]'

const ACCENT_SLOTS = ['blue', 'violet', 'teal', 'amber', 'rose', 'slate']

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
  const accents = await page.evaluate((slots) => {
    const probe = document.createElement('span')
    document.body.append(probe)
    const colors = slots.map((slot) => {
      probe.style.color = `var(--aeg-color-editor-accent-${slot})`
      return getComputedStyle(probe).color
    })
    probe.remove()
    return colors
  }, ACCENT_SLOTS)
  for (const card of cards) {
    expect(card.known, `${card.type} 用了镜像里没有的类型`).toBe(true)
    expect(card.paths, `${card.type} 的图标没有路径`).toBeGreaterThan(0)
    expect(accents, `${card.type} 的强调色 ${card.color} 不在令牌表里`).toContain(card.color)
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

test('手机底部有三个工作区，助手独立放在顶栏', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 740 })
  await page.route('**/api/v1/alerts**', (route) =>
    route.fulfill({ json: { count: 0, items: [], persistent: true, escalation_minutes: 15 } }),
  )
  await page.goto('/dashboard')
  const nav = page.getByRole('navigation', { name: '主导航' })
  await expect(nav.getByRole('link')).toHaveCount(3)
  await expect(nav.getByRole('link', { name: '值班处置' })).toHaveAttribute('aria-current', 'page')
  const assistant = page.getByTestId('mobile-assistant')
  await expect(assistant).toBeVisible()
  const frame = await nav.evaluate((el) => {
    const bar = el.getBoundingClientRect()
    const main = document.querySelector('main')!.getBoundingClientRect()
    return {
      bottomGap: innerHeight - bar.bottom,
      contentGap: bar.top - main.bottom,
      linkWidths: Array.from(el.querySelectorAll('a'), (link) => link.getBoundingClientRect().width),
    }
  })
  expect(frame.bottomGap).toBeCloseTo(0, 0)
  expect(frame.contentGap).toBeCloseTo(0, 0)
  expect(Math.min(...frame.linkWidths)).toBeGreaterThanOrEqual(44)

  await nav.getByRole('link', { name: '数据处理' }).click()
  await expect(page).toHaveURL(/\/data-processing$/)
  const data = page.getByRole('navigation', { name: '数据处理' })
  await expect(data.getByRole('link')).toHaveText(['处理流水线', '监测数据', '指标量测'])
  await data.getByRole('link', { name: '监测数据' }).click()
  await expect(page).toHaveURL(/\/monitor$/)
  await nav.getByRole('link', { name: '值班处置' }).click()
  const duty = page.getByRole('navigation', { name: '值班处置' })
  await expect(duty.getByRole('link')).toHaveText(['态势总览', '告警与任务'])
  await duty.getByRole('link', { name: '告警与任务' }).click()
  await expect(nav.getByRole('link', { name: '值班处置' })).toHaveAttribute('aria-current', 'location')

  await page.getByTestId('assistant-dock').click()
  const drawer = page.getByTestId('assistant-drawer')
  await expect(drawer).toBeVisible()
  const drawerBox = (await drawer.boundingBox())!
  expect(drawerBox.y + drawerBox.height).toBeCloseTo((await nav.boundingBox())!.y, 0)
  await drawer.getByRole('button', { name: '收起助手' }).click()
  await page.getByRole('link', { name: '处置演练', exact: true }).click()
  await expect(page).toHaveURL(/\/monitor#drill$/)
  await expect(nav.getByRole('link', { name: '值班处置' })).toHaveAttribute('aria-current', 'location')
  await nav.getByRole('link', { name: '多智能体研究' }).click()
  await expect(page.getByTestId('editor-header')).toBeVisible()
  await expect(nav.getByRole('link', { name: '多智能体研究' })).toHaveAttribute('aria-current', 'page')
  await page.setViewportSize({ width: 390, height: 844 })
  await assistant.click()
  await expect(assistant).toHaveAttribute('aria-current', 'page')
  await expect(nav.locator('[aria-current]')).toHaveCount(0)
  const send = page.getByTestId('send')
  await expect(send).toBeVisible()
  for (const size of [{ width: 390, height: 844 }, { width: 320, height: 740 }]) {
    await page.setViewportSize(size)
    await expect.poll(async () => {
      const sendBox = (await send.boundingBox())!
      return (await nav.boundingBox())!.y - sendBox.y - sendBox.height
    }).toBeGreaterThan(0)
  }
  await page.setViewportSize({ width: 1440, height: 900 })
  await expect(page.getByTestId('mobile-nav')).toBeHidden()
  await expect(assistant).toBeHidden()
  await expect(page.locator('.topnav')).toBeVisible()
})

test('桌面高亮跟随四个编号入口，并尊重减少动态效果', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.route('**/api/v1/alerts**', (route) =>
    route.fulfill({ json: { count: 0, items: [], persistent: true, escalation_minutes: 15 } }),
  )
  await page.goto('/monitor#drill')
  const nav = page.getByRole('navigation', { name: '主导航' })
  const active = nav.locator('[aria-current]')
  const highlight = nav.locator('.topnav__highlight')
  await expect(nav.getByRole('link')).toHaveCount(4)
  await expect(nav.locator('.topnav__number')).toHaveText(['01', '02', '03', '04'])
  await expect(active).toHaveAccessibleName('值班处置')
  const alignment = () => highlight.evaluate((el) => {
    const frame = el.getBoundingClientRect()
    const tab = el.parentElement!.querySelector('[aria-current]')!.getBoundingClientRect()
    return Math.max(Math.abs(frame.left - tab.left), Math.abs(frame.width - tab.width))
  })
  await expect.poll(alignment).toBeLessThan(1)

  const data = nav.getByRole('link', { name: '数据处理' })
  await data.focus()
  await data.press('Enter')
  await expect(page).toHaveURL(/\/data-processing$/)
  await expect(active).toHaveAccessibleName('数据处理')
  await expect.poll(alignment).toBeLessThan(1)

  await page.emulateMedia({ reducedMotion: 'reduce' })
  await nav.getByRole('link', { name: '助手' }).click()
  await expect(active).toHaveAccessibleName('助手')
  expect(await highlight.evaluate((el) => el.getAnimations().length)).toBe(0)
  await expect.poll(alignment).toBeLessThan(1)

  await nav.getByRole('link', { name: '多智能体研究' }).click()
  await expect(page.getByTestId('editor-header')).toBeVisible()
  await expect(nav).toBeVisible()
  await expect(active).toHaveAccessibleName('多智能体研究')
})

test('数据处理显示同一批次的原始输入、拒绝样本与数据库提交结果', async ({ page }) => {
  await page.route('**/api/v1/alerts**', (route) =>
    route.fulfill({ json: { count: 0, items: [], persistent: true, escalation_minutes: 15 } }),
  )
  const reading = { station_id: 'QA-540121-01', metric: 'rain_10min', value: 18.7 }
  const step = (key: ProcessingStage['key'], overrides: Partial<ProcessingStage> = {}): ProcessingStage => ({
    key, status: 'ok', duration_ms: 2, inputs: {}, results: {}, input_samples: [], result_samples: [], issues: [], ...overrides,
  })
  const abnormal: ProcessingDetail = {
    id: 'batch-a', trace_id: 'trace-a', started_at: '2026-11-24T05:20:00Z', status: 'partial',
    duration_ms: 5, sources: 1, readings: 1, published: 1, source_details: [],
    stages: [
      step('collect'),
      step('normalize', {
        status: 'partial', inputs: { raw_items: 2 }, results: { normalized: 1 },
        input_samples: [{ ...reading, value: '18.7' }], result_samples: [reading],
        issues: [{
          code: 'invalid_value', message: '观测值不是有限数值', count: 1,
          samples: [{ station_id: 'QA-540221-01', metric: 'rain_10min', value: 'NaN' }],
        }],
      }),
      step('store', { results: { queryable: 1 } }),
      step('durability', { inputs: { readings: 1, async: true }, results: { queued: 1, committed: 1, pending: 0 } }),
      step('publish', { results: { published: 1 } }),
      step('analytics', { status: 'skipped', results: { enabled: false } }),
    ],
  }
  const normal = { ...abnormal, id: 'batch-b', trace_id: 'trace-b', status: 'ok', started_at: '2026-11-24T05:19:00Z' }
  await page.route('**/api/v1/telemetry/processing?*', (route) =>
    route.fulfill({ json: { capacity: 120, sources: ['weather_api'], items: [abnormal, normal] } }),
  )
  await page.route('**/api/v1/telemetry/processing/*', (route) =>
    route.fulfill({ json: route.request().url().endsWith('/batch-a') ? abnormal : normal }),
  )
  await page.goto('/data-processing')
  const stages = page.getByRole('list', { name: '处理步骤' })
  await stages.getByRole('button', { name: /标准化/ }).click()
  await page.getByText('查看输入样本').click()
  await expect(page.locator('.stage-detail').getByRole('cell', { name: '18.7' })).toBeVisible()
  await page.getByText('查看异常样本').click()
  await expect(page.getByRole('cell', { name: 'NaN' })).toBeVisible()
  await stages.getByRole('button', { name: /数据库提交/ }).click()
  await expect(page.getByText('已提交', { exact: true })).toBeVisible()
  await expect(page.locator('.stage-detail').getByRole('cell', { name: 'NaN' })).toHaveCount(0)

  await page.locator('.batch-filter').getByRole('button', { name: '有异常' }).click()
  await expect(page.locator('.batch-row')).toHaveCount(1)
  await page.setViewportSize({ width: 320, height: 740 })
  await expect(page.getByRole('combobox', { name: '接入批次' })).toBeVisible()
  await expect(stages.getByRole('button', { name: /事件投递/ })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(320)
})
