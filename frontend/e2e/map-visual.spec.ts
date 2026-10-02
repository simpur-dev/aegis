/**
 * 视觉烟雾门禁：在真浏览器（本机 chromium + SwiftShader）里打开构建好的一张图，
 * 用**画面、网络与场景状态**三路互相印证"地形与影像真的出来了"。
 *
 * 为什么三路都要：上一轮影像矩形越界把渲染循环打死（`undefined.x`）时，单元与集成用例全绿——
 * 因为替身不解码、无头也不能建网格。只看截图也不行：GL 上下文丢了会整片黑，而场景 API 里的
 * 高程与瓦计数却一切正常。反过来只看场景 API 也不行：生产包里类名被压缩（实测
 * `terrainProvider.constructor.name === 'hd'`），"是不是我们那个 provider"只能靠行为证明。
 * 所以三条各自管一段：画面管"看得见"，网络管"字节是真的"，场景管"高程是这块地形的"。
 *
 * 跑的是构建产物（`npm run build` → `vite preview`），场景状态走 `?mapDebug` 显式挂出的只读句柄
 * （见 src/components/map/debugHandle.ts）。断言只用 Cesium 公开 API；
 * `_surface._tilesToRender` 这类私有字段只打印作现场取证，不参与判定。
 */

import { writeFile } from 'node:fs/promises'
import { join } from 'node:path'

import { expect, test, type Page } from '@playwright/test'

import { pngPixelStats } from './pngStats'

/** 现场图留这一张（目录已 gitignore）：数值断言之外的取景判断要靠它能被复核。 */
const ARTIFACT_PATH = join('test-results', 'e2e', 'map-canvas.png')

/**
 * 采样点刻意取瓦内部、离瓦边至少 3 个格点。原因不是猜的：Cesium 给 quantized-mesh 算的裙边是
 * `getLevelMaximumGeometricError(level) * 5`，我们这种只烘到 maxzoom 2 的粗金字塔里 L2 误差还有
 * 19 km，裙边就深达 96 km；实测贴边样本取回 -7638 m，而那张瓦自己的高度界是 [-8, 0]。
 * 那是引擎的裙边而不是我们的字节，所以门禁采 interior 点。
 */
const PLATEAU = { lon: 85.0, lat: 31.0 }
const LOW_INTERIOR = { lon: 60.0, lat: -20.0 }

const MAP_ROUTE = '/map?mapDebug=1'
const CESIUM_ERROR_PANEL = '.cesium-widget-errorPanel'
const MAP_CANVAS = '.cesium-widget canvas'
/** 真正被当作数据消费的资产：地形瓦与 PMTiles 归档。它们被兜底页顶掉就是故障。 */
const DATA_PATH = /\/terrain\/|\.pmtiles(\?|$)/i
/** 只是候选地址的探针（XYZ 模板、可选静态矢量文件）：回兜底页是预期，决策逻辑正靠此判死。 */
const PROBE_PATH = /\/basemaps\//i

interface AssetResponse {
  readonly url: string
  readonly status: number
  readonly contentType: string
  /** 取不到体（例如已被浏览器回收）时为 null；判定只统计拿得到的那些。 */
  readonly body: Buffer | null
}

/** 兜底页的判据看**字节**而不是响应头：vite 给 .terrain 回的 content-type 实测是空，而兜底页永远以 doctype/`<html` 开头。 */
function looksLikeHtml(body: Buffer): boolean {
  const head = body.subarray(0, 512).toString('latin1').trimStart().toLowerCase()
  return head.startsWith('<!doctype html') || head.startsWith('<html')
}

interface SceneFacts {
  readonly handlePresent: boolean
  readonly tilesLoaded: boolean
  readonly renderedTileCount: number
  readonly renderedLevels: number[]
  readonly imageryLayerCount: number
  readonly cameraHeightMeters: number | null
  readonly heightAtPlateau: number | null
  readonly heightAtLowInterior: number | null
}

interface PendingResponse {
  readonly meta: Omit<AssetResponse, 'body'>
  readonly body: Promise<Buffer | null>
}

let dataResponses: PendingResponse[] = []
let probeResponses: PendingResponse[] = []
let requestUrls: string[] = []
let pageErrors: string[] = []
let consoleErrors: string[] = []

test.beforeEach(async ({ page }) => {
  dataResponses = []
  probeResponses = []
  requestUrls = []
  pageErrors = []
  consoleErrors = []
  // 每个请求都记一条 URL：下面那条"一张图不出网"的断言靠的就是它，
  // 而 Cesium 想偷偷去要 Ion 令牌只会多出一个外部主机名。
  page.on('request', (request) => requestUrls.push(request.url()))
  page.on('response', (response) => {
    const url = response.url()
    const isData = DATA_PATH.test(url)
    if (!isData && !PROBE_PATH.test(url)) return
    const meta = { url, status: response.status(), contentType: response.headers()['content-type'] ?? '' }
    // 响应体必须在到达这一刻就取：过一会儿再取，CDP 会报
    // `Protocol error (Network.getResponseBody): No data found for resource with given identifier`（实测）。
    const body = response.body().catch(() => null)
    if (isData) dataResponses.push({ meta, body })
    else probeResponses.push({ meta, body })
  })
  page.on('pageerror', (error) => pageErrors.push(String(error?.message ?? error)))
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text())
  })

  await page.goto(MAP_ROUTE, { waitUntil: 'domcontentloaded' })
  await expect(page.locator(MAP_CANVAS)).toBeVisible({ timeout: 60_000 })
})

async function inspect(responses: readonly PendingResponse[]): Promise<AssetResponse[]> {
  return Promise.all(
    responses.map(async (entry) => ({ ...entry.meta, body: await entry.body })),
  )
}

test('离线前提：一张图的请求全留在本机源上（不打 Cesium Ion 与任何外部主机）', async ({ page }) => {
  await settle(page, { waitForTerrain: true })
  const origin = new URL(page.url()).origin
  const httpUrls = requestUrls.filter((url) => url.startsWith('http'))
  const foreign = [...new Set(httpUrls.map((url) => new URL(url).origin))].filter((other) => other !== origin)
  // 断言而不是"看一眼控制台"：REPORT 里那句"请求主机只有 localhost"此前只是观察记录，
  // 谁哪天挂了个 Ion 令牌或 CDN 底图，界面照样好看，弱网离线这条口径却已经假了。
  expect(httpUrls.length, '一个请求都没抓到：采集挂了，这条断言会空转').toBeGreaterThan(0)
  expect(foreign, `一张图打到了本机源之外：${foreign.join(', ')}`).toEqual([])
  console.log(
    `[视觉门禁] 离线检查：源=${origin}，http 请求 ${httpUrls.length} 条，` +
      `主机集合=${[...new Set(httpUrls.map((url) => new URL(url).hostname))].sort().join(',')}`,
  )
})

test('画布真的出画：没有渲染错误框，截图也不是单一平色', async ({ page }) => {
  // 等地形进场景再截图：不然拍到的是"只挂了椭球面"的中间态，取景判断就没有意义。
  await settle(page, { waitForTerrain: true })

  await expect(page.locator(CESIUM_ERROR_PANEL)).toHaveCount(0)
  const shot = await page.locator(MAP_CANVAS).screenshot()
  // 留一张现场图：门禁给的是数，判"取景对不对"还得看图，而图不在断言里就没人能复核。
  await writeFile(ARTIFACT_PATH, shot)
  const stats = pngPixelStats(shot)
  console.log(
    `[视觉门禁] 画布 ${stats.width}×${stats.height}，取样 ${stats.samples} 像素，颜色 ${stats.distinctColors} 种，` +
      `主色占 ${(stats.dominantShare * 100).toFixed(1)}%，高原山体阴影（橄榄色）占 ${(stats.oliveShare * 100).toFixed(2)}%，` +
      `亮度 ${stats.darkestLuminance.toFixed(1)}..${stats.brightestLuminance.toFixed(1)}`,
  )

  // 主色几乎占满就是"整片一个色"：既覆盖渲染崩溃后的黑屏，也覆盖 GL 上下文丢了但场景 API 正常的情况。
  expect(stats.samples).toBeGreaterThan(10_000)
  expect(stats.dominantShare, `画布几乎是单一颜色：${JSON.stringify(stats)}`).toBeLessThan(0.98)
  expect(stats.distinctColors).toBeGreaterThan(8)
  expect(stats.brightestLuminance - stats.darkestLuminance).toBeGreaterThan(20)
  // 高原的山体阴影必须在画面上看得见：合成底图把起伏烘成暗橄榄色，低海拔则是浅米色。
  // 只判"有没有这种颜色"，不判它出现在哪个像素——位置属于取景观感，不是链路事实。
  expect(stats.oliveShare, `画面里没有高原起伏的着色：${JSON.stringify(stats)}`).toBeGreaterThan(0.001)
})

test('被当数据消费的资产都是 2xx 且是真字节；探针那一路判死之后仍留下了可用图层', async ({ page }) => {
  const facts = await settle(page)
  const consumed = await inspect(dataResponses)
  const probes = await inspect(probeResponses)
  const terrainTiles = consumed.filter((entry) => /\/terrain\/\d+\/\d+\/\d+\.terrain/.test(entry.url))
  console.log(
    `[视觉门禁] 消费型资源 ${consumed.length} 次（地形瓦 ${terrainTiles.length}，字节 ` +
      `${terrainTiles.map((entry) => entry.body?.length ?? -1).join('/')}），探针型 ${probes.length} 次；场景=${JSON.stringify(facts)}`,
  )

  expect(terrainTiles.length, '一次地形瓦请求都没发生：layer.json 或瓦路径没挂上').toBeGreaterThan(0)
  const failed = consumed.filter((entry) => entry.status < 200 || entry.status >= 300)
  expect(failed.map((entry) => `${entry.url} → ${entry.status}`), '消费型资源不是 2xx').toEqual([])
  const fallback = consumed.filter((entry) => entry.body !== null && looksLikeHtml(entry.body))
  // 判字节而不是判 MIME：vite 的静态服务不认识 .terrain，实测回的是**空** content-type
  // （自托管上 nginx 要在 mime.types 里补 application/vnd.quantized-mesh）。按 MIME 白名单判会把
  // 正常的地形腿判死——上一轮探针就栽在"只看 200"和"只看 MIME"这两个相反方向上。
  expect(fallback.map((entry) => entry.url), '有资产被 SPA 兜底页顶掉后仍被当数据用').toEqual([])
  const undersized = terrainTiles.filter((entry) => (entry.body?.length ?? 0) < 1_000)
  expect(undersized.map((entry) => `${entry.url} → ${entry.body?.length ?? 'no-body'}`), '地形瓦没有一张像样').toEqual([])

  // 探针那一路允许被兜底页顶掉（那正是"没有真 XYZ 瓦片服务"的现场形状），
  // 但结论必须是"仍然挂上了一层可用影像"，否则就是退化没兜住。
  if (probes.some((entry) => entry.body !== null && looksLikeHtml(entry.body))) {
    expect(facts.imageryLayerCount, '探针被兜底页顶掉后，影像图层应当仍由本地归档提供').toBeGreaterThan(0)
  }
})

test('场景里的高程是真的：高原有起伏，另一处低瓦没有', async ({ page }) => {
  const facts = await settle(page, { waitForTerrain: true })

  expect(facts.handlePresent, '没挂上 ?mapDebug 句柄：装配面改了？').toBe(true)
  expect(facts.imageryLayerCount).toBeGreaterThan(0)
  // 椭球面地形在这里只会给 0/undefined，取到 4000 m 以上只可能是我们烘的 quantized-mesh 上了场景——
  // 所以这条就是"provider 是谁"的行为证据，不看被压缩过的类名（实测生产包里是 'hd'）。
  expect(facts.heightAtPlateau, `高原处取不到高程：${JSON.stringify(facts)}`).not.toBeNull()
  expect(facts.heightAtPlateau as number).toBeGreaterThan(4_000)
  // 只判"这里没有高原"：贴瓦边时引擎的裙边会把插值结果拖到几万米负值（已实测），
  // 所以下界不属地形资产的承诺，硬判就是假红。要判负值是否来自裙边，见 terrain-parse.spec.ts 的逐顶点反算。
  expect(facts.heightAtLowInterior, `低瓦内部本不该有起伏，实际 ${facts.heightAtLowInterior}`).toBeLessThan(1_000)

  // 私有字段只打印：升级挪了位置也只会少一项日志，不会把门禁搞成假红。
  console.log(`[视觉门禁] 渲染中的瓦 ${facts.renderedTileCount} 张，层级 ${facts.renderedLevels.join(',') || '无'}，相机高 ${facts.cameraHeightMeters?.toFixed(0)} m`)
})

test('画面上的默认署名是自托管说明，不是 Cesium ion 的 logo', async ({ page }) => {
  await settle(page)

  const credits = page.locator('.cesium-widget-credits').first()
  await expect(credits).toBeVisible()
  await expect(credits).toContainText('自托管')
  // P0 口径是"不依赖 Ion/令牌"：代码里没取 ion 资产不算数，画面上写着 ion 就是自相矛盾的证据。
  const html = await credits.innerHTML()
  expect(html).not.toMatch(/cesium\.com|ion-credit|Cesium ion/i)
})

test('整条链路没有把渲染相关异常吞进 console', async ({ page }) => {
  await settle(page)

  const renderRelated = [...pageErrors, ...consoleErrors].filter((text) => /render|terrain|imagery|quantized|Cesium|undefined is not|cannot read/i.test(text))
  expect(renderRelated, `渲染相关报错：${renderRelated.slice(0, 3).join(' | ')}`).toEqual([])
})

/**
 * 等场景稳定：`requestRenderMode: true` 下 Cesium 只在"有变化"时重绘，
 * 不主动 requestRender 的话瓦会一直停在已请求未加载的状态（与静置轻薄本的现场行为一致）。
 */
async function settle(page: Page, options: { waitForTerrain?: boolean } = {}): Promise<SceneFacts> {
  const deadline = Date.now() + 60_000
  for (;;) {
    await page.evaluate(() => {
      const handle = (window as unknown as { __aegisMap?: { viewer?: { scene?: { requestRender?: () => void } } } }).__aegisMap
      handle?.viewer?.scene?.requestRender?.()
    })
    await page.waitForTimeout(150)
    const facts = await readSceneFacts(page)
    const terrainInScene = facts.heightAtPlateau !== null && facts.heightAtPlateau > 4_000
    if (terrainInScene) return facts
    if (!options.waitForTerrain && facts.renderedTileCount > 0 && facts.imageryLayerCount > 0) return facts
    if (Date.now() > deadline) return facts
  }
}

async function readSceneFacts(page: Page): Promise<SceneFacts> {
  // 页面里没有全局 Cesium，也没有 Node 侧的闭包变量可用：evaluate 体内只能用自己的字面量。
  return page.evaluate(([plateau, lowInterior]) => {
    const viewer = (window as unknown as { __aegisMap?: { viewer?: any } }).__aegisMap?.viewer
    if (!viewer) {
      return {
        handlePresent: false,
        tilesLoaded: false,
        renderedTileCount: 0,
        renderedLevels: [] as number[],
        imageryLayerCount: 0,
        cameraHeightMeters: null as number | null,
        heightAtPlateau: null as number | null,
        heightAtLowInterior: null as number | null,
      }
    }
    const globe = viewer.scene.globe
    const radians = (degrees: number) => (degrees * Math.PI) / 180
    // Globe.getHeight 只读 longitude/latitude 两个属性，不必请 Cesium 造 Cartographic。
    const heightAt = (point: { lon: number; lat: number }) => {
      const value = globe.getHeight({ longitude: radians(point.lon), latitude: radians(point.lat), height: 0 })
      return typeof value === 'number' && Number.isFinite(value) ? value : null
    }
    const rendered: any[] = globe._surface?._tilesToRender ?? []
    return {
      handlePresent: true,
      tilesLoaded: Boolean(globe.tilesLoaded),
      renderedTileCount: rendered.length,
      renderedLevels: [...new Set(rendered.map((tile) => tile.level as number))].sort((a, b) => a - b),
      imageryLayerCount: globe.imageryLayers ? globe.imageryLayers.length : 0,
      cameraHeightMeters: viewer.camera.positionCartographic ? viewer.camera.positionCartographic.height : null,
      heightAtPlateau: heightAt(plateau),
      heightAtLowInterior: heightAt(lowInterior),
    }
  }, [PLATEAU, LOW_INTERIOR])
}
