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

  /* 拖一条 fetch → notify 的连线：变量选择器的候选完全由连线决定。落点取 handle 自己的盒子。 */
  const notifyIdForEdge = await nodeIdOf(page, '事件通报')
  const source = await page.locator(`[data-id="${fetchId}"] .vue-flow__handle.source`).boundingBox()
  const target = await page.locator(`[data-id="${notifyIdForEdge}"] .vue-flow__handle.target`).boundingBox()
  expect(source, '数据接入没有出线圆点').not.toBeNull()
  expect(target, '事件通报没有入线圆点').not.toBeNull()
  if (source && target) {
    await page.mouse.move(source.x + source.width / 2, source.y + source.height / 2)
    await page.mouse.down()
    await page.mouse.move(target.x + target.width / 2, target.y + target.height / 2, { steps: 12 })
    await page.mouse.up()
    await page.waitForTimeout(800)
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
  await expect(picker.locator(`[data-testid="var-${notifyId}-notified"]`), '自己不能是自己的上游').toHaveCount(0)
  await picker.locator(`[data-testid="var-${fetchId}-rows"]`).click()
  await expect(page.locator('[data-testid="field-text"] textarea')).toHaveValue(`本轮共{{#${fetchId}.rows#}} 条读数`)
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

/**
 * 批次 D4 真机验收（一）：**18 类可上面板的节点逐类走一遍**，每类都要在面板里铺出自己的字段。
 *
 * `chain_stage` 不在这里——它是内部原语，面板按 `internal` 滤掉了（拖出来必然跑不起来），
 * 它那一格（段名下拉）由组件单测覆盖。
 * 判据取"字段格数 + 该类的专属编辑器在不在"，不取像素：像素归 `ui-overlap` 那套几何门禁。
 */
const FORMS: { query: string; fields: number; marker: string }[] = [
  { query: '开始', fields: 1, marker: '[data-testid="var-add"]' },
  { query: '结束', fields: 0, marker: '.wf-fields__empty' },
  { query: '数据接入', fields: 3, marker: '[data-testid="field-region_code"] input' },
  { query: '外部接口', fields: 3, marker: '[data-testid="variable-toggle"]' },
  { query: '设备联动', fields: 3, marker: '[data-testid="variable-toggle"]' },
  { query: '事件通报', fields: 2, marker: '[data-testid="variable-toggle"]' },
  { query: '阈值判断', fields: 3, marker: '[data-testid="rule-editor-conditions"]' },
  { query: '条件分支', fields: 3, marker: '[data-testid="rule-editor-rules"]' },
  { query: '并行汇聚', fields: 1, marker: '[data-testid="node-ref-empty"]' },
  { query: '延时等待', fields: 1, marker: '[data-testid="field-seconds"] input[type="number"]' },
  { query: '反馈采集', fields: 1, marker: '[data-testid="field-wait_seconds"] input' },
  { query: '灾种识别', fields: 1, marker: '[data-testid="field-extra"] textarea' },
  { query: '风险定级', fields: 2, marker: '[data-testid="node-ref-empty"]' },
  { query: '态势推演', fields: 2, marker: '[data-testid="node-ref-empty"]' },
  { query: '预警生成', fields: 1, marker: '[data-testid="field-extra"] textarea' },
  { query: '预警发布', fields: 1, marker: '[data-testid="list-add"]' },
  { query: '规则降级', fields: 1, marker: '[data-testid="field-risk_level"] input' },
  { query: '人工核签', fields: 2, marker: '[data-testid="list-add"]' },
]

test('18 类节点逐类在面板里铺出自己的表单', async ({ page }) => {
  test.setTimeout(240_000)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.route('**/api/**', (route) => route.abort())
  await page.goto('/workflow')
  await page.waitForSelector(READY, { timeout: 45_000 })

  const seen: string[] = []
  for (const form of FORMS) {
    await addBlock(page, form.query)
    const fields = page.locator('[data-testid^="field-"]')
    // 面板每次只画当前选中那颗：字段格数对得上，才说明这一类没有被别人的表单顶替。
    await expect(fields, `${form.query} 的字段格数不对`).toHaveCount(form.fields)
    if (form.fields === 0) {
      await expect(page.locator(form.marker)).toBeVisible()
    } else {
      await expect(page.locator(form.marker).first(), `${form.query} 缺自己的编辑器`).toBeVisible()
      /* 每类都要能真的改一格：空集合那一格先给"新增"按钮；
         而 `upstream` 这类在没有入边时刻意不给控件（只能先去连线），那就得有明确的空态说明。 */
      const first = fields.first()
      const controls = await first.locator('input, textarea, select, button').count()
      const hints = await first.locator('[data-testid="node-ref-empty"]').count()
      expect(controls + hints, `${form.query} 第一格既没有可编辑控件，也没说清为什么没有`).toBeGreaterThan(0)
    }
    await expect(page.locator('[data-testid^="node-issues"]').first()).toBeVisible()
    seen.push(form.query)
    await page.locator('[data-testid="panel-close"]').click()
  }
  expect(seen).toHaveLength(18)
  await expect(page.locator('.wf-node')).toHaveCount(18)
  await page.screenshot({ path: 'test-results/d4/all-forms.png' })
})

test('深色 + 900px 窄档：表单与变量浮层读得清、不出屏', async ({ page }) => {
  test.setTimeout(150_000)
  await page.addInitScript(() => localStorage.setItem('aegis.theme', 'dark'))
  await page.setViewportSize({ width: 900, height: 900 })
  await page.route('**/api/**', (route) => route.abort())
  await page.goto('/workflow')
  await page.waitForSelector(READY, { timeout: 45_000 })

  await addBlock(page, '数据接入')
  const fetchId = await nodeIdOf(page, '数据接入')
  await page.locator('[data-testid="panel-close"]').click()
  await addBlock(page, '事件通报')
  const notifyId = await nodeIdOf(page, '事件通报')
  /* 先关面板再拖线：900 宽时那 400px 面板正压在画布右侧，不关就把落点盖掉了。 */
  await page.locator('[data-testid="panel-close"]').click()
  // 落点取 handle 自己的盒子：按卡片边角猜坐过两次事故（窄屏两张卡挨得近，猜的落点落在别人的卡上）。
  const source = await page.locator(`[data-id="${fetchId}"] .vue-flow__handle.source`).boundingBox()
  const target = await page.locator(`[data-id="${notifyId}"] .vue-flow__handle.target`).boundingBox()
  expect(source, '数据接入没有出线圆点').not.toBeNull()
  expect(target, '事件通报没有入线圆点').not.toBeNull()
  if (source && target) {
    await page.mouse.move(source.x + source.width / 2, source.y + source.height / 2)
    await page.mouse.down()
    await page.mouse.move(target.x + target.width / 2, target.y + target.height / 2, { steps: 12 })
    await page.mouse.up()
    await page.waitForTimeout(700)
  }
  await expect(page.locator('.vue-flow__edge-path'), '拖线没成功，后面的变量判据就是空跑').toHaveCount(1)
  await selectNode(page, '事件通报')
  await page.locator('[data-testid="panel-node"]').click()
  await page.locator('[data-testid="variable-toggle"]').click()
  await expect(page.locator('[data-testid="variable-picker"]')).toBeVisible()

  /* 浮层不出屏：900 宽时面板占满右侧，变量列表的每颗按钮都还得点得到。 */
  const boxes = await page.evaluate(() => {
    const inside = (el: Element): boolean => {
      const rect = el.getBoundingClientRect()
      return rect.left >= 0 && rect.right <= window.innerWidth && rect.width > 0
    }
    return {
      theme: document.documentElement.dataset.theme ?? '',
      offscreen: [...document.querySelectorAll('.wf-varpicker__key, .wf-field__label')].filter((el) => !inside(el))
        .length,
      keys: document.querySelectorAll('.wf-varpicker__key').length,
    }
  })
  expect(boxes.theme, '深色档没落到 DOM 上').toBe('dark')
  expect(boxes.keys, '变量选择器一颗键都没铺出来——判据在空跑').toBeGreaterThan(0)
  expect(boxes.offscreen, '窄屏深色下有元素被推出视口').toBe(0)

  /* 深色下的可读性：字段文字不能与面板底同档（白底浅字是 B 批踩过的坑，反向也一样）。 */
  const contrast = await page.evaluate(() => {
    const parse = (color: string): [number, number, number] => {
      const hit = /rgba?\((\d+), (\d+), (\d+)/.exec(color)
      return hit ? [Number(hit[1]), Number(hit[2]), Number(hit[3])] : [0, 0, 0]
    }
    const lum = ([r, g, b]: [number, number, number]): number => 0.2126 * r + 0.7152 * g + 0.0722 * b
    const panel = document.querySelector('.ed-panel')
    const label = document.querySelector('.wf-field__label')
    if (panel === null || label === null) return null
    return Math.abs(lum(parse(getComputedStyle(panel).backgroundColor)) - lum(parse(getComputedStyle(label).color)))
  })
  expect(contrast, '读不到面板底与字段文字的颜色').not.toBeNull()
  expect(contrast ?? 0, `深色下字段文字与面板底只差 ${String(contrast)} 档亮度`).toBeGreaterThan(60)
  await page.screenshot({ path: 'test-results/d4/form-dark-900.png' })
})
