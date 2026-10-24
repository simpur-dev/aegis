/**
 * 批次 D3 真机验收：19 类节点的专属表单与变量选择器（构建产物 + vite preview，目录不可达那一档）。
 *
 * 为什么要真机：这批的东西全是"手感"——下拉里有没有那条没连线的上游、点变量按钮是不是真的
 * 把引用插在光标处、清空一格红字是不是就长在它下面。jsdom 能证明组件在跑，证不了
 * 400px 宽的浮层里这张表还点得到。
 */
import { expect, test, type Page } from '@playwright/test'

const READY = '[data-testid="editor-header"]'

const addBlock = async (page: Page, query: string): Promise<void> => {
  await page.locator('[data-testid="toolbar-add"]').click()
  await page.waitForSelector('[data-testid="block-selector"]', { timeout: 10_000 })
  await page.getByPlaceholder('搜索节点').fill(query)
  await page.waitForTimeout(250)
  const rows = page.locator('.block-selector__row:not(.is-disabled)')
  await expect(rows, `搜 "${query}" 一个可选块都没有——判据在空跑`).not.toHaveCount(0)
  await rows.first().click()
  await page.waitForTimeout(600)
}

/** 画布上按显示名找节点 ID（ID 由 store 生成，用例不许把它写死）。 */
const nodeIdOf = (page: Page, label: string): Promise<string> =>
  page.evaluate((want) => {
    const cards = [...document.querySelectorAll('.vue-flow__node')]
    const hit = cards.find((card) => (card.querySelector('.wf-node__title')?.textContent ?? '').includes(want))
    return hit?.getAttribute('data-id') ?? ''
  }, label)

const selectNode = async (page: Page, label: string): Promise<void> => {
  await page.locator('.wf-node', { hasText: label }).first().click()
  await page.waitForTimeout(400)
}

test('阈值条件逐行可编；写残的那一条就长在那一格下面', async ({ page }) => {
  test.setTimeout(150_000)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.route('**/api/**', (route) => route.abort())
  await page.goto('/workflow')
  await page.waitForSelector(READY, { timeout: 45_000 })

  await addBlock(page, '阈值判断')
  const editor = page.locator('[data-testid="rule-editor-conditions"]')
  await expect(editor, '阈值判断没铺出条件表').toBeVisible()
  await expect(page.locator('[data-testid="condition-metric-0"]')).toHaveValue('rain_10min')
  await expect(page.locator('[data-testid="node-issues-clean"]')).toBeVisible()

  await page.locator('[data-testid="condition-threshold-0"]').fill('50')
  const aggs = page.locator('[data-testid="condition-agg-0"] option')
  await expect(aggs, '聚合候选不是后端认识的那三种').toHaveCount(3)

  await page.locator('[data-testid="rule-add-conditions"]').click()
  await expect(page.locator('[data-testid="condition-metric-1"]')).toBeVisible()
  await page.locator('[data-testid="rule-remove-1"]').click()
  await expect(page.locator('[data-testid="condition-metric-1"]')).toHaveCount(0)

  /* 清空指标 → 面板当场说"缺少指标名"，而不是等发布吃一趟 422。 */
  await page.locator('[data-testid="condition-metric-0"]').fill('')
  await expect(page.locator('[data-testid="error-conditions"]')).toContainText('缺少指标名')
  await expect(page.locator('[data-testid="node-issues"]')).toContainText('1 处会被发布拦下')
  await expect(page.locator('[data-testid="node-issues"]')).toContainText('第 1 条条件缺少指标名')
  await page.screenshot({ path: 'test-results/d3/threshold-conditions.png' })
})

test('通报内容清空后必填红字落在它下面；上游候选与变量都只认真祖先', async ({ page }) => {
  test.setTimeout(150_000)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.route('**/api/**', (route) => route.abort())
  await page.goto('/workflow')
  await page.waitForSelector(READY, { timeout: 45_000 })

  await addBlock(page, '数据接入')
  const fetchId = await nodeIdOf(page, '数据接入')
  expect(fetchId, '找不到数据接入那颗').not.toBe('')
  await page.locator('[data-testid="panel-close"]').click()
  await addBlock(page, '事件通报')
  await page.locator('[data-testid="panel-close"]').click()

  /* 拖一条 fetch → notify 的连线：变量选择器的候选完全由连线决定。 */
  const source = await page.locator(`[data-id="${fetchId}"] .vue-flow__handle.source`).boundingBox()
  const notifyBox = await page.locator('.wf-node', { hasText: '事件通报' }).first().boundingBox()
  expect(source, '数据接入没有出线圆点').not.toBeNull()
  expect(notifyBox, '事件通报量不到盒子').not.toBeNull()
  if (source && notifyBox) {
    const target = { x: notifyBox.x + 8, y: notifyBox.y + 16 }
    await page.mouse.move(source.x + source.width / 2, source.y + source.height / 2)
    await page.mouse.down()
    await page.mouse.move(target.x, target.y, { steps: 12 })
    await page.mouse.up()
    await page.waitForTimeout(700)
  }
  await expect(page.locator('.vue-flow__edge-path'), '拖线后没有生成连线').toHaveCount(1)

  await selectNode(page, '事件通报')
  await page.locator('[data-testid="panel-node"]').click()
  await expect(page.locator('[data-testid="node-issues-clean"]')).toBeVisible()

  /* 1) 清空必填：红字就长在正文那一格下面。 */
  await page.locator('[data-testid="field-text"] textarea').fill('')
  await expect(page.locator('[data-testid="error-text"]')).toContainText('必填')
  await expect(page.locator('[data-testid="node-issues"]')).toContainText('通报内容')
  await page.screenshot({ path: 'test-results/d3/notify-required.png' })

  /* 2) 点变量：只列得出祖先的输出键，插进光标处。 */
  await page.locator('[data-testid="field-text"] textarea').fill('本轮共 条读数')
  await page.locator('[data-testid="field-text"] textarea').evaluate((el) => {
    const area = el as HTMLTextAreaElement
    area.focus()
    area.setSelectionRange(3, 3)
  })
  await page.locator('[data-testid="variable-toggle"]').click()
  const picker = page.locator('[data-testid="variable-picker"]')
  await expect(picker).toBeVisible()
  const notifyId = await nodeIdOf(page, '事件通报')
  await expect(picker.locator(`[data-testid="var-${fetchId}-rows"]`)).toBeVisible()
  await expect(
    picker.locator(`[data-testid="var-${notifyId}-notified"]`),
    '自己不能是自己的上游',
  ).toHaveCount(0)
  await picker.locator(`[data-testid="var-${fetchId}-rows"]`).click()
  await expect(page.locator('[data-testid="field-text"] textarea')).toHaveValue(
    `本轮共{{#${fetchId}.rows#}} 条读数`,
  )
  await expect(page.locator('[data-testid="field-text"]')).toContainText('已引用 1 处')
  /* 引用是从真实祖先点出来的，整图校验不该冒出新的红字。 */
  await expect(page.locator('[data-testid="node-issues-clean"]')).toBeVisible()
  await page.screenshot({ path: 'test-results/d3/variable-picker.png' })

  /* 3) 手敲一只不存在的引用：面板当场说，不等发布。 */
  await page.locator('[data-testid="field-text"] textarea').fill('见 {{#nobody.rows#}}')
  await expect(page.locator('[data-testid="error-text"]')).toContainText('不是本节点的上游')

  /* 4) 上游那格：没连线时只有"不指定"，连上之后才点得到。 */
  await page.locator('[data-testid="panel-close"]').click()
  await addBlock(page, '阈值判断')
  await expect(page.locator('[data-testid="node-ref-empty"]')).toContainText('先在画布上连线')
  const judgeBox = await page.locator('.wf-node', { hasText: '阈值判断' }).first().boundingBox()
  expect(judgeBox, '阈值判断量不到盒子').not.toBeNull()
  if (source && judgeBox) {
    await page.mouse.move(source.x + source.width / 2, source.y + source.height / 2)
    await page.mouse.down()
    await page.mouse.move(judgeBox.x + 8, judgeBox.y + 16, { steps: 12 })
    await page.mouse.up()
    await page.waitForTimeout(700)
  }
  await selectNode(page, '阈值判断')
  await page.locator('[data-testid="panel-node"]').click()
  const upstream = page.locator('[data-testid="field-upstream"] select')
  // 收起状态的 <option> 在 Playwright 眼里恒为 hidden，只能按存在与选中值判。
  await expect(upstream.locator('option', { hasText: fetchId })).toHaveCount(1)
  await upstream.selectOption(fetchId)
  await expect(upstream).toHaveValue(fetchId)
  await expect(page.locator('[data-testid="node-issues-clean"]')).toBeVisible()
  await page.screenshot({ path: 'test-results/d3/node-ref-upstream.png' })
})
